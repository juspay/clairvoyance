"""
Database module for the application.
This module contains database connection and models.
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
    POSTGRES_STATEMENT_CACHE_SIZE,
    POSTGRES_USER,
)
from app.core.logger import logger
from app.services.aws.kms import decrypt_kms

pool = None


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
    if pool is None:
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
                statement_cache_size=POSTGRES_STATEMENT_CACHE_SIZE,
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
