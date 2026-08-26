"""Outbound dispatch — claims queued messages and hands each to send().

Producers only queue; this decides. That split exists so the permission
check happens at send time rather than compose time, and so a customer who
opts out in between is still honoured.

The loop itself is not here: claim_sends()/dispatch_send() plug into the
shared drain-loop scaffold (app/crm/shared/worker.py) as the "dispatcher"
role in app/crm/worker_main.py. This file owns only what a row means.

TODO: no permission check yet — may_contact() does not exist. It belongs in
_dispatch_one before send(), and a refusal must write a 'blocked' row rather
than retry. This phase ships demo sends without it by explicit scope decision
(see the PR thread with Swaroop); the gate structure lands with B5.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from app.core.config.static import (
    CRM_DISPATCH_MAX_ATTEMPTS,
    CRM_DISPATCH_RETRY_BASE_SECONDS,
    CRM_DISPATCH_STALE_MINUTES,
)
from app.core.logger import logger
from app.core.logger.context import set_log_context
from app.crm.connectivity.db import accessor
from app.crm.connectivity.schemas import QueuedMessage, SendOutcome
from app.crm.connectivity.send import send

LOG_COMPONENT = "crm.connectivity.dispatch"

# Batch size is operator-configurable and the stale sweep is unbounded, so
# never interpolate a whole id list into a log line — one bad tick would
# emit a multi-megabyte record.
LOG_ID_SAMPLE = 100

# Ceiling on the exponential backoff. Not a config knob: at the default three
# attempts it is never reached, and it exists only so that raising
# CRM_DISPATCH_MAX_ATTEMPTS cannot silently park a message for hours.
RETRY_MAX_SECONDS = 300

# 'failed' is the provider refusing for good; 'dead' is us running out of
# retries. A merchant asking why nothing arrived needs to know which.
STATUS_QUEUED = "queued"
STATUS_ACCEPTED = "accepted"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"

REASON_SEND_ERROR = "send_error"
REASON_ATTEMPTS_EXHAUSTED = "max_attempts_exhausted"
REASON_PROVIDER_REJECTED = "provider_rejected"


def sample_ids(ids: Sequence[str], limit: int = LOG_ID_SAMPLE) -> str:
    """Render ids for a log line, bounded. The full set is always recoverable
    from the rows themselves; an unbounded log line is not worth it."""
    if len(ids) <= limit:
        return ", ".join(ids)
    return f"{', '.join(ids[:limit])} … +{len(ids) - limit} more"


def backoff_seconds(attempt: int, base: int, cap: int) -> int:
    """How long a retry waits. Exponential from ``base``, capped.

    ``attempt`` is already incremented by the claim, so the first retry uses
    the base delay rather than zero. Retrying a rate-limited send immediately
    is the one response guaranteed to make it fail again.
    """
    return min(base * 2 ** max(attempt - 1, 0), cap)


@dataclass(frozen=True)
class DispatchPlan:
    status: str
    reason: Optional[str]
    provider_message_id: Optional[str]
    mark_sent: bool
    # Only set when requeuing; a terminal outcome has no next attempt.
    retry_after_seconds: Optional[int] = None


def plan_for_outcome(
    outcome: SendOutcome, attempt: int, max_attempts: int
) -> DispatchPlan:
    """Decide what one attempt means for the row.

    Pure, so the whole retry policy is testable without a database.
    ``attempt`` already counts this try — the claim increments it.
    """
    if outcome.status == STATUS_ACCEPTED:
        return DispatchPlan(
            status=STATUS_ACCEPTED,
            reason=None,
            provider_message_id=outcome.provider_message_id,
            mark_sent=True,
        )

    # A row with no reason is a support ticket nobody can answer.
    reason = outcome.reason or REASON_PROVIDER_REJECTED

    if not outcome.retryable:
        return DispatchPlan(
            status=STATUS_FAILED,
            reason=reason,
            provider_message_id=outcome.provider_message_id,
            mark_sent=False,
        )

    if attempt >= max_attempts:
        # Our reason, not the provider's last error — we stopped, they didn't.
        return DispatchPlan(
            status=STATUS_DEAD,
            reason=REASON_ATTEMPTS_EXHAUSTED,
            provider_message_id=outcome.provider_message_id,
            mark_sent=False,
        )

    # Requeue, but not immediately: the delay is what makes a retry a retry
    # rather than a second helping of whatever just failed.
    #
    # Note this is at-least-once, not exactly-once. The dedupe key stops a
    # producer creating a second ROW; it cannot stop this row reaching the
    # provider twice — if the provider accepted the attempt and we lost the
    # response, the retry sends again. That risk is bounded by the claim
    # lease, not eliminated.
    return DispatchPlan(
        status=STATUS_QUEUED,
        reason=reason,
        provider_message_id=outcome.provider_message_id,
        mark_sent=False,
        retry_after_seconds=backoff_seconds(
            attempt, CRM_DISPATCH_RETRY_BASE_SECONDS, RETRY_MAX_SECONDS
        ),
    )


def _jittered(seconds: Optional[int]) -> Optional[int]:
    """Spread retries by +-20%, so a provider outage does not produce a
    synchronised stampede when every backed-off row comes due at once."""
    if seconds is None:
        return None
    return max(1, round(seconds * random.uniform(0.8, 1.2)))


async def claim_sends(batch: int) -> List[QueuedMessage]:
    """The scaffold's claim: sweep stale leases, then claim up to ``batch``.

    Lease-style, not txn-style — the claim commits before any send happens
    (holding a transaction across a provider call would pin a pooled
    connection for as long as the network takes), and the stale sweep is
    what expires a dead worker's lease.
    """
    # Reset per pass, so the previous message's ids can't leak into this
    # pass's claim and sweep lines.
    set_log_context(component=LOG_COMPONENT)

    # Reclaim first so rows abandoned by a dead worker rejoin this batch
    # instead of waiting another tick.
    reclaimed = await accessor.requeue_stale_claims(CRM_DISPATCH_STALE_MINUTES)
    if reclaimed:
        # Named, not counted: these are the rows a double-send investigation
        # starts from.
        logger.warning(
            f"crm dispatch reclaimed {len(reclaimed)} stale claim(s): "
            f"{sample_ids(reclaimed)}"
        )

    messages = await accessor.claim_queued_messages(batch)
    _log_queue_lag(messages, batch)
    return messages


def _log_queue_lag(messages: Sequence[QueuedMessage], limit: int) -> None:
    """How long the oldest claimed row sat past due — the alert that rises
    whatever the cause. Measured from next_attempt_at (timestamptz NOT NULL,
    so always aware), not created_at: a retry serving out its backoff is
    waiting on purpose, and on-purpose waiting is not lag."""
    if not messages:
        return
    oldest = min(m.next_attempt_at for m in messages)
    lag_s = (datetime.now(timezone.utc) - oldest).total_seconds()
    logger.info(
        f"crm dispatch claimed {len(messages)} message(s) lag_s={lag_s:.1f} "
        f"queue_deeper_than_batch={len(messages) >= limit}: "
        f"{sample_ids([m.id for m in messages])}"
    )


async def dispatch_send(message: QueuedMessage) -> None:
    """The scaffold's per-row handler: one claimed message, send to outcome."""
    await _dispatch_one(message, CRM_DISPATCH_MAX_ATTEMPTS)


async def _dispatch_one(message: QueuedMessage, max_attempts: int) -> None:
    """Work one claimed message.

    Never raises: the rest of the batch is already claimed, so an escaping
    exception would strand it until those claims expire.

    Nothing is wrapped in a transaction. Holding one across the provider call
    would pin a pooled connection for as long as the network takes, and one
    slow provider could then starve the app behind the connection pooler.
    """
    # Structured, so a log search can filter by message or merchant instead of
    # substring-matching the text. Every line below inherits these fields.
    set_log_context(
        component=LOG_COMPONENT,
        message_id=message.id,
        merchant_id=message.merchant_id,
        customer_id=message.customer_id,
        dedupe_key=message.dedupe_key,
        channel=message.channel,
        attempt=message.attempt,
    )
    try:
        outcome = await send(message)
    except Exception as e:
        # Retryable, not rejected: we don't know whether the provider saw it.
        # opt(exception=) not exc_info=True — loguru drops the latter's stack.
        logger.opt(exception=e).error(f"send raised for message {message.id}")
        outcome = SendOutcome(
            status=STATUS_FAILED, reason=REASON_SEND_ERROR, retryable=True
        )

    # message.attempt already counts this try — the claim incremented it.
    plan = plan_for_outcome(outcome, message.attempt, max_attempts)

    try:
        applied = await accessor.apply_outcome(
            message.id,
            plan.status,
            plan.reason,
            plan.provider_message_id,
            plan.mark_sent,
            _jittered(plan.retry_after_seconds),
        )
    except Exception as e:
        # Leave it claimed; the sweep will hand it to another worker.
        # opt(exception=) not exc_info=True — loguru drops the latter's stack.
        logger.opt(exception=e).error(
            f"could not record outcome for message {message.id}"
        )
        return

    if not applied:
        # Our send outlasted the claim window and the row already belongs to
        # someone else. Their outcome wins.
        logger.warning(
            f"message {message.id} was reclaimed mid-send — "
            f"outcome '{plan.status}' discarded"
        )
        return

    logger.info(
        f"message {message.id} -> {plan.status}"
        + (f" ({plan.reason})" if plan.reason else "")
    )
