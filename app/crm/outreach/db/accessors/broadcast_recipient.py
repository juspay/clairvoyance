"""Mechanical DB access for crm_broadcast_recipient (T18) — one table, one
file. Two shapes, by signature: a ``conn`` parameter runs inside the
caller's atom, after the parent broadcast was locked in the same
transaction; no parameter self-scopes one read through the parent. No
decisions.
"""

from typing import List, Optional

import asyncpg

from app.crm.outreach.db.queries.broadcast_recipient import (
    cancel_queued_recipients_query,
    insert_recipients_query,
    last_frozen_customer_query,
)
from app.crm.shared.db import crm_connection


async def insert_recipients(
    conn: asyncpg.Connection, broadcast_id: str, customer_ids: List[str]
) -> int:
    """How many rows were really added (the rest were already queued)."""
    query, values = insert_recipients_query(broadcast_id, customer_ids)
    return await conn.fetchval(query, *values) or 0


async def cancel_queued_recipients(conn: asyncpg.Connection, broadcast_id: str) -> int:
    query, values = cancel_queued_recipients_query(broadcast_id)
    return await conn.fetchval(query, *values) or 0


async def last_frozen_customer(merchant_id: str, broadcast_id: str) -> Optional[str]:
    """The largest customer id already frozen (any status), or None."""
    query, values = last_frozen_customer_query(merchant_id, broadcast_id)
    async with crm_connection() as conn:
        customer_id = await conn.fetchval(query, *values)
    return str(customer_id) if customer_id else None
