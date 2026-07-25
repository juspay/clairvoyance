"""
Database query functions for the application.

Two entry points are provided:

  run_parameterized_query(query, values)
      Executes on the WRITE pool (primary).
      Use for INSERT, UPDATE, DELETE, and any query that must see the latest
      committed data (e.g. a SELECT immediately after a write in the same
      request cycle, or SELECT … FOR UPDATE).

  run_read_query(query, values)
      Executes on the READ pool (replica when POSTGRES_READER_HOST is set,
      otherwise the primary via the write_pool alias — safe fallback with no
      config change required).
      Use for plain SELECT queries where a small replication lag is acceptable.

Both functions raise exceptions on failure so that all accessor functions
(which have their own try/except that log and re-raise) handle errors
appropriately.
"""

from typing import Any, List

import asyncpg

from app.database import get_db_connection, get_read_db_connection


async def run_parameterized_query(
    query_text: str, values: List[Any]
) -> List[asyncpg.Record]:
    """
    Execute a parameterized query on the WRITE pool (primary) and return all
    result rows.

    Use for INSERT / UPDATE / DELETE and for SELECT queries that must read
    the latest committed data (e.g. post-write reads, locking queries).
    """
    async for conn in get_db_connection():
        result = await conn.fetch(query_text, *values)
        return result
    return []


async def run_read_query(query_text: str, values: List[Any]) -> List[asyncpg.Record]:
    """
    Execute a parameterized SELECT query on the READ pool (replica) and return
    all result rows.

    When POSTGRES_READER_HOST is not configured the read pool is aliased to
    the write pool, so this function transparently routes to the primary with
    no configuration change required.

    Do NOT use this for:
      - INSERT / UPDATE / DELETE
      - SELECT … FOR UPDATE  (must hold a write-pool connection)
      - Reads inside a write transaction that need to see uncommitted data
    """
    async for conn in get_read_db_connection():
        result = await conn.fetch(query_text, *values)
        return result
    return []
