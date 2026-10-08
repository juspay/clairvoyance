"""Mechanical DB access for crm_message — one query builder per function, no
decisions.

Every function self-scopes; see queries/message.py for why no transaction is
needed.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.connectivity.db.decoders.message import (
    decode_message_state,
    decode_queued_message,
    decode_send_behind,
)
from app.crm.connectivity.db.queries.message import (
    abandon_stale_session_sends_query,
    apply_outcome_query,
    apply_receipt_query,
    claim_queued_messages_query,
    insert_message_query,
    insert_session_message_query,
    message_state_by_dedupe_query,
    requeue_stale_claims_query,
    send_behind_provider_id_query,
)
from app.crm.connectivity.schemas.message import (
    MessageState,
    QueuedMessage,
    SendBehind,
)
from app.crm.connectivity.status import MESSAGE_QUEUED
from app.crm.shared.db import DbTxn, crm_connection


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
    requeued = [str(row["id"]) for row in rows if row["status"] == MESSAGE_QUEUED]
    dead = [str(row["id"]) for row in rows if row["status"] != MESSAGE_QUEUED]
    return requeued, dead


async def apply_outcome(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    attempt: int,
    retry_after_seconds: Optional[int] = None,
    binding_id: Optional[str] = None,
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
        binding_id,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def send_behind(
    merchant_id: str, provider_message_id: str
) -> Optional[SendBehind]:
    """Who caused the message this provider id names, or None when no row
    of ours carries it — an id from a message this system never sent."""
    query, values = send_behind_provider_id_query(merchant_id, provider_message_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_send_behind(row) if row is not None else None


async def insert_session_message(
    merchant_id: str,
    customer_id: str,
    channel: str,
    sent_to_address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    dedupe_key: str,
) -> Optional[str]:
    """A free-form reply's row, already in flight. None = the dedupe unique
    absorbed it (this reply was already written)."""
    query, values = insert_session_message_query(
        merchant_id,
        customer_id,
        channel,
        sent_to_address,
        source_kind,
        source_id,
        purpose_key,
        dedupe_key,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return str(row["id"]) if row else None


async def message_state_by_dedupe(
    merchant_id: str, dedupe_key: str
) -> Optional[MessageState]:
    """The row a dedupe key already names, as it stands now."""
    query, values = message_state_by_dedupe_query(merchant_id, dedupe_key)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_message_state(row) if row is not None else None


async def abandon_stale_session_sends(stale_minutes: int) -> List[str]:
    """Ids of session rows closed dead because their sender never returned."""
    query, values = abandon_stale_session_sends_query(stale_minutes)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [str(row["id"]) for row in rows]


async def apply_receipt(
    merchant_id: str,
    provider_message_id: str,
    state: str,
    occurred_at: Optional[datetime],
    error_code: Optional[str],
    pricing_category: Optional[str],
    txn: Optional[DbTxn] = None,
) -> Optional[str]:
    """The row's status after the receipt, or None when no row of this
    merchant carries the provider's id. ``txn`` when the caller's atom owns
    the fate (the parked-receipt drain)."""
    query, values = apply_receipt_query(
        merchant_id,
        provider_message_id,
        state,
        occurred_at,
        error_code,
        pricing_category,
    )
    if txn is not None:
        row = await txn.fetchrow(query, *values)
    else:
        async with crm_connection() as conn:
            row = await conn.fetchrow(query, *values)
    return row["status"] if row is not None else None
