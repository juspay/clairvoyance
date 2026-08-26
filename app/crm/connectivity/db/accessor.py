"""Mechanical DB access only — one query builder per function, no decisions.

Every function self-scopes; see queries.py for why no transaction is needed.
"""

from typing import List, Optional

from app.crm.connectivity.db.decoder import decode_queued_message
from app.crm.connectivity.db.queries import (
    apply_outcome_query,
    claim_queued_messages_query,
    requeue_stale_claims_query,
)
from app.crm.connectivity.schemas import QueuedMessage
from app.crm.shared.db import crm_connection


async def claim_queued_messages(batch_size: int) -> List[QueuedMessage]:
    query, values = claim_queued_messages_query(batch_size)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_queued_message(row) for row in rows]


async def requeue_stale_claims(stale_minutes: int) -> List[str]:
    """Returns the ids released, not just a count — a reclaimed message is the
    first thing anyone investigating a possible double send asks about."""
    query, values = requeue_stale_claims_query(stale_minutes)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [str(row["id"]) for row in rows]


async def apply_outcome(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    retry_after_seconds: Optional[int] = None,
) -> bool:
    """False means the row was no longer ours — another worker reclaimed it."""
    query, values = apply_outcome_query(
        message_id, status, reason, provider_message_id, mark_sent, retry_after_seconds
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None
