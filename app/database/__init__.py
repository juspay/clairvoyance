"""
Database module for the application.
This module contains database connection and models.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg

from app.core.config.static import (
    POSTGRES_DB,
    POSTGRES_HOST,
    POSTGRES_MAX_OVERFLOW,
    POSTGRES_PASSWORD,
    POSTGRES_POOL_SIZE,
    POSTGRES_PORT,
    POSTGRES_USER,
)
from app.core.logger import logger
from app.services.aws.kms import decrypt_kms

pool = None
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
    """
    global pool
    if pool is not None:
        return
    async with _pool_init_lock:
        # Re-check under the lock: another cold start may have won the race
        # while this one waited.
        if pool is None:
            await _create_pool(min_size, max_size)


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


async def close_db_pool():
    """
    Close the database connection pool.
    """
    if pool:
        try:
            await pool.close()
            logger.info("Database pool closed.")
        except Exception as e:
            logger.error(f"Failed to close database pool: {e}")
            raise


__all__ = [
    "init_db_pool",
    "db_connection",
    "get_db_connection",
    "close_db_pool",
]
