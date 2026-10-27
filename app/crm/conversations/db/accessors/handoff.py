"""Mechanical access to crm_handoff."""

from typing import Any, List, Optional

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


async def open_for_thread(
    merchant_id: str, thread_id: str, txn: Optional[DbTxn] = None
) -> Optional[Handoff]:
    query, values = q.open_for_thread_query(merchant_id, thread_id)
    return await _one(txn, query, values)


async def close_for_threads(
    txn: DbTxn, merchant_id: str, thread_ids: List[str], outcome: str
) -> int:
    if not thread_ids:
        return 0
    query, values = q.close_for_threads_query(merchant_id, thread_ids, outcome)
    rows = await txn.fetch(query, *values)
    return len(rows)
