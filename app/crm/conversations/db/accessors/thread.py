"""Mechanical access to crm_conversation. Writes inside an atom take its
``txn``; the rest self-scope (crm_connection)."""

from datetime import datetime
from typing import Any, List, Optional

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


async def resolve_binding(txn: DbTxn, merchant_id: str, binding_id: str) -> List[str]:
    query, values = q.resolve_binding_query(merchant_id, binding_id)
    rows = await txn.fetch(query, *values)
    return [str(row["id"]) for row in rows]
