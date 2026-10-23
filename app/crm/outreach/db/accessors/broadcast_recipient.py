"""Mechanical DB access for crm_broadcast_recipient (T18) — one table, one
file. Two shapes, by signature: a ``conn`` parameter runs inside the
caller's atom, after the parent broadcast was locked in the same
transaction; no parameter self-scopes one read through the parent. No
decisions.
"""

from typing import Dict, List, Optional, Tuple

import asyncpg

from app.crm.outreach.db.queries.broadcast_recipient import (
    cancel_queued_recipients_query,
    claim_page_query,
    insert_recipients_query,
    last_frozen_customer_query,
    reap_stale_claims_query,
    recipient_counts_query,
    release_claims_query,
    settle_recipient_query,
)
from app.crm.shared.db import crm_connection, crm_replica_read


async def insert_recipients(
    conn: asyncpg.Connection, broadcast_id: str, customer_ids: List[str]
) -> int:
    """How many rows were really added (the rest were already queued)."""
    query, values = insert_recipients_query(broadcast_id, customer_ids)
    return await conn.fetchval(query, *values) or 0


async def cancel_queued_recipients(conn: asyncpg.Connection, broadcast_id: str) -> int:
    query, values = cancel_queued_recipients_query(broadcast_id)
    return await conn.fetchval(query, *values) or 0


async def last_frozen_customer(merchant_id: str, broadcast_id: str) -> Optional[str]:
    """The largest customer id already frozen (any status), or None."""
    query, values = last_frozen_customer_query(merchant_id, broadcast_id)
    async with crm_connection() as conn:
        customer_id = await conn.fetchval(query, *values)
    return str(customer_id) if customer_id else None


async def claim_page(broadcast_id: str, claimed_by: str, limit: int) -> List[str]:
    """The claimed customer ids, in key order."""
    query, values = claim_page_query(broadcast_id, claimed_by, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return sorted(str(row["customer_id"]) for row in rows)


async def settle_recipient(
    conn: asyncpg.Connection,
    broadcast_id: str,
    customer_id: str,
    status: str,
    skip_reason: Optional[str],
) -> bool:
    """Inside the per-person atom. False: the row was no longer claimed."""
    query, values = settle_recipient_query(
        broadcast_id, customer_id, status, skip_reason
    )
    return await conn.fetchval(query, *values) is not None


async def release_claims(broadcast_id: str, customer_ids: List[str]) -> None:
    query, values = release_claims_query(broadcast_id, customer_ids)
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def reap_stale_claims(lease_minutes: int) -> int:
    """How many rows went back to the queue."""
    query, values = reap_stale_claims_query(lease_minutes)
    async with crm_connection() as conn:
        return len(await conn.fetch(query, *values))


async def recipient_counts(
    merchant_id: str, broadcast_id: str
) -> Dict[Tuple[str, Optional[str]], int]:
    """{(status, skip_reason): count} for one broadcast's frozen list. The
    console's funnel: the replica."""
    query, values = recipient_counts_query(merchant_id, broadcast_id)
    rows = await crm_replica_read(query, values)
    return {(row["status"], row["skip_reason"]): int(row["n"]) for row in rows}
