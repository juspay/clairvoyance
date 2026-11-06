"""Mechanical DB access for crm_segment (T15) — one table, one file (module
rules §1 at scale). Every function self-scopes one statement; no decisions.
"""

from typing import List, Optional

from app.crm.outreach.db.decoders.segment import decode_segment
from app.crm.outreach.db.queries.segment import (
    count_segments_query,
    get_segment_query,
    insert_static_list_query,
    list_segments_query,
)
from app.crm.outreach.schemas import Segment
from app.crm.shared.db import crm_connection


async def insert_static_list(
    merchant_id: str, name: str, created_by: Optional[str]
) -> Segment:
    """Raises the db door's UniqueViolation when the merchant already has a
    segment with this name — the caller says what that means."""
    query, values = insert_static_list_query(merchant_id, name, created_by)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    assert row is not None  # INSERT ... RETURNING always yields the row
    return decode_segment(row)


async def get_segment(merchant_id: str, segment_id: str) -> Optional[Segment]:
    query, values = get_segment_query(merchant_id, segment_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_segment(row) if row else None


async def list_segments(merchant_id: str, limit: int, offset: int) -> List[Segment]:
    query, values = list_segments_query(merchant_id, limit, offset)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_segment(row) for row in rows]


async def count_segments(merchant_id: str) -> int:
    query, values = count_segments_query(merchant_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return int(row["total"]) if row else 0
