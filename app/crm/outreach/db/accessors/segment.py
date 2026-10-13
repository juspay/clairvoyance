"""Mechanical DB access for crm_segment (T15) — one table, one file (module
rules §1 at scale). Two shapes, by signature: a ``conn`` parameter runs inside
the caller's atom; no parameter self-scopes one statement. No decisions.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.crm.outreach.db.decoders.segment import decode_segment
from app.crm.outreach.db.queries.segment import (
    count_segments_query,
    get_segment_query,
    insert_dynamic_segment_query,
    insert_static_list_query,
    list_segments_query,
    lock_segment_query,
    stamp_member_count_query,
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


async def insert_dynamic_segment(
    merchant_id: str,
    name: str,
    definition: Dict[str, Any],
    created_by: Optional[str],
) -> Segment:
    """Raises the db door's UniqueViolation when the name is taken."""
    query, values = insert_dynamic_segment_query(
        merchant_id, name, definition, created_by
    )
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


async def lock_segment(
    conn: asyncpg.Connection, merchant_id: str, segment_id: str
) -> Optional[bool]:
    """Inside the add-members atom: lock this merchant's segment row and say
    whether it is a static list. None = no such segment for this merchant."""
    query, values = lock_segment_query(merchant_id, segment_id)
    row = await conn.fetchrow(query, *values)
    return bool(row["is_static"]) if row else None


async def stamp_member_count(
    conn: asyncpg.Connection, merchant_id: str, segment_id: str
) -> Tuple[int, datetime]:
    """Inside the add-members atom: recount the list from its rows and stamp
    it now. The row was locked by lock_segment in the same atom, so it is
    there."""
    query, values = stamp_member_count_query(merchant_id, segment_id)
    row = await conn.fetchrow(query, *values)
    assert row is not None  # locked earlier in this same transaction
    return int(row["member_count"]), row["computed_at"]
