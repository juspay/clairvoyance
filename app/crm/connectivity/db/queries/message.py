"""SQL builders for crm_message (T16, the manifest). $1 placeholders only,
never interpolation.

Every builder emits a single statement, which Postgres runs atomically — so
nothing here needs a transaction. The claim and the sweep are deliberately
unscoped by merchant: one global queue, not a loop per tenant.

The vault is deliberately absent: it belongs to app/database, so send.py
reads it through that layer's accessor, never SQL from here.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.crm.connectivity.reasons import (
    REASON_ATTEMPTS_EXHAUSTED,
    REASON_RECLAIMED_STALE_CLAIM,
)
from app.crm.connectivity.status import (
    MESSAGE_DEAD,
    MESSAGE_QUEUED,
    MESSAGE_SENDING,
)

MESSAGE_TABLE = "crm_message"

# Named once so the claim's RETURNING and the decoder cannot drift apart.
# next_attempt_at rides along for the queue-lag log line.
CLAIMED_COLUMNS = """
    id, merchant_id, customer_id, channel, sent_to_address, binding_id,
    source_kind, source_id, purpose_key, template_id, variables,
    dedupe_key, attempt, next_attempt_at
"""


def insert_message_query(
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
) -> Tuple[str, List[Any]]:
    """One queued row, no verdict (gate-mechanics §1). The dedupe unique
    (merchant_id, dedupe_key) absorbs a producer's retry: conflict = no
    row returned, and the caller treats that as already queued."""
    query = f"""
        INSERT INTO {MESSAGE_TABLE}
            (merchant_id, customer_id, channel, sent_to_address, source_kind,
             source_id, purpose_key, template_id, variables, dedupe_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
        ON CONFLICT (merchant_id, dedupe_key) DO NOTHING
        RETURNING id
    """
    return query, [
        merchant_id,
        customer_id,
        channel,
        sent_to_address,
        source_kind,
        source_id,
        purpose_key,
        template_id,
        json.dumps(variables),
        dedupe_key,
    ]


def claim_queued_messages_query(batch_size: int) -> Tuple[str, List[Any]]:
    """Take up to ``batch_size`` queued rows for this worker.

    SKIP LOCKED steps over rows another worker holds instead of waiting, so
    the loop is safe on every pod at once.

    attempt increments HERE, not after the send, so a worker killed mid-send
    still spends one — otherwise a message that reliably crashes workers is
    retried forever.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = $2,
               claimed_at = now(),
               attempt = attempt + 1
         WHERE id IN (
               SELECT id
                 FROM {MESSAGE_TABLE}
                WHERE status = $3
                  AND next_attempt_at <= now()
                ORDER BY next_attempt_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
         )
        RETURNING {CLAIMED_COLUMNS}
    """
    return query, [batch_size, MESSAGE_SENDING, MESSAGE_QUEUED]


def requeue_stale_claims_query(
    stale_minutes: int, max_attempts: int
) -> Tuple[str, List[Any]]:
    """Requeue rows whose worker never came back — unless they are out of
    attempts, in which case they die here.

    Without the requeue, a pod restart leaves rows in-flight forever:
    invisible to the queue, never sent, and nothing raises.

    Without the attempt check, the sweep loops forever on a row whose outcome
    can never be RECORDED (a duplicate provider_message_id makes apply_outcome
    raise every lap) — claimed, really sent, left 'sending', reclaimed, really
    sent again. The claim spends an attempt per lap, so the ceiling that
    bounds retries bounds this too, and dead-by-sweep gets the same reason as
    dead-by-retry: we stopped, the provider didn't.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = CASE WHEN attempt >= $2::int
                             THEN $3::text ELSE $4::text END,
               reason = CASE WHEN attempt >= $2::int
                             THEN $5::text ELSE $6::text END,
               claimed_at = NULL
         WHERE status = $7
           AND claimed_at < now() - make_interval(mins => $1::int)
        RETURNING id, status
    """
    return query, [
        stale_minutes,
        max_attempts,
        MESSAGE_DEAD,
        MESSAGE_QUEUED,
        REASON_ATTEMPTS_EXHAUSTED,
        REASON_RECLAIMED_STALE_CLAIM,
        MESSAGE_SENDING,
    ]


def apply_outcome_query(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    attempt: int,
    retry_after_seconds: Optional[int],
    binding_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Record what happened to a claimed message.

    The WHERE clause pins the write to the claim that did the send. Status
    alone is not enough: the sweep can requeue a stale row and a second
    worker reclaim it, putting it back in 'sending' under a NEW claim, and
    the first worker's late outcome would overwrite it. The claim increments
    ``attempt``, making it a claim-generation token — an expired claim's
    write matches zero rows, the same "their outcome wins" answer.

    COALESCE stops a later failure erasing an id an earlier attempt earned.
    ``retry_after_seconds`` is set only when requeuing; NULL leaves
    next_attempt_at alone, since a terminal outcome has no next attempt.

    ``binding_id`` is which pipe the message LEFT on (T16 col 6): stamped
    once, on the accepted outcome, and never rewritten — migration 060's
    trigger permits exactly that one write. NULL on blocked rows, where no
    pipe was ever used, is the honest answer, so a NULL here leaves the
    column alone.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = $2,
               reason = $3,
               provider_message_id = COALESCE($4, provider_message_id),
               binding_id = COALESCE(binding_id, $9::uuid),
               claimed_at = NULL,
               sent_at = CASE WHEN $5 THEN now() ELSE sent_at END,
               next_attempt_at = CASE
                   WHEN $6::int IS NULL THEN next_attempt_at
                   ELSE now() + make_interval(secs => $6::int)
               END
         WHERE id = $1
           AND status = $8
           AND attempt = $7::int
        RETURNING id
    """
    return query, [
        message_id,
        status,
        reason,
        provider_message_id,
        mark_sent,
        retry_after_seconds,
        attempt,
        MESSAGE_SENDING,
        binding_id,
    ]
