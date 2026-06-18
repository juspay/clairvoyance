"""
Database module for the application.
This module contains database connection and models.
"""

import asyncpg

try:
    from pgvector.asyncpg import register_vector as _register_vector

    _PGVECTOR_AVAILABLE = True
except ImportError:
    _PGVECTOR_AVAILABLE = False


from app.core.config.static import (
    BUDDY_MEMORY_BACKEND,
    BUDDY_MEMORY_ENABLED,
    POSTGRES_DB,
    POSTGRES_HOST,
    POSTGRES_MAX_OVERFLOW,
    POSTGRES_PASSWORD,
    POSTGRES_POOL_SIZE,
    POSTGRES_PORT,
    POSTGRES_USER,
)
from app.core.logger import logger


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register the pgvector codec on each new connection.

    Only runs when persistent memory is enabled *and* the pgvector backend is
    selected — otherwise a memory-off deployment (or one using the supermemory
    backend) would needlessly require the `vector` extension / migration 032.
    Wrapped so a half-provisioned pgvector deployment degrades with a warning
    instead of failing every connection (which would kill the whole pool).
    """
    if not (
        _PGVECTOR_AVAILABLE
        and BUDDY_MEMORY_ENABLED
        and BUDDY_MEMORY_BACKEND == "pgvector"
    ):
        return
    try:
        await _register_vector(conn)  # type: ignore[arg-type]
    except Exception as e:
        # vector extension / migration 032 not applied — memory will degrade,
        # but the connection (and the rest of the app) stays healthy.
        logger.warning(f"pgvector codec registration skipped: {e}")


from app.services.aws.kms import decrypt_kms

pool = None


async def init_db_pool():
    """
    Initialize the database connection pool.
    """
    global pool
    if pool is None:
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
                min_size=POSTGRES_POOL_SIZE,
                max_size=POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW,
                init=_init_connection,
            )
            logger.info("Database pool initialized successfully.")
        except Exception as e:
            logger.error(f"Database pool initialization failed: {e}")
            raise


async def get_db_connection():
    """
    Get a database connection from the pool.
    """
    global pool
    if pool is None:
        await init_db_pool()

    if pool is None:
        raise RuntimeError("Database pool is not initialized")

    async with pool.acquire() as connection:
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
    "get_db_connection",
    "close_db_pool",
]
