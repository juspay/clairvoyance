"""
Database module for the application.
This module contains database connection and models.
"""

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

pool: asyncpg.Pool | None = None


def get_pool() -> asyncpg.Pool:
    """Return the initialised connection pool, raising if not yet ready."""
    if pool is None:
        raise RuntimeError(
            "Database pool is not initialized. Call init_db_pool() first."
        )
    return pool


async def _init_vector_codec(conn: asyncpg.Connection) -> None:
    """Register pgvector <-> Python list codec on a connection.

    asyncpg does not know the ``vector`` type by default.  We register a
    text codec so that:
      - Python lists / numpy arrays are sent as the text representation
        ``'[0.1, -0.2, ...]'`` that pgvector accepts.
      - Values read back from the DB are returned as Python lists of floats.

    This is called via ``init`` in ``create_pool`` so it runs for every
    connection in the pool automatically.

    The ``vector`` extension itself is created by migration 023 — we do not
    issue DDL here to avoid requiring elevated privileges on every connection.
    """
    # Fetch the OID of the vector type (must already exist via migration)
    vector_oid = await conn.fetchval(
        "SELECT oid FROM pg_type WHERE typname = 'vector' LIMIT 1"
    )
    if vector_oid is None:
        logger.warning(
            "pgvector extension not available — RAG vector search will not work"
        )
        return

    def _encode_vector(value: object) -> str:  # list[float] | np.ndarray → str
        if hasattr(value, "tolist"):
            value = value.tolist()  # type: ignore[union-attr]
        return "[" + ",".join(str(float(v)) for v in value) + "]"  # type: ignore[arg-type]

    def _decode_vector(data: str) -> list:  # '[0.1,…]' → list[float]
        inner = data.strip("[]").strip()
        if not inner:
            return []
        return [float(x) for x in inner.split(",")]

    await conn.set_type_codec(
        "vector",
        encoder=_encode_vector,
        decoder=_decode_vector,
        schema="public",
        format="text",
    )


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
                init=_init_vector_codec,
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
    "get_pool",
    "close_db_pool",
]
