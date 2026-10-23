"""Mechanical access to crm_handoff."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.conversations.db.decoders.rows import decode_handoff
from app.crm.conversations.db.queries import handoff as q
from app.crm.conversations.schemas import Handoff
from app.crm.shared.db import DbTxn, crm_connection


async def _one(
    txn: Optional[DbTxn], query: str, values: List[Any]
) -> Optional[Handoff]:
    if txn is not None:
        row = await txn.fetchrow(query, *values)
    else:
        async with crm_connection() as conn:
            row = await conn.fetchrow(query, *values)
    return decode_handoff(row) if row is not None else None


async def open_handoff(
    txn: DbTxn,
    merchant_id: str,
    thread_id: str,
    chat_session_id: str,
    reason: Optional[str],
    summary: Optional[str],
    priority: str,
) -> Optional[Handoff]:
    """None = the thread already has one open."""
    query, values = q.open_handoff_query(
        merchant_id, thread_id, chat_session_id, reason, summary, priority
    )
    return await _one(txn, query, values)


async def open_for_thread(
    merchant_id: str, thread_id: str, txn: Optional[DbTxn] = None
) -> Optional[Handoff]:
    query, values = q.open_for_thread_query(merchant_id, thread_id)
    return await _one(txn, query, values)


async def open_for_threads(
    merchant_id: str, thread_ids: List[str]
) -> Dict[str, Handoff]:
    if not thread_ids:
        return {}
    query, values = q.open_for_threads_query(merchant_id, thread_ids)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    handoffs = [decode_handoff(row) for row in rows]
    return {h.conversation_id: h for h in handoffs}


async def claim(
    txn: DbTxn, merchant_id: str, thread_id: str, user_id: str
) -> Optional[Handoff]:
    query, values = q.claim_query(merchant_id, thread_id, user_id)
    return await _one(txn, query, values)


async def close(
    txn: Optional[DbTxn],
    merchant_id: str,
    thread_id: str,
    outcome: str,
    closed_by: Optional[str],
) -> Optional[Handoff]:
    """None = nothing was open (already closed: closing is once)."""
    query, values = q.close_query(merchant_id, thread_id, outcome, closed_by)
    return await _one(txn, query, values)


async def lapse(
    merchant_id: str, handoff_id: str, outcome: str, claim_sla_minutes: int
) -> Optional[Handoff]:
    """None = claimed, closed or not yet due by the time the write landed."""
    query, values = q.lapse_query(merchant_id, handoff_id, outcome, claim_sla_minutes)
    return await _one(None, query, values)


async def close_for_threads(
    txn: DbTxn, merchant_id: str, thread_ids: List[str], outcome: str
) -> int:
    if not thread_ids:
        return 0
    query, values = q.close_for_threads_query(merchant_id, thread_ids, outcome)
    rows = await txn.fetch(query, *values)
    return len(rows)


async def unclaimed_older_than(
    seconds: int, after: Optional[Tuple[datetime, str]], limit: int
) -> List[Handoff]:
    query, values = q.unclaimed_older_than_query(seconds, after, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_handoff(row) for row in rows]
