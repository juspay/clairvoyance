"""Mechanical DB access for crm_message_receipt_pending. Functions that run
inside the drain's atom take its ``txn``; the rest self-scope."""

from typing import List, Optional, Tuple

from app.crm.connectivity.db.queries.receipt import (
    expire_parked_query,
    lock_parked_query,
    park_receipt_query,
    parked_messages_query,
    unpark_query,
)
from app.crm.connectivity.schemas.message import ProviderReceipt
from app.crm.shared.db import DbTxn, crm_connection


async def park_receipt(merchant_id: str, receipt: ProviderReceipt) -> None:
    query, values = park_receipt_query(
        merchant_id,
        receipt.provider_message_id,
        receipt.state,
        receipt.occurred_at,
        receipt.error_code,
        receipt.pricing_category,
    )
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def lock_parked(
    txn: DbTxn, merchant_id: str, provider_message_id: str
) -> List[ProviderReceipt]:
    query, values = lock_parked_query(merchant_id, provider_message_id)
    rows = await txn.fetch(query, *values)
    return [
        ProviderReceipt(
            provider_message_id=provider_message_id,
            state=row["state"],
            occurred_at=row["occurred_at"],
            error_code=row["error_code"],
            pricing_category=row["pricing_category"],
        )
        for row in rows
    ]


async def unpark(
    merchant_id: str, provider_message_id: str, state: str, txn: Optional[DbTxn] = None
) -> None:
    query, values = unpark_query(merchant_id, provider_message_id, state)
    if txn is not None:
        await txn.execute(query, *values)
        return
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def parked_messages(limit: int) -> List[Tuple[str, str]]:
    query, values = parked_messages_query(limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [(row["merchant_id"], row["provider_message_id"]) for row in rows]


async def expire_parked(grace_seconds: int) -> int:
    query, values = expire_parked_query(grace_seconds)
    async with crm_connection() as conn:
        return int(await conn.fetchval(query, *values) or 0)
