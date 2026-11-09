"""Mechanical DB access for crm_broadcast (T17) — one table, one file. Each
function self-scopes one statement. No decisions.
"""

from typing import List, Optional

from app.crm.outreach.db.decoders.broadcast import decode_broadcast
from app.crm.outreach.db.queries.broadcast import (
    count_broadcasts_query,
    get_broadcast_query,
    insert_broadcast_query,
    list_broadcasts_query,
)
from app.crm.outreach.schemas import Broadcast
from app.crm.shared.db import crm_connection


async def insert_broadcast(
    merchant_id: str,
    name: str,
    segment_id: str,
    workflow_id: str,
    campaign_key: Optional[str],
    created_by: Optional[str],
) -> Broadcast:
    query, values = insert_broadcast_query(
        merchant_id, name, segment_id, workflow_id, campaign_key, created_by
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    assert row is not None  # INSERT ... RETURNING always yields the row
    return decode_broadcast(row)


async def get_broadcast(merchant_id: str, broadcast_id: str) -> Optional[Broadcast]:
    query, values = get_broadcast_query(merchant_id, broadcast_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_broadcast(row) if row else None


async def list_broadcasts(
    merchant_id: str, campaign_key: Optional[str], limit: int, offset: int
) -> List[Broadcast]:
    query, values = list_broadcasts_query(merchant_id, campaign_key, limit, offset)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_broadcast(row) for row in rows]


async def count_broadcasts(merchant_id: str, campaign_key: Optional[str]) -> int:
    query, values = count_broadcasts_query(merchant_id, campaign_key)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return int(row["total"]) if row else 0
