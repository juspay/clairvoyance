"""Mechanical access to crm_conversation. Writes inside an atom take its
``txn``; the rest self-scope (crm_connection)."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.conversations.db.decoders.rows import decode_thread
from app.crm.conversations.db.queries import thread as q
from app.crm.conversations.schemas import Thread
from app.crm.shared.db import DbTxn, crm_connection


async def _one(txn: Optional[DbTxn], query: str, values: List[Any]) -> Optional[Thread]:
    if txn is not None:
        row = await txn.fetchrow(query, *values)
    else:
        async with crm_connection() as conn:
            row = await conn.fetchrow(query, *values)
    return decode_thread(row) if row is not None else None


async def ensure_thread(
    txn: DbTxn,
    merchant_id: str,
    channel: str,
    contact_key: str,
    customer_id: Optional[str],
    address: Optional[str],
    binding_id: Optional[str],
) -> Thread:
    query, values = q.ensure_thread_query(
        merchant_id, channel, contact_key, customer_id, address, binding_id
    )
    thread = await _one(txn, query, values)
    if thread is None:
        raise RuntimeError("crm_conversation upsert returned no row")
    return thread


async def get_thread(
    merchant_id: str,
    thread_id: str,
    txn: Optional[DbTxn] = None,
    for_update: bool = False,
) -> Optional[Thread]:
    query, values = q.thread_query(merchant_id, thread_id, for_update)
    return await _one(txn, query, values)


async def threads_by_ids(merchant_id: str, thread_ids: List[str]) -> List[Thread]:
    query, values = q.threads_by_ids_query(merchant_id, thread_ids)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_thread(row) for row in rows]


async def thread_for_contact(
    merchant_id: str, channel: str, contact_key: str, txn: Optional[DbTxn] = None
) -> Optional[Thread]:
    query, values = q.thread_for_contact_query(merchant_id, channel, contact_key)
    return await _one(txn, query, values)


async def record_inbound(
    txn: DbTxn,
    merchant_id: str,
    thread_id: str,
    occurred_at: datetime,
    preview: Optional[str],
    reopen: bool,
    bot_template_id: Optional[str],
    bot_session_id: Optional[str],
    written_at: datetime,
) -> Optional[Thread]:
    query, values = q.record_inbound_query(
        merchant_id,
        thread_id,
        occurred_at,
        preview,
        reopen,
        bot_template_id,
        bot_session_id,
        written_at,
    )
    return await _one(txn, query, values)


async def touch_outbound(
    merchant_id: str,
    thread_id: str,
    occurred_at: datetime,
    preview: Optional[str],
    txn: Optional[DbTxn] = None,
) -> Optional[Thread]:
    query, values = q.touch_outbound_query(merchant_id, thread_id, occurred_at, preview)
    return await _one(txn, query, values)


async def take_over(
    txn: DbTxn, merchant_id: str, thread_id: str, user_id: str, trail: Dict[str, Any]
) -> Optional[Thread]:
    query, values = q.take_over_query(merchant_id, thread_id, user_id, trail)
    return await _one(txn, query, values)


async def assign(
    txn: DbTxn, merchant_id: str, thread_id: str, user_id: str, trail: Dict[str, Any]
) -> Optional[Thread]:
    query, values = q.assign_query(merchant_id, thread_id, user_id, trail)
    return await _one(txn, query, values)


async def hand_back(
    txn: DbTxn,
    merchant_id: str,
    thread_id: str,
    user_id: str,
    agent_id: Optional[str],
    trail: Dict[str, Any],
) -> Optional[Thread]:
    query, values = q.hand_back_query(merchant_id, thread_id, user_id, agent_id, trail)
    return await _one(txn, query, values)


async def resolve(
    txn: DbTxn,
    merchant_id: str,
    thread_id: str,
    last_inbound_at: Optional[datetime] = None,
) -> Optional[Thread]:
    query, values = q.resolve_query(merchant_id, thread_id, last_inbound_at)
    return await _one(txn, query, values)


async def resolve_binding(txn: DbTxn, merchant_id: str, binding_id: str) -> List[str]:
    query, values = q.resolve_binding_query(merchant_id, binding_id)
    rows = await txn.fetch(query, *values)
    return [str(row["id"]) for row in rows]


async def mark_read(merchant_id: str, thread_id: str) -> Optional[Thread]:
    query, values = q.mark_read_query(merchant_id, thread_id)
    return await _one(None, query, values)


async def list_threads(
    merchant_id: str,
    view: str,
    user_id: str,
    channel: Optional[str],
    search: Optional[str],
    before: Optional[Tuple[datetime, str]],
    limit: int,
) -> List[Thread]:
    query, values = q.list_threads_query(
        merchant_id, view, user_id, channel, search, before, limit
    )
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_thread(row) for row in rows]


async def view_counts(
    merchant_id: str, user_id: str, channel: Optional[str]
) -> Dict[str, int]:
    query, values = q.view_counts_query(merchant_id, user_id, channel)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return {view: int(row[view] or 0) for view in q.COUNTED_VIEWS} if row else {}


async def set_bot_session(
    merchant_id: str, thread_id: str, session_id: str
) -> Optional[Thread]:
    query, values = q.set_bot_session_query(merchant_id, thread_id, session_id)
    return await _one(None, query, values)


async def mark_bot_cursor(
    merchant_id: str, thread_id: str, answered_upto: datetime
) -> Optional[Thread]:
    query, values = q.mark_bot_cursor_query(merchant_id, thread_id, answered_upto)
    return await _one(None, query, values)


async def closing_candidates(
    channel: str,
    older_than_seconds: int,
    after: Optional[Tuple[datetime, str]],
    limit: int,
) -> List[Thread]:
    query, values = q.closing_candidates_query(
        channel, older_than_seconds, after, limit
    )
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_thread(row) for row in rows]


async def delete_resolved(older_than_days: int, limit: int) -> int:
    query, values = q.delete_resolved_query(older_than_days, limit)
    async with crm_connection() as conn:
        return int(await conn.fetchval(query, *values) or 0)
