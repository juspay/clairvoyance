"""
Database module for the application.
This module contains database connection and models.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg

from app.core.config.static import (
    POD_ROLE,
    POSTGRES_DB,
    POSTGRES_HOST,
    POSTGRES_MAX_OVERFLOW,
    POSTGRES_PASSWORD,
    POSTGRES_POOL_SIZE,
    POSTGRES_PORT,
    POSTGRES_READER_DB,
    POSTGRES_READER_HOST,
    POSTGRES_READER_PASSWORD,
    POSTGRES_READER_PORT,
    POSTGRES_READER_USER,
    POSTGRES_USER,
)
from app.core.logger import logger
from app.services.aws.kms import decrypt_kms

pool = None
reader_pool = None
# Serialises pool CREATION. The lifespan swallows an init failure and keeps
# serving (app/main.py), so `pool` can still be None when traffic arrives --
# and `create_pool()` awaits, so concurrent cold starts would each build a
# pool and the last assignment would win. The losers stay open holding
# min_size connections with no reference left to close them. Measured before
# this lock: 5 concurrent cold starts created 5 pools, 4 unreachable.
_pool_init_lock = asyncio.Lock()


async def init_db_pool(min_size: Optional[int] = None, max_size: Optional[int] = None):
    """
    Initialize the database connection pool.

    Args:
        min_size: Connections opened eagerly. Defaults to POSTGRES_POOL_SIZE.
            Per-call bot subprocesses pass an explicit small size so each
            child doesn't open the API pod's full pool.
        max_size: Pool ceiling. Defaults to POSTGRES_POOL_SIZE +
            POSTGRES_MAX_OVERFLOW.

    When POSTGRES_READER_HOST is set, a second pool is opened against the
    read replica using the same min/max sizes. The reader is optional
    infrastructure: if its creation fails the process keeps running with
    reader_pool = None and read queries fall back to the writer pool.
    """
    global pool
    if pool is not None:
        return
    async with _pool_init_lock:
        # Re-check under the lock: another cold start may have won the race
        # while this one waited.
        if pool is None:
            await _create_pool(min_size, max_size)
            if pool is not None:
                await _create_reader_pool(min_size, max_size)


async def _create_pool(
    min_size: Optional[int] = None, max_size: Optional[int] = None
) -> None:
    """Build the pool. Callers hold _pool_init_lock."""
    global pool
    if min_size is None:
        min_size = POSTGRES_POOL_SIZE
    if max_size is None:
        max_size = POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW
    db_env_vars = [
        POSTGRES_USER,
        POSTGRES_PASSWORD,
        POSTGRES_HOST,
        POSTGRES_PORT,
        POSTGRES_DB,
    ]
    if not all(db_env_vars):
        logger.warning(
            "One or more database environment variables are missing. Skipping database initialization."
        )
        return

    # Decrypt PostgreSQL password using KMS if needed
    decrypted_postgres_password = await decrypt_kms(POSTGRES_PASSWORD)

    # If decryption fails, use the original password
    if decrypted_postgres_password is None:
        logger.warning("KMS decryption failed, using original password")
        return

    try:
        pool = await asyncpg.create_pool(
            user=POSTGRES_USER,
            password=decrypted_postgres_password,
            database=POSTGRES_DB,
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            min_size=min_size,
            max_size=max_size,
        )
        logger.info("Database pool initialized successfully.")
    except Exception as e:
        logger.error(f"Database pool initialization failed: {e}")
        raise


async def _create_reader_pool(
    min_size: Optional[int] = None, max_size: Optional[int] = None
) -> None:
    """Build the reader pool when POSTGRES_READER_HOST is set.

    The reader is optional infrastructure: any failure here is logged and
    swallowed -- reader_pool stays None and read queries fall back to the
    writer pool. Callers hold _pool_init_lock.
    """
    global reader_pool
    if POD_ROLE == "agent_pool":
        # Agent pods only run calls, and no call path reads from the replica.
        logger.info("POD_ROLE=agent_pool; skipping reader pool.")
        return
    if not POSTGRES_READER_HOST:
        logger.info("No reader database configured; all queries use the writer.")
        return
    if reader_pool is not None:
        return
    if min_size is None:
        min_size = POSTGRES_POOL_SIZE
    if max_size is None:
        max_size = POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW

    try:
        # A separately configured POSTGRES_READER_PASSWORD is decrypted on
        # its own; a same-credential replica reuses the writer's password,
        # decrypted here exactly as _create_pool() did (a passthrough on
        # GCP, so no extra cost).
        if POSTGRES_READER_PASSWORD:
            reader_password = await decrypt_kms(POSTGRES_READER_PASSWORD)
            if reader_password is None:
                raise ValueError("KMS decryption failed for POSTGRES_READER_PASSWORD")
        else:
            reader_password = await decrypt_kms(POSTGRES_PASSWORD)
            if reader_password is None:
                raise ValueError("KMS decryption failed for POSTGRES_PASSWORD")

        reader_pool = await asyncpg.create_pool(
            user=POSTGRES_READER_USER or POSTGRES_USER,
            password=reader_password,
            database=POSTGRES_READER_DB or POSTGRES_DB,
            host=POSTGRES_READER_HOST,
            port=POSTGRES_READER_PORT or POSTGRES_PORT,
            min_size=min_size,
            max_size=max_size,
        )
        logger.info("Reader database pool initialized successfully.")
    except Exception as e:
        logger.error(f"Reader database pool initialization failed: {e}")
        # reader_pool stays None; read queries fall back to the writer.


async def _ensure_pool() -> asyncpg.Pool:
    """The pool, initialising it on first use."""
    global pool
    if pool is None:
        await init_db_pool()
    if pool is None:
        raise RuntimeError("Database pool is not initialized")
    return pool


@asynccontextmanager
async def db_connection() -> AsyncIterator[asyncpg.Connection]:
    """A pooled connection whose release is GUARANTEED on exit.

    Prefer this over get_db_connection() everywhere. The difference is not
    style, it is connection count:

        async for conn in get_db_connection():
            return await conn.fetch(...)     # <- `return` jumps out of the
                                             #    loop WITHOUT closing the
                                             #    generator, so the
                                             #    `async with pool.acquire()`
                                             #    exit below never runs here.

    Python then releases the connection only when the event loop finalises
    the abandoned async generator -- several loop iterations later. Any query
    issued before that (a setup burst, a drain loop, a `for action in ...`
    loop) finds every connection still checked out and opens another one.

    Measured against a real Postgres: six SEQUENTIAL queries through the
    generator open THREE connections; the same six through this open ONE.
    `async with` runs __aexit__ on every path -- return, break, or exception
    -- so the connection is back in the pool before the next statement.
    """
    conn_pool = await _ensure_pool()
    async with conn_pool.acquire() as connection:
        yield connection


async def get_db_connection():
    """Async-generator form, kept for callers that iterate it properly.

    DEPRECATED for new code: a caller that `return`s out of `async for`
    abandons this generator and leaks the connection until the event loop
    finalises it. Use db_connection() instead -- see its docstring.
    """
    async with db_connection() as connection:
        yield connection


def is_reader_configured() -> bool:
    """
    Whether this process has a reader pool. Read queries can use
    reader_db_connection()/run_reader_query unconditionally either way.
    """
    return reader_pool is not None


@asynccontextmanager
async def reader_db_connection(
    timeout: Optional[float] = None,
) -> AsyncIterator[asyncpg.Connection]:
    """A pooled READ connection: the reader pool if one exists, else the writer.

    ``timeout`` also caps cleanup: asyncpg reuses it as the release budget
    (pool.py:222), so unset, a black-holed replica blocks this exit for ~77s.
    """
    target_pool = reader_pool if reader_pool is not None else await _ensure_pool()
    # `or None`: 0 means "no cap" here, but acquire(timeout=0) fails instantly.
    async with target_pool.acquire(timeout=timeout or None) as connection:
        yield connection


async def close_db_pool():
    """
    Close the database connection pools.
    """
    global pool, reader_pool
    # Held, not raised: raising here would skip the reader below and leave its
    # connections open on the replica.
    writer_error: Optional[Exception] = None
    if pool:
        try:
            await pool.close()
            logger.info("Database pool closed.")
        except Exception as e:
            logger.error(f"Failed to close database pool: {e}")
            writer_error = e
    if reader_pool:
        try:
            await reader_pool.close()
            logger.info("Reader database pool closed.")
        except Exception as e:
            # Best-effort: a reader close failure must not mask the writer's.
            logger.error(f"Failed to close reader database pool: {e}")
        finally:
            reader_pool = None
    if writer_error:
        raise writer_error


__all__ = [
    "init_db_pool",
    "db_connection",
    "get_db_connection",
    "reader_db_connection",
    "is_reader_configured",
    "close_db_pool",
]
