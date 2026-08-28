"""
Database module for the application.
This module contains database connection and models.

Two pools are maintained:
  write_pool  — connects to the PRIMARY (INSERT / UPDATE / DELETE / transactions).
                Exposed as the module-level ``pool`` alias for backward compat.
  read_pool   — connects to the READ REPLICA when POSTGRES_READER_HOST is set.
                Falls back to ``write_pool`` (the same object) when the env var
                is absent, so all existing behaviour is preserved with zero config
                change.

Use ``get_db_connection()``      for writes / transactions.
Use ``get_read_db_connection()`` for plain SELECT queries.
"""

from typing import Optional

import asyncpg

from app.core.config.static import (
    POSTGRES_DB,
    POSTGRES_HOST,
    POSTGRES_MAX_OVERFLOW,
    POSTGRES_PASSWORD,
    POSTGRES_POOL_SIZE,
    POSTGRES_PORT,
    POSTGRES_READER_HOST,
    POSTGRES_READER_PORT,
    POSTGRES_USER,
)
from app.core.logger import logger
from app.services.aws.kms import decrypt_kms

# ---------------------------------------------------------------------------
# Module-level pool references
# ---------------------------------------------------------------------------
# ``pool`` is kept as a public alias for ``write_pool`` so that any code
# outside this module that imported ``pool`` directly continues to work.
# ---------------------------------------------------------------------------

write_pool: Optional[asyncpg.Pool] = None
read_pool: Optional[asyncpg.Pool] = None

# Backward-compat alias — always mirrors write_pool after initialisation.
pool = write_pool


async def _create_pool(
    host: str,
    port: str,
    min_size: int,
    max_size: int,
    decrypted_password: str,
    label: str,
) -> asyncpg.Pool:
    """Create and return a single asyncpg pool.  Raises on failure."""
    try:
        p = await asyncpg.create_pool(
            user=POSTGRES_USER,
            password=decrypted_password,
            database=POSTGRES_DB,
            host=host,
            port=port,
            min_size=min_size,
            max_size=max_size,
        )
        logger.info(f"{label} pool initialised successfully.")
        return p
    except Exception as e:
        logger.error(f"{label} pool initialisation failed: {e}")
        raise


async def init_db_pool(
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
) -> None:
    """
    Initialise the write pool (primary) and, when POSTGRES_READER_HOST is
    configured, a separate read pool (replica).

    When POSTGRES_READER_HOST is absent the read pool is set to the same
    object as the write pool — all queries go to the primary and the
    application behaves exactly as before.

    Args:
        min_size: Connections opened eagerly.  Defaults to POSTGRES_POOL_SIZE.
            Per-call bot subprocesses pass an explicit small value so each
            child process does not open the full API pod pool.
        max_size: Pool ceiling.  Defaults to POSTGRES_POOL_SIZE +
            POSTGRES_MAX_OVERFLOW.
    """
    global write_pool, read_pool, pool

    resolved_min = min_size if min_size is not None else POSTGRES_POOL_SIZE
    resolved_max = (
        max_size if max_size is not None else POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW
    )

    required_vars = [
        POSTGRES_USER,
        POSTGRES_PASSWORD,
        POSTGRES_HOST,
        POSTGRES_PORT,
        POSTGRES_DB,
    ]
    if not all(required_vars):
        logger.warning(
            "One or more database environment variables are missing. "
            "Skipping database initialisation."
        )
        return

    # KMS-decrypt the password once; reused for both pools.
    decrypted_password = await decrypt_kms(POSTGRES_PASSWORD)
    if decrypted_password is None:
        logger.warning("KMS decryption failed — cannot initialise database pools.")
        return

    # ------------------------------------------------------------------
    # Write pool (primary)
    # ------------------------------------------------------------------
    if write_pool is None:
        write_pool = await _create_pool(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            min_size=resolved_min,
            max_size=resolved_max,
            decrypted_password=decrypted_password,
            label="Write",
        )
        # Keep the legacy ``pool`` alias in sync so callers that imported
        # ``pool`` directly still get a live pool object.
        pool = write_pool

    # ------------------------------------------------------------------
    # Read pool (replica)
    # ------------------------------------------------------------------
    if read_pool is None:
        if POSTGRES_READER_HOST:
            read_pool = await _create_pool(
                host=POSTGRES_READER_HOST,
                port=POSTGRES_READER_PORT or POSTGRES_PORT,
                min_size=resolved_min,
                max_size=resolved_max,
                decrypted_password=decrypted_password,
                label="Read",
            )
        else:
            # No replica configured — alias the write pool so run_read_query
            # transparently routes to the primary.
            read_pool = write_pool
            logger.info(
                "POSTGRES_READER_HOST not set — read_pool aliased to write_pool "
                "(all queries routed to primary)."
            )


# ---------------------------------------------------------------------------
# Connection generators
# ---------------------------------------------------------------------------


async def get_db_connection():
    """
    Yield a connection from the WRITE pool (primary).

    Use for INSERT / UPDATE / DELETE and any transaction that mixes reads
    with writes.
    """
    global write_pool
    if write_pool is None:
        await init_db_pool()

    if write_pool is None:
        raise RuntimeError("Write pool is not initialised.")

    async with write_pool.acquire() as connection:
        yield connection


async def get_read_db_connection():
    """
    Yield a connection from the READ pool (replica when configured, otherwise
    the primary via the write_pool alias).

    Use exclusively for plain SELECT queries where replica lag is acceptable.
    Do NOT use inside a write transaction — call get_db_connection() instead.
    """
    global read_pool
    if read_pool is None:
        await init_db_pool()

    if read_pool is None:
        raise RuntimeError("Read pool is not initialised.")

    async with read_pool.acquire() as connection:
        yield connection


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def close_db_pool() -> None:
    """
    Close both pools gracefully.

    The read pool is only closed separately when it is a distinct object from
    the write pool (i.e. POSTGRES_READER_HOST was configured).  This guard
    prevents a double-close when the two are aliased to the same pool.
    """
    global write_pool, read_pool, pool

    if read_pool is not None and read_pool is not write_pool:
        try:
            await read_pool.close()
            logger.info("Read pool closed.")
        except Exception as e:
            logger.error(f"Failed to close read pool: {e}")
            raise
        finally:
            read_pool = None

    if write_pool is not None:
        try:
            await write_pool.close()
            logger.info("Write pool closed.")
        except Exception as e:
            logger.error(f"Failed to close write pool: {e}")
            raise
        finally:
            write_pool = None
            pool = None
            read_pool = None  # clear alias if it was pointing at write_pool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "init_db_pool",
    "close_db_pool",
    "get_db_connection",
    "get_read_db_connection",
    # Pool references
    "write_pool",
    "read_pool",
    "pool",
]
