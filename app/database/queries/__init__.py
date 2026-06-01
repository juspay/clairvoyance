"""
Database query functions for the application.
"""

from typing import Any, List

import asyncpg

from app.database import get_db_connection


# Helper function to execute parameterized queries
async def run_parameterized_query(
    query_text: str, values: List[Any]
) -> List[asyncpg.Record]:
    """
    Execute a parameterized query and return the results.

    Raises exceptions on failure so callers can handle them appropriately
    (all accessor functions have try/except that log and re-raise).
    """
    async for conn in get_db_connection():
        result = await conn.fetch(query_text, *values)
        return result
    return []
