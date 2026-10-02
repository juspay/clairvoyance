"""send_session() — a free-form reply inside the customer-service window.

The other way a message leaves this module. A template is PROPOSED
(queue.py) and the dispatcher decides later; a free-form reply is sent NOW,
synchronously, because a person or Buddy is mid-conversation and the reply
is only worth anything while the customer is there (ADR 0014: "human reply:
gate -> send() -> reference row, read-your-writes").

What stays the same as a template, on purpose:

- **One manifest row per reply** (canon T16), written before the provider
  is called, deduped on the caller's key — so a retried call never sends
  twice, and "what did we send her" has one answer.
- **The same gate and the same send door**: suppression fails closed, the
  pipe / door / credential checks in send.py apply unchanged.

What differs, and why:

- **The words are not on the row.** The manifest stores no rendered text
  (T16); the ``message.queued`` letter carries them (decision D1), filed
  alongside the send so even a refused reply leaves its words on the spine.
- **No dispatcher, no retry ladder.** A row the dispatcher cannot read the
  words of must never be claimed by it: the row is written in flight with
  no claim (see insert_session_message_query), closed here, and — if this
  process dies first — closed dead by its own sweep. A retry is the
  caller's, under a NEW dedupe key.
- **The window is not checked here.** Whether the customer wrote in the
  last 24 hours is the conversations module's predicate (it knows her last
  message; this module does not), and Meta's 131047 is the backstop.

Gather -> decide -> apply: the refusals that need nothing are PURE
(``validate_session_send``, ``plan_for_session_outcome``); the row, the
letter, the gate and the provider are the shell around them.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.core.logger import logger
from app.core.logger.context import (
    get_log_context,
    set_log_context,
    update_log_context,
)
from app.crm.connectivity.channels import conversation_profile
from app.crm.connectivity.db.accessors import message as message_accessor
from app.crm.connectivity.dispatch import gate, mint_send_token, refusal_outcome
from app.crm.connectivity.letters import file_queued_letter
from app.crm.connectivity.queue import (
    SERVICE_ROOT,
    SESSION_SOURCE_KINDS,
    normalize_address,
    purpose_root,
)
from app.crm.connectivity.reasons import (
    REASON_GATE_UNAVAILABLE,
    REASON_PROVIDER_REJECTED,
    REASON_SEND_ERROR,
    reason_class,
)
from app.crm.connectivity.receipts import apply_parked
from app.crm.connectivity.schemas.message import (
    QueuedMessage,
    SendOutcome,
    SessionBodyType,
    SessionSendResult,
)
from app.crm.connectivity.send import deliver_session_send
from app.crm.connectivity.status import (
    MESSAGE_ACCEPTED,
    MESSAGE_BLOCKED,
    MESSAGE_FAILED,
)

LOG_COMPONENT = "crm.connectivity.session"


def validate_session_send(
    channel: str, address: str, source_kind: str, purpose_key: str
) -> str:
    """PURE: the normalized address a session send goes to, or ValueError.

    Every refusal here is the CALLER's defect, raised before anything is
    written: only Buddy or a teammate make free-form replies, a free-form
    reply is always a 'service' message (Meta's category for it, D8), the
    channel must carry a conversation at all, and the address must be one.
    """
    if source_kind not in SESSION_SOURCE_KINDS:
        raise ValueError(
            f"a free-form reply comes from one of {SESSION_SOURCE_KINDS}, "
            f"not {source_kind!r}"
        )
    if purpose_root(purpose_key) != SERVICE_ROOT:
        raise ValueError(
            f"a free-form reply's purpose_key must start with {SERVICE_ROOT!r}"
        )
    if conversation_profile(channel) is None:
        raise ValueError(f"{channel!r} does not carry free-form conversations")
    sent_to = normalize_address(channel, address)
    if sent_to is None:
        raise ValueError(f"unusable {channel} address")
    return sent_to


@dataclass(frozen=True)
class SessionPlan:
    status: str
    reason: Optional[str]
    provider_message_id: Optional[str]
    mark_sent: bool
    retryable: bool


def plan_for_session_outcome(outcome: SendOutcome) -> SessionPlan:
    """PURE: what one attempt means for a session row — settled, always.

    There is no 'queued' and no 'dead' here: the row is never retried by
    the dispatcher, so a retryable failure is written as 'failed' with the
    provider's word and handed back to the caller flagged ``retryable``.
    """
    if outcome.status == MESSAGE_ACCEPTED:
        return SessionPlan(
            MESSAGE_ACCEPTED, None, outcome.provider_message_id, True, False
        )
    if outcome.status == MESSAGE_BLOCKED:
        return SessionPlan(
            MESSAGE_BLOCKED,
            outcome.reason or REASON_GATE_UNAVAILABLE,
            None,
            False,
            False,
        )
    return SessionPlan(
        MESSAGE_FAILED,
        outcome.reason or REASON_PROVIDER_REJECTED,
        outcome.provider_message_id,
        False,
        outcome.retryable,
    )


async def send_session(
    *,
    merchant_id: str,
    customer_id: str,
    channel: str,
    address: str,
    body: SessionBodyType,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    dedupe_key: str,
    binding_id: Optional[str] = None,
) -> SessionSendResult:
    """Send one free-form reply now, and say what became of it.

    ``binding_id`` names the pipe to reply from (the number the customer
    wrote to); None is the merchant's default for the channel. Raises
    ValueError on a proposal ``validate_session_send`` refuses; every other
    outcome — our refusal, the provider's, a timeout — comes back as a
    result, never a raise.
    """
    # A library call, not an entrypoint: its fields ride this send's lines
    # (the send door's included) and the caller's own context is put back
    # exactly as it was — the Inbox or the responder keeps its fields.
    caller_context = get_log_context()
    update_log_context(
        component=LOG_COMPONENT,
        merchant_id=merchant_id,
        customer_id=customer_id,
        channel=channel,
        source_kind=source_kind,
        source_id=source_id,
        dedupe_key=dedupe_key,
    )
    try:
        return await _send_session(
            merchant_id=merchant_id,
            customer_id=customer_id,
            channel=channel,
            address=address,
            body=body,
            source_kind=source_kind,
            source_id=source_id,
            purpose_key=purpose_key,
            dedupe_key=dedupe_key,
            binding_id=binding_id,
        )
    finally:
        set_log_context(**caller_context)


async def _send_session(
    *,
    merchant_id: str,
    customer_id: str,
    channel: str,
    address: str,
    body: SessionBodyType,
    source_kind: str,
    source_id: Optional[str],
    purpose_key: str,
    dedupe_key: str,
    binding_id: Optional[str],
) -> SessionSendResult:
    """send_session's body, inside the caller-safe log scope."""
    sent_to = validate_session_send(channel, address, source_kind, purpose_key)

    message_id = await message_accessor.insert_session_message(
        merchant_id,
        customer_id,
        channel,
        sent_to,
        source_kind,
        source_id,
        purpose_key,
        dedupe_key,
    )
    if message_id is None:
        existing = await message_accessor.message_state_by_dedupe(
            merchant_id, dedupe_key
        )
        if existing is None:
            # The unique said a row exists and the read found none: a row
            # deleted in between, which nothing in this system does.
            raise RuntimeError(f"dedupe key {dedupe_key!r} conflicted with no row")
        logger.info(f"session send {dedupe_key} already written as {existing.id}")
        return SessionSendResult(
            message_id=existing.id,
            status=existing.status,
            reason=existing.reason,
            provider_message_id=existing.provider_message_id,
            duplicate=True,
        )

    message = QueuedMessage(
        id=message_id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        channel=channel,
        sent_to_address=sent_to,
        binding_id=binding_id,
        source_kind=source_kind,
        source_id=source_id,
        purpose_key=purpose_key,
        template_id=None,
        variables={},
        dedupe_key=dedupe_key,
        attempt=1,
        next_attempt_at=datetime.now(timezone.utc),
    )
    # The letter and the send run side by side: the letter is fail-open
    # (record_event never raises) and nothing about the send waits on it, so
    # the reply does not pay a spine write in its latency.
    _, outcome = await asyncio.gather(
        file_queued_letter(
            merchant_id=merchant_id,
            customer_id=customer_id,
            message_id=message_id,
            channel=channel,
            sent_to_address=sent_to,
            source_kind=source_kind,
            source_id=source_id,
            purpose_key=purpose_key,
            template_id=None,
            variables={},
            body=body,
        ),
        _deliver(message, body),
    )
    plan = plan_for_session_outcome(outcome)
    await _record(message, plan, outcome.binding_id)
    logger.bind(
        message_id=message_id,
        outcome=plan.status,
        reason=plan.reason,
        reason_class=reason_class(plan.reason),
        permanent=True,
    ).info(
        f"session message {message_id} -> {plan.status}"
        + (f" ({plan.reason})" if plan.reason else "")
    )
    return SessionSendResult(
        message_id=message_id,
        status=plan.status,
        reason=plan.reason,
        provider_message_id=plan.provider_message_id,
        retryable=plan.retryable,
    )


async def _deliver(message: QueuedMessage, body: SessionBodyType) -> SendOutcome:
    """The gate, then the send door. Never raises."""
    refusal = await gate(message)
    if refusal is not None:
        logger.warning(f"session message {message.id} stopped by gate — {refusal}")
        return refusal_outcome(refusal)
    try:
        return await deliver_session_send(mint_send_token(message), message, body)
    except Exception as e:
        # We do not know whether the provider saw it — retryable, like the
        # dispatcher's catch-all.
        logger.opt(exception=e).error(f"session send raised for {message.id}")
        return SendOutcome(
            status=MESSAGE_FAILED, reason=REASON_SEND_ERROR, retryable=True
        )


async def _record(
    message: QueuedMessage, plan: SessionPlan, binding_id: Optional[str]
) -> None:
    """Close the row. A write that fails or misses leaves it 'sending' for
    the abandoned-session sweep — the reply's fate is still what the caller
    is told, because it is what the provider did."""
    try:
        applied = await message_accessor.apply_outcome(
            message.id,
            plan.status,
            plan.reason,
            plan.provider_message_id,
            plan.mark_sent,
            message.attempt,
            None,
            binding_id=binding_id,
        )
    except Exception as e:
        logger.opt(exception=e).error(
            f"could not record outcome for session message {message.id}"
        )
        return
    if not applied:
        logger.warning(
            f"session message {message.id} was closed by the sweep before its "
            f"outcome '{plan.status}' could be recorded"
        )
        return
    # The row knows its provider id now: apply any receipt that raced ahead.
    await apply_parked(message.merchant_id, plan.provider_message_id)
