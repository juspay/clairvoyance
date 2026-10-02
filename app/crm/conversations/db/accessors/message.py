"""Mechanical access to crm_conversation_message."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.conversations.db.decoders.rows import decode_timeline
from app.crm.conversations.db.queries import message as q
from app.crm.conversations.schemas import TimelineRow
from app.crm.shared.db import DbTxn, crm_connection


async def _one(
    txn: Optional[DbTxn], query: str, values: List[Any]
) -> Optional[TimelineRow]:
    if txn is not None:
        row = await txn.fetchrow(query, *values)
    else:
        async with crm_connection() as conn:
            row = await conn.fetchrow(query, *values)
    return decode_timeline(row) if row is not None else None


async def insert_inbound(
    txn: Optional[DbTxn],
    merchant_id: str,
    thread_id: str,
    kind: str,
    author_kind: str,
    event_raw_id: Optional[str],
    provider_message_id: Optional[str],
    body: Optional[Dict[str, Any]],
    occurred_at: datetime,
) -> Optional[TimelineRow]:
    """None = this letter is already on the timeline."""
    query, values = q.insert_inbound_query(
        merchant_id,
        thread_id,
        kind,
        author_kind,
        event_raw_id,
        provider_message_id,
        body,
        occurred_at,
    )
    return await _one(txn, query, values)


async def insert_outbound(
    txn: Optional[DbTxn],
    merchant_id: str,
    thread_id: str,
    kind: str,
    author_kind: str,
    author_user_id: Optional[str],
    message_id: Optional[str],
    provider_message_id: Optional[str],
    body: Optional[Dict[str, Any]],
    occurred_at: datetime,
) -> Optional[TimelineRow]:
    """None = this send is already on the timeline."""
    query, values = q.insert_outbound_query(
        merchant_id,
        thread_id,
        kind,
        author_kind,
        author_user_id,
        message_id,
        provider_message_id,
        body,
        occurred_at,
    )
    return await _one(txn, query, values)


async def timeline(
    merchant_id: str,
    thread_id: str,
    before: Optional[Tuple[datetime, str]],
    limit: int,
) -> List[TimelineRow]:
    query, values = q.timeline_query(merchant_id, thread_id, before, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_timeline(row) for row in rows]


async def rows_after(
    merchant_id: str,
    thread_id: str,
    kinds: List[str],
    after: Optional[datetime],
    limit: int,
) -> List[TimelineRow]:
    query, values = q.rows_after_query(merchant_id, thread_id, kinds, after, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_timeline(row) for row in rows]


async def last_by_author(
    merchant_id: str, thread_id: str, kind: str, author_kind: str
) -> Optional[datetime]:
    query, values = q.last_by_author_query(merchant_id, thread_id, kind, author_kind)
    async with crm_connection() as conn:
        return await conn.fetchval(query, *values)
