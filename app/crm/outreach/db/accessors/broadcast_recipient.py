"""Mechanical DB access for crm_broadcast_recipient (T18) — one table, one
file. Every function runs inside the caller's atom (a ``conn`` parameter):
the parent broadcast is locked first, in the same transaction. No decisions.
"""

from typing import List

import asyncpg

from app.crm.outreach.db.queries.broadcast_recipient import (
    cancel_queued_recipients_query,
    insert_recipients_query,
)


async def insert_recipients(
    conn: asyncpg.Connection, broadcast_id: str, customer_ids: List[str]
) -> int:
    """How many rows were really added (the rest were already queued)."""
    query, values = insert_recipients_query(broadcast_id, customer_ids)
    return await conn.fetchval(query, *values) or 0


async def cancel_queued_recipients(conn: asyncpg.Connection, broadcast_id: str) -> int:
    query, values = cancel_queued_recipients_query(broadcast_id)
    return len(await conn.fetch(query, *values))
