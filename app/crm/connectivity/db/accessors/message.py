"""Mechanical DB access for crm_message — one query builder per function, no
decisions.

Every function self-scopes; see queries/message.py for why no transaction is
needed.
"""

from typing import Any, Dict, List, Optional, Tuple

from app.crm.connectivity.db.decoders.message import decode_queued_message
from app.crm.connectivity.db.queries.message import (
    apply_outcome_query,
    claim_queued_messages_query,
    insert_message_query,
    requeue_stale_claims_query,
)
from app.crm.connectivity.schemas.message import QueuedMessage
from app.crm.shared.db import crm_connection


async def insert_message(
    merchant_id: str,
    customer_id: str,
    channel: str,
    sent_to_address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    template_id: Optional[str],
    variables: Dict[str, Any],
    dedupe_key: str,
) -> Optional[str]:
    """None = the dedupe unique absorbed it (a row already names this send)."""
    query, values = insert_message_query(
        merchant_id,
        customer_id,
        channel,
        sent_to_address,
        source_kind,
        source_id,
        purpose_key,
        template_id,
        variables,
        dedupe_key,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return str(row["id"]) if row else None


async def claim_queued_messages(batch_size: int) -> List[QueuedMessage]:
    """Take up to ``batch_size`` due rows for this worker; the claim spends an attempt."""
    query, values = claim_queued_messages_query(batch_size)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_queued_message(row) for row in rows]


async def requeue_stale_claims(
    stale_minutes: int, max_attempts: int
) -> Tuple[List[str], List[str]]:
    """(requeued ids, ids dead on reclaim) — ids, not counts, because a
    reclaimed message is the first thing anyone investigating a possible
    double send asks about, and a dead-on-reclaim one is a row that was
    really attempted max times without a recorded answer."""
    query, values = requeue_stale_claims_query(stale_minutes, max_attempts)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    requeued = [str(row["id"]) for row in rows if row["status"] == "queued"]
    dead = [str(row["id"]) for row in rows if row["status"] != "queued"]
    return requeued, dead


async def apply_outcome(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    attempt: int,
    retry_after_seconds: Optional[int] = None,
) -> bool:
    """False means the row was no longer ours — another worker reclaimed it
    (``attempt`` is the claim's generation; a stale claim's write misses)."""
    query, values = apply_outcome_query(
        message_id,
        status,
        reason,
        provider_message_id,
        mark_sent,
        attempt,
        retry_after_seconds,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None
