"""Mechanical DB access for crm_segment_member (T21) — one table, one file
(module rules §1 at scale). Two shapes, by signature: a ``conn`` parameter
runs inside the caller's atom; no parameter self-scopes one statement.
"""

from typing import List, Optional

import asyncpg

from app.crm.outreach.db.decoders.segment_member import decode_segment_member
from app.crm.outreach.db.queries.segment_member import (
    insert_members_query,
    member_page_query,
)
from app.crm.outreach.schemas import SegmentMember
from app.crm.shared.db import crm_connection


async def insert_members(
    conn: asyncpg.Connection,
    segment_id: str,
    customer_ids: List[str],
    source: Optional[str],
) -> int:
    """Inside the add-members atom: how many rows were really added (a
    customer already on the list adds nothing)."""
    query, values = insert_members_query(segment_id, customer_ids, source)
    rows = await conn.fetch(query, *values)
    return len(rows)


async def member_page(
    merchant_id: str, segment_id: str, after: Optional[str], limit: int
) -> List[SegmentMember]:
    query, values = member_page_query(merchant_id, segment_id, after, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_segment_member(row) for row in rows]
