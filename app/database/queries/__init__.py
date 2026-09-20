"""
Database query functions for the application.
"""

from typing import Any, List, Optional

import asyncpg

from app.core.config.static import POSTGRES_READER_TIMEOUT_SECS
from app.core.logger import logger
from app.database import (
    db_connection,
    is_reader_configured,
    reader_db_connection,
)


# Helper function to execute parameterized queries
async def run_parameterized_query(
    query_text: str, values: List[Any], timeout: Optional[float] = None
) -> List[asyncpg.Record]:
    """
    Execute a parameterized query and return the results.

    Raises exceptions on failure so callers can handle them appropriately
    (all accessor functions have try/except that log and re-raise).

    ``timeout`` (seconds) bounds the statement server-side: asyncpg sends a
    cancel request when it fires, so a read the browser has already given
    up on cannot keep running on Postgres. Reads a client can cancel — the
    console's reports — pass one; the default keeps every other caller as
    it was.
    """
    async with db_connection() as conn:
        if timeout is None:
            return await conn.fetch(query_text, *values)
        return await conn.fetch(query_text, *values, timeout=timeout)


# Helper function to execute parameterized READ queries on the replica
async def run_reader_query(
    query_text: str, values: List[Any], timeout: Optional[float] = None
) -> List[asyncpg.Record]:
    """Read from the replica when configured, else the writer; retry once on
    the writer if the reader fails. Lag-tolerant reads only.

    ``timeout`` defaults to POSTGRES_READER_TIMEOUT_SECS (0 to opt out) and
    bounds the statement and the cancel -- the fallback needs a raise.
    """
    reader_timeout = timeout if timeout is not None else POSTGRES_READER_TIMEOUT_SECS

    if not is_reader_configured():
        return await run_parameterized_query(query_text, values, timeout)

    try:
        async with reader_db_connection(reader_timeout) as conn:
            if not reader_timeout:
                return await conn.fetch(query_text, *values)
            return await conn.fetch(query_text, *values, timeout=reader_timeout)
    except Exception as e:
        logger.warning(f"Reader query failed, falling back to writer: {e}")
        return await run_parameterized_query(query_text, values, timeout)
