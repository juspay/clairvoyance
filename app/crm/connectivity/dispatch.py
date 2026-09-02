"""Outbound dispatch — claims queued messages and hands each to send().

Producers only queue; this decides. That split exists so the permission
check happens at send time rather than compose time, and so a customer who
opts out in between is still honoured.

The loop itself is not here: claim_sends()/dispatch_send() plug into the
shared drain-loop scaffold (app/crm/shared/worker.py) as the "dispatcher"
role in app/crm/worker_main.py. This file owns only what a row means.

The gate exists as a thin slice: _gate() below probes platform suppression
(fail closed) before every send, and a refusal writes a 'blocked' row. The
full may_contact() — consent, purpose, quiet hours — replaces _gate's body
with B5; the call site never changes.
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from app.core.config.static import (
    CRM_DISPATCH_MAX_ATTEMPTS,
    CRM_DISPATCH_RETRY_BASE_SECONDS,
    CRM_DISPATCH_STALE_MINUTES,
    CRM_MESSAGE_SEND_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.core.logger.context import set_log_context
from app.crm.connectivity.channels import gate_handle_kind_for
from app.crm.connectivity.db.accessors import message as message_accessor
from app.crm.connectivity.reasons import (
    REASON_ATTEMPTS_EXHAUSTED,
    REASON_GATE_UNAVAILABLE,
    REASON_PROVIDER_REJECTED,
    REASON_SEND_ERROR,
    REASON_SUPPRESSED,
)
from app.crm.connectivity.schemas import QueuedMessage, SendOutcome, SendToken
from app.crm.connectivity.send import send
from app.crm.platform.contracts import is_suppressed

LOG_COMPONENT = "crm.connectivity.dispatch"

# Batch size is operator-configurable and the stale sweep is unbounded, so
# never interpolate a whole id list into a log line — one bad tick would
# emit a multi-megabyte record.
LOG_ID_SAMPLE = 100

# Ceiling on the exponential backoff. Not a config knob: at the default three
# attempts it is never reached, and it exists only so that raising
# CRM_DISPATCH_MAX_ATTEMPTS cannot silently park a message for hours.
RETRY_MAX_SECONDS = 300

# 'failed' is the provider refusing for good; 'blocked' is US refusing
# (gate, no route); 'dead' is us running out of retries. A merchant asking
# why nothing arrived needs to know which (T16 col 12).
STATUS_QUEUED = "queued"
STATUS_ACCEPTED = "accepted"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_DEAD = "dead"

# All REASON_* words live in reasons.py — one file, one name per failure
# mode. Channel metadata (which handle kind the gate probes) lives in
# channels.py — one registry, pinned ADAPTERS ⊆ CHANNELS by the test suite.


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

    if outcome.status == STATUS_BLOCKED:
        # OUR refusal: terminal, nothing was sent, and retrying cannot
        # change what WE decided (T16: blocked vs failed vs dead).
        return DispatchPlan(
            status=STATUS_BLOCKED,
            reason=outcome.reason or REASON_GATE_UNAVAILABLE,
            provider_message_id=None,
            mark_sent=False,
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

    # Requeue with a delay — the delay is what makes it a retry rather than a
    # second helping of whatever just failed.
    #
    # At-least-once, not exactly-once: the dedupe key stops a producer
    # creating a second ROW, but if the provider accepted an attempt whose
    # response we lost, the retry sends again. Bounded by the claim lease,
    # not eliminated.
    return DispatchPlan(
        status=STATUS_QUEUED,
        reason=reason,
        provider_message_id=outcome.provider_message_id,
        mark_sent=False,
        retry_after_seconds=backoff_seconds(
            attempt, CRM_DISPATCH_RETRY_BASE_SECONDS, RETRY_MAX_SECONDS
        ),
    )


async def _gate(message: QueuedMessage) -> Optional[str]:
    """Returns a refusal reason, or None to allow. may_contact() (B5)
    replaces this body; the call site never changes.

    The thin slice that exists today is the one check a person who said
    STOP is protected by: platform's suppression probe, which itself fails
    closed (a DB error inside it reads as suppressed). Consent, purpose and
    quiet hours arrive with the full gate.
    """
    # TODO(permission): the full gate must ALSO call may_contact() (B5) —
    # consent from the consent table, purpose authorisation, quiet hours.
    # Suppression stays as check #1; may_contact() joins it here, and its
    # decision_id flows into mint_send_token below.
    kind = gate_handle_kind_for(message.channel)
    if kind is None:
        # A channel the gate cannot check must not slip through unchecked.
        return REASON_GATE_UNAVAILABLE
    try:
        # Same deadline law as send(): the probe reads the same pool, and a
        # hung probe stalls the serial batch past the claim lease exactly
        # like a hung provider — same reassigned tail, same double send. The
        # lease-pin test bounds BATCH × timeout × 2, which covers this
        # deadline plus send()'s.
        if await asyncio.wait_for(
            is_suppressed({kind: message.sent_to_address}),
            timeout=CRM_MESSAGE_SEND_TIMEOUT_SECONDS,
        ):
            return REASON_SUPPRESSED
    except asyncio.TimeoutError:
        # Fail closed, not retry-later-by-word: nothing was sent, so this is
        # OUR refusal, same as a probe that raised.
        logger.error(
            f"gate probe timed out after {CRM_MESSAGE_SEND_TIMEOUT_SECONDS}s "
            f"for {message.id}"
        )
        return REASON_GATE_UNAVAILABLE
    except Exception as e:
        # is_suppressed is total by contract; this catches an escape from
        # OUR plumbing around it. Unknown gate input → NO (ADR 0018).
        logger.opt(exception=e).error(f"gate probe raised for {message.id}")
        return REASON_GATE_UNAVAILABLE
    return None


def mint_send_token(message: QueuedMessage) -> SendToken:
    """The token that names the one message _gate() just allowed.

    Policy lives in _gate(); this mints identity, and send() refuses a token
    that does not name this exact message — so no adapter was ever wired
    without one. When may_contact() (B5) replaces _gate's body, its
    decision_id lands here, tracing a send back to the grant.
    """
    return SendToken(
        message_id=message.id,
        purpose_key=message.purpose_key,
        granted=True,
    )


def _jittered(seconds: Optional[int]) -> Optional[int]:
    """Spread retries by +-20%, so a provider outage does not produce a
    synchronised stampede when every backed-off row comes due at once."""
    if seconds is None:
        return None
    return max(1, round(seconds * random.uniform(0.8, 1.2)))


async def claim_sends(batch: int) -> List[QueuedMessage]:
    """The scaffold's claim: sweep stale leases, then claim up to ``batch``.

    Lease-style, not txn-style: the claim commits before any send happens,
    because holding a transaction across a provider call would pin a pooled
    connection for as long as the network takes. The stale sweep is what
    expires a dead worker's lease.
    """
    # Reset per pass, so the previous message's ids can't leak into this
    # pass's claim and sweep lines.
    set_log_context(component=LOG_COMPONENT)

    # Reclaim first, so rows abandoned by a dead worker rejoin this batch
    # instead of waiting a tick. The sweep KILLS rather than requeues a row
    # out of attempts — every lap through it was a claim that really sent, so
    # an unbounded sweep would be an unbounded sender.
    reclaimed, exhausted = await message_accessor.requeue_stale_claims(
        CRM_DISPATCH_STALE_MINUTES, CRM_DISPATCH_MAX_ATTEMPTS
    )
    if reclaimed:
        # Named, not counted: these are the rows a double-send investigation
        # starts from.
        logger.warning(
            f"crm dispatch reclaimed {len(reclaimed)} stale claim(s): "
            f"{sample_ids(reclaimed)}"
        )
    if exhausted:
        # Louder than a reclaim: each of these was attempted max times and
        # never once recorded an outcome — the customer may have every copy.
        logger.error(
            f"crm dispatch killed {len(exhausted)} stale claim(s) out of "
            f"attempts: {sample_ids(exhausted)}"
        )

    messages = await message_accessor.claim_queued_messages(batch)
    _log_queue_lag(messages, batch)
    return messages


def _log_queue_lag(messages: Sequence[QueuedMessage], limit: int) -> None:
    """How long the oldest claimed row sat past due — the alert that rises
    whatever the cause. Measured from next_attempt_at, not created_at: a
    retry serving out its backoff is waiting on purpose, and on-purpose
    waiting is not lag."""
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
    exception would strand it until those claims expire. Nothing is wrapped
    in a transaction either — one held across a provider call would pin a
    pooled connection for as long as the network takes.
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
    refusal = await _gate(message)
    if refusal is not None:
        # Blocked before the adapter is ever touched — the row records OUR
        # refusal with its reason, and nothing reaches the provider.
        # gate_unavailable is louder: it means misconfig (a channel the gate
        # cannot probe), not policy — the one refusal an operator must notice.
        log = logger.error if refusal == REASON_GATE_UNAVAILABLE else logger.warning
        log(f"message {message.id} blocked by gate — {refusal}")
        outcome = SendOutcome(status=STATUS_BLOCKED, reason=refusal)
    else:
        try:
            outcome = await send(mint_send_token(message), message)
        except Exception as e:
            # Retryable, not rejected: we don't know whether the provider
            # saw it. opt(exception=) — loguru drops exc_info's stack.
            logger.opt(exception=e).error(f"send raised for message {message.id}")
            outcome = SendOutcome(
                status=STATUS_FAILED, reason=REASON_SEND_ERROR, retryable=True
            )

    # message.attempt already counts this try — the claim incremented it.
    plan = plan_for_outcome(outcome, message.attempt, max_attempts)

    try:
        applied = await message_accessor.apply_outcome(
            message.id,
            plan.status,
            plan.reason,
            plan.provider_message_id,
            plan.mark_sent,
            # The claim's generation: if the sweep reassigned this row and a
            # newer claim bumped attempt, our late outcome must miss.
            message.attempt,
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
