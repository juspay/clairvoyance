"""
Database query functions for the application.
"""

from typing import Any, List, Optional

import asyncpg

from app.database import db_connection


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
