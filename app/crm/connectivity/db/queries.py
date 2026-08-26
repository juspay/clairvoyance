"""SQL builders for crm_message. $1 placeholders only, never interpolation.

Every builder emits a single statement, which Postgres runs atomically — so
nothing here needs a transaction. The claim and the sweep are deliberately
unscoped by merchant: one global queue, not a loop per tenant.
"""

from typing import Any, List, Optional, Tuple

MESSAGE_TABLE = "crm_message"

# Named once so the claim's RETURNING and the decoder cannot drift apart.
# next_attempt_at rides along for the queue-lag log line: how long the oldest
# row sat past due is the alert that rises whatever the cause.
CLAIMED_COLUMNS = """
    id, merchant_id, customer_id, channel, sent_to_address, binding_id,
    source_kind, source_id, purpose_key, template_id, variables,
    dedupe_key, attempt, next_attempt_at
"""


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


def requeue_stale_claims_query(stale_minutes: int) -> Tuple[str, List[Any]]:
    """Requeue rows whose worker never came back.

    Without this a pod restart leaves rows marked in-flight forever: invisible
    to the queue, never sent, and nothing raises.
    """
    query = f"""
        UPDATE {MESSAGE_TABLE}
           SET status = 'queued',
               claimed_at = NULL,
               reason = 'reclaimed_stale_claim'
         WHERE status = 'sending'
           AND claimed_at < now() - make_interval(mins => $1::int)
        RETURNING id
    """
    return query, [stale_minutes]


def apply_outcome_query(
    message_id: str,
    status: str,
    reason: Optional[str],
    provider_message_id: Optional[str],
    mark_sent: bool,
    retry_after_seconds: Optional[int],
) -> Tuple[str, List[Any]]:
    """Record what happened to a claimed message.

    ``AND status = 'sending'`` keeps a slow send from overwriting a row the
    sweep already reassigned; zero rows is the right answer there. COALESCE
    stops a later failure erasing an id an earlier attempt earned.
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
        RETURNING id
    """
    return query, [
        message_id,
        status,
        reason,
        provider_message_id,
        mark_sent,
        retry_after_seconds,
    ]
