"""Mechanical access to crm_conversation_message."""

from datetime import datetime
from typing import Any, Dict, List, Optional

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


async def thread_for_send(
    merchant_id: str, provider_message_id: str, message_id: Optional[str]
) -> Optional[str]:
    query, values = q.thread_for_send_query(
        merchant_id, provider_message_id, message_id
    )
    async with crm_connection() as conn:
        found = await conn.fetchval(query, *values)
    return str(found) if found is not None else None
