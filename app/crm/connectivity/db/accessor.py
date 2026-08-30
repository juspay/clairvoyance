"""Mechanical DB access only — one query builder per function, no decisions.

Every function self-scopes; see queries.py for why no transaction is needed.
"""

from typing import Any, Dict, List, Optional, Tuple

from app.crm.connectivity.db.decoder import (
    decode_binding,
    decode_installation,
    decode_queued_message,
)
from app.crm.connectivity.db.queries import (
    apply_outcome_query,
    binding_by_id_query,
    claim_queued_messages_query,
    insert_message_query,
    installation_by_id_query,
    primary_binding_query,
    requeue_stale_claims_query,
)
from app.crm.connectivity.schemas import (
    ChannelBinding,
    ConnectorInstallation,
    QueuedMessage,
)
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


async def get_binding(
    merchant_id: str, channel: str, binding_id: Optional[str]
) -> Optional[ChannelBinding]:
    """The pipe a message leaves on: the one it named, or the merchant's
    default for that channel."""
    if binding_id:
        query, values = binding_by_id_query(merchant_id, binding_id, channel)
    else:
        query, values = primary_binding_query(merchant_id, channel)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_binding(row) if row is not None else None


async def get_installation(
    merchant_id: str, installation_id: str
) -> Optional[ConnectorInstallation]:
    """The account behind a pipe, merchant-scoped; None if it is not this tenant's."""
    query, values = installation_by_id_query(merchant_id, installation_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_installation(row) if row is not None else None
