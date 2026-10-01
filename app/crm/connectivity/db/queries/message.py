"""SQL builders for crm_message (T16, the manifest). $1 placeholders only,
never interpolation.

Every builder emits a single statement, which Postgres runs atomically — so
nothing here needs a transaction. The claim and the sweep are deliberately
unscoped by merchant: one global queue, not a loop per tenant.

The vault is deliberately absent: it belongs to app/database, so send.py
reads it through that layer's accessor, never SQL from here.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.connectivity.reasons import (
    REASON_ATTEMPTS_EXHAUSTED,
    REASON_RECLAIMED_STALE_CLAIM,
    REASON_SESSION_ABANDONED,
)
from app.crm.connectivity.status import (
    MESSAGE_ACCEPTED,
    MESSAGE_DEAD,
    MESSAGE_DELIVERED,
    MESSAGE_FAILED,
    MESSAGE_QUEUED,
    MESSAGE_READ,
    MESSAGE_SENDING,
    MESSAGE_SENT,
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
    binding_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """One queued row, no verdict (gate-mechanics §1). The dedupe unique
    (merchant_id, dedupe_key) absorbs a producer's retry: conflict = no
    row returned, and the caller treats that as already queued.
    ``binding_id`` names the binding to send from; NULL is the primary."""
    query = f"""
        INSERT INTO {MESSAGE_TABLE}
            (merchant_id, customer_id, channel, sent_to_address, source_kind,
             source_id, purpose_key, template_id, variables, dedupe_key,
             binding_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11::uuid)
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
        binding_id,
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


def send_behind_provider_id_query(
    merchant_id: str, provider_message_id: str
) -> Tuple[str, List[Any]]:
    """Who caused the message this provider id names (contracts.send_behind).

    ONE point read on ``crm_message_provider_id_uq`` — migration 056's
    partial UNIQUE on provider_message_id alone, whose own canon note reads
    "how an inbound receipt finds this row" (T16 col 14). A reply carries
    exactly that id for the message it answers, so the same index answers
    "whose send is she replying to" with nothing stored anywhere else.

    merchant_id leads the WHERE although the unique does not need it: the id
    is the PROVIDER's and arrives on a letter, so a payload naming another
    tenant's message must find nothing rather than something (the tenancy
    law, the same posture as template_by_provider_id_query).
    """
    query = f"""
        SELECT source_kind, source_id, dedupe_key
          FROM {MESSAGE_TABLE}
         WHERE merchant_id = $1
           AND provider_message_id = $2
    """
    return query, [merchant_id, provider_message_id]


# ---------------------------------------------------------------------------
# Session sends — a free-form reply's row is written IN FLIGHT and closed by
# the same call; the dispatcher never claims it.
# ---------------------------------------------------------------------------


def insert_session_message_query(
    merchant_id: str,
    customer_id: str,
    channel: str,
    sent_to_address: str,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    dedupe_key: str,
) -> Tuple[str, List[Any]]:
    """One free-form reply, written already in flight ('sending', attempt 1).

    ``claimed_at`` stays NULL, and that is the marker, not an accident: the
    dispatcher's claim always stamps claimed_at, so its stale sweep
    (``claimed_at < now() - …``) can never pick this row up and re-send a
    reply whose words it does not have. An abandoned session row is closed
    by its own sweep below instead.

    No template, no variables (canon T16 col 11: free-text sends carry
    none — the words ride the message.queued letter). The dedupe unique
    absorbs a caller's retry of the same reply: conflict = no row returned.
    """
    query = f"""
        INSERT INTO {MESSAGE_TABLE}
            (merchant_id, customer_id, channel, sent_to_address, source_kind,
             source_id, purpose_key, template_id, variables, dedupe_key,
             status, attempt)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, '{{}}'::jsonb, $8, $9, 1)
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
        dedupe_key,
        MESSAGE_SENDING,
    ]


def message_state_by_dedupe_query(
    merchant_id: str, dedupe_key: str
) -> Tuple[str, List[Any]]:
    """The row a dedupe key already names — one point read on the
    (merchant_id, dedupe_key) unique."""
    query = f"""
        SELECT id, status, reason, provider_message_id
          FROM {MESSAGE_TABLE}
         WHERE merchant_id = $1
           AND dedupe_key = $2
    """
    return query, [merchant_id, dedupe_key]


def abandon_stale_session_sends_query(stale_minutes: int) -> Tuple[str, List[Any]]:
    """Close session rows whose sender died between the insert and the
    outcome — 'sending' with no claim (the session marker) past the stale
    window.

    Dead, never requeued: the words are not on the row, so there is nothing
    to send again, and the honest answer is "we do not know whether it went
    out". The candidate set is small by construction — only rows still
    'sending' — so it runs beside the dispatcher's own stale sweep each pass.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = $2,
               reason = $3
         WHERE status = $4
           AND claimed_at IS NULL
           AND created_at < now() - make_interval(mins => $1::int)
        RETURNING id
    """
    return query, [
        stale_minutes,
        MESSAGE_DEAD,
        REASON_SESSION_ABANDONED,
        MESSAGE_SENDING,
    ]


# ---------------------------------------------------------------------------
# Receipts — what became of a message we sent (message.status letters)
# ---------------------------------------------------------------------------


def _rank(expr: str) -> str:
    """A status's place on the delivery ladder; 0 for anything off it.
    The words are bound ($7..$10), never spelled."""
    return (
        f"CASE {expr} WHEN $7::text THEN 1 WHEN $8::text THEN 2 "
        f"WHEN $9::text THEN 3 WHEN $10::text THEN 4 ELSE 0 END"
    )


def apply_receipt_query(
    merchant_id: str,
    provider_message_id: str,
    state: str,
    occurred_at: Optional[datetime],
    error_code: Optional[str],
    pricing_category: Optional[str],
) -> Tuple[str, List[Any]]:
    """Move one row along accepted -> sent -> delivered -> read, never back.

    Receipts arrive out of order and more than once, so the STATUS only ever
    advances (a late 'delivered' after 'read' changes nothing), while each
    TIMESTAMP is a fact recorded the first time it is seen, whatever order
    it came in. 'failed' is taken only from accepted or sent: a message the
    customer already has cannot fail after the fact. The provider's code
    rides into ``reason`` on that transition alone (canon T16 col 13).

    'failed' and 'dead' are TERMINAL for the status: a 'sent' receipt that
    arrives after the 'failed' one (Meta sends sent, then failed 131026; the
    spine may hand them over the other way round) must not move a failed row
    back up the ladder and hide the failure. Its timestamp is still
    recorded — the facts stay first-seen whatever the status says.

    Every SET expression reads the OLD row — Postgres evaluates them before
    writing — so the status CASEs compare against where the row was.
    ``merchant_id`` leads the WHERE: the id is the provider's and arrives on
    a letter, so another tenant's id must match nothing.
    """
    moved = (
        f"status NOT IN ($11::text, $12::text) "
        f"AND {_rank('$3::text')} > {_rank('status')}"
    )
    fails = "$3::text = $11::text AND status IN ($7::text, $8::text)"
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = CASE
                   WHEN {fails} THEN $11::text
                   WHEN $3::text <> $11::text AND {moved} THEN $3::text
                   ELSE status
               END,
               reason = CASE WHEN {fails} THEN COALESCE($5, reason)
                             ELSE reason END,
               sent_at = CASE WHEN $3::text <> $11::text
                              THEN COALESCE(sent_at, $4::timestamptz, now())
                              ELSE sent_at END,
               delivered_at = CASE WHEN $3::text IN ($9::text, $10::text)
                                   THEN COALESCE(delivered_at, $4::timestamptz, now())
                                   ELSE delivered_at END,
               read_at = CASE WHEN $3::text = $10::text
                              THEN COALESCE(read_at, $4::timestamptz, now())
                              ELSE read_at END,
               pricing_category = COALESCE($6, pricing_category)
         WHERE merchant_id = $1
           AND provider_message_id = $2
        RETURNING id, status
    """
    return query, [
        merchant_id,
        provider_message_id,
        state,
        occurred_at,
        error_code,
        pricing_category,
        MESSAGE_ACCEPTED,
        MESSAGE_SENT,
        MESSAGE_DELIVERED,
        MESSAGE_READ,
        MESSAGE_FAILED,
        MESSAGE_DEAD,
    ]


def message_ticks_query(
    merchant_id: str, message_ids: List[str]
) -> Tuple[str, List[Any]]:
    """What became of these rows — the timeline joins its ticks from here
    at read, never copying them."""
    query = f"""
        SELECT id, status, reason, sent_at, delivered_at, read_at
          FROM {MESSAGE_TABLE}
         WHERE merchant_id = $1
           AND id = ANY($2::uuid[])
    """
    return query, [merchant_id, message_ids]


def message_id_by_provider_query(
    merchant_id: str, provider_message_id: str
) -> Tuple[str, List[Any]]:
    """Our row for the provider's id — what a receipt is about."""
    query = f"""
        SELECT id
          FROM {MESSAGE_TABLE}
         WHERE merchant_id = $1
           AND provider_message_id = $2
         LIMIT 1
    """
    return query, [merchant_id, provider_message_id]
