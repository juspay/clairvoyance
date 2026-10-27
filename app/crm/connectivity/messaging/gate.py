"""The permission gate — asked before every message leaves, whichever way it
goes out.

It answers one question, "may this message reach this person now?", and
returns a refusal reason or None. The dispatcher asks it before each queued
send; nothing reaches send() without the token it mints. It lives in
messaging/ rather than outbound/ because it guards every message, not one
kind of send.

The gate exists as a thin slice: gate() probes platform suppression (fail
closed) before every send, and a refusal writes a 'blocked' row. The full
may_contact() — consent, purpose, quiet hours — replaces gate's body with
B5; the call sites never change.
"""

import asyncio
from typing import Optional

from app.core.config.static import CRM_MESSAGE_SEND_TIMEOUT_SECONDS
from app.core.logger import logger
from app.crm.connectivity.schemas.message import QueuedMessage, SendToken
from app.crm.connectivity.vocabulary.channels import gate_handle_kind_for
from app.crm.connectivity.vocabulary.reasons import (
    REASON_GATE_UNAVAILABLE,
    REASON_SUPPRESSED,
)
from app.crm.platform.contracts import is_suppressed


async def gate(message: QueuedMessage) -> Optional[str]:
    """Returns a refusal reason, or None to allow. may_contact() (B5)
    replaces this body; the call sites never change.

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
    """The token that names the one message gate() just allowed.

    Policy lives in gate(); this mints identity, and send() refuses a token
    that does not name this exact message — so no adapter was ever wired
    without one. When may_contact() (B5) replaces gate's body, its
    decision_id lands here, tracing a send back to the grant.
    """
    return SendToken(
        message_id=message.id,
        purpose_key=message.purpose_key,
        granted=True,
    )
