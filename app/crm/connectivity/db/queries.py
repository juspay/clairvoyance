"""SQL builders for crm_message. $1 placeholders only, never interpolation.

Every builder emits a single statement, which Postgres runs atomically — so
nothing here needs a transaction. The claim and the sweep are deliberately
unscoped by merchant: one global queue, not a loop per tenant.

The vault is deliberately absent: it belongs to app/database, so send.py
reads it through that layer's accessor, never SQL from here.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

MESSAGE_TABLE = "crm_message"
INSTALLATION_TABLE = "crm_connector_installation"
BINDING_TABLE = "crm_channel_binding"

INSTALLATION_COLUMNS = """
    id, merchant_id, connector_key, external_account_id, display_label,
    credential_id, status, token_expires_at
"""

BINDING_COLUMNS = """
    id, merchant_id, channel, installation_id, address, capabilities,
    is_primary, status
"""

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
           SET status = 'sending',
               claimed_at = now(),
               attempt = attempt + 1
         WHERE id IN (
               SELECT id
                 FROM {MESSAGE_TABLE}
                WHERE status = 'queued'
                  AND next_attempt_at <= now()
                ORDER BY next_attempt_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
         )
        RETURNING {CLAIMED_COLUMNS}
    """
    return query, [batch_size]


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
                             THEN 'dead' ELSE 'queued' END,
               reason = CASE WHEN attempt >= $2::int
                             THEN 'max_attempts_exhausted'
                             ELSE 'reclaimed_stale_claim' END,
               claimed_at = NULL
         WHERE status = 'sending'
           AND claimed_at < now() - make_interval(mins => $1::int)
        RETURNING id, status
    """
    return query, [stale_minutes, max_attempts]


def apply_outcome_query(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    attempt: int,
    retry_after_seconds: Optional[int],
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
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = $2,
               reason = $3,
               provider_message_id = COALESCE($4, provider_message_id),
               claimed_at = NULL,
               sent_at = CASE WHEN $5 THEN now() ELSE sent_at END,
               next_attempt_at = CASE
                   WHEN $6::int IS NULL THEN next_attempt_at
                   ELSE now() + make_interval(secs => $6::int)
               END
         WHERE id = $1
           AND status = 'sending'
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
    ]


def primary_binding_query(merchant_id: str, channel: str) -> Tuple[str, List[Any]]:
    """The merchant's default pipe on a channel.

    Only 'active': a paused or retired pipe must produce NO route rather than
    fall through to another number — sending from an unexpected address is
    worse than not sending. is_primary is partial-unique per (merchant,
    channel), so this never has to choose between two rows.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND channel = $2
           AND is_primary
           AND status = 'active'
    """
    return query, [merchant_id, channel]


def binding_by_id_query(
    merchant_id: str, binding_id: str, channel: str
) -> Tuple[str, List[Any]]:
    """One named pipe, scoped to its merchant AND channel in the WHERE clause
    rather than checked afterwards.

    The channel filter is not redundant with the id: binding_id is a bare
    uuid with no FK, so a row could name a binding of a DIFFERENT channel,
    whose address would then reach this channel's adapter as if it were its
    own kind of endpoint. A mismatch must be 'no route'.
    """
    query = f"""
        SELECT {BINDING_COLUMNS}
          FROM {BINDING_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND channel = $3
           AND status = 'active'
    """
    return query, [merchant_id, binding_id, channel]


def installation_by_id_query(
    merchant_id: str, installation_id: str
) -> Tuple[str, List[Any]]:
    """The account behind a pipe. Status is NOT filtered here — the caller
    decides what an unhealthy installation means, and a route that silently
    disappeared would be reported as 'no connection' when the truth is
    'connection revoked'."""
    query = f"""
        SELECT {INSTALLATION_COLUMNS}
          FROM {INSTALLATION_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
    """
    return query, [merchant_id, installation_id]
