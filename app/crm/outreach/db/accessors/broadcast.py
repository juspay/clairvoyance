"""Mechanical DB access for crm_broadcast (T17) — one table, one file. Two
shapes, by signature: a ``conn`` parameter runs inside the caller's atom; no
parameter self-scopes one statement. No decisions.
"""

from datetime import datetime
from typing import List, Optional

import asyncpg

from app.crm.outreach.db.decoders.broadcast import decode_broadcast
from app.crm.outreach.db.queries.broadcast import (
    cancel_broadcast_query,
    claim_due_freezes_query,
    count_broadcasts_query,
    fail_freeze_query,
    finish_freeze_query,
    get_broadcast_query,
    insert_broadcast_query,
    list_broadcasts_query,
    lock_broadcast_query,
    schedule_broadcast_query,
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


async def schedule_broadcast(
    merchant_id: str, broadcast_id: str, scheduled_for: Optional[datetime]
) -> Optional[Broadcast]:
    query, values = schedule_broadcast_query(merchant_id, broadcast_id, scheduled_for)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_broadcast(row) if row else None


async def lock_broadcast(
    conn: asyncpg.Connection, merchant_id: str, broadcast_id: str
) -> Optional[str]:
    """Inside an atom: lock the row and return its status (None = not this
    merchant's broadcast)."""
    query, values = lock_broadcast_query(merchant_id, broadcast_id)
    row = await conn.fetchrow(query, *values)
    return row["status"] if row else None


async def cancel_broadcast(
    conn: asyncpg.Connection, merchant_id: str, broadcast_id: str
) -> Broadcast:
    """Inside the cancel atom; the row was locked by lock_broadcast first."""
    query, values = cancel_broadcast_query(merchant_id, broadcast_id)
    row = await conn.fetchrow(query, *values)
    assert row is not None  # locked earlier in this same transaction
    return decode_broadcast(row)


async def claim_due_freezes(limit: int, lease_minutes: int) -> List[Broadcast]:
    query, values = claim_due_freezes_query(limit, lease_minutes)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_broadcast(row) for row in rows]


async def finish_freeze(merchant_id: str, broadcast_id: str) -> Optional[Broadcast]:
    query, values = finish_freeze_query(merchant_id, broadcast_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_broadcast(row) if row else None


async def fail_freeze(merchant_id: str, broadcast_id: str) -> Optional[Broadcast]:
    query, values = fail_freeze_query(merchant_id, broadcast_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_broadcast(row) if row else None
