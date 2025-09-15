"""
Database module for the application.
This module contains database connection and models.
"""

import asyncpg
from app.core.config import (
    POSTGRES_USER,
    POSTGRES_PASSWORD,
    POSTGRES_HOST,
    POSTGRES_PORT,
    POSTGRES_DB,
    POSTGRES_POOL_SIZE,
    POSTGRES_MAX_OVERFLOW,
)
from app.core.logger import logger

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
        try:
            pool = await asyncpg.create_pool(
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                database=POSTGRES_DB,
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                min_size=POSTGRES_POOL_SIZE,
                max_size=POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW,
            )
            logger.info("Database pool initialized successfully.")
        except Exception as e:
            logger.error(f"Database pool initialization failed: {e}")
            raise


async def get_db_connection():
    """
    Get a database connection from the pool.
    """
    if pool is None:
        await init_db_pool()

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
