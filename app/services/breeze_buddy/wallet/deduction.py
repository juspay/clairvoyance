"""Credit deduction for billable events (chat turns, voice calls, ...).

Strategy pattern: BILLING_RULES maps an event_type to a callable that
computes how many credits that event costs. deduct() looks up the rule by
event_type and dispatches -- adding a new billable event type (e.g. a
different chat surface, or a new voice product) is a one-line addition to
this dict, never a change to deduct() itself or its callers.
"""

import math
from decimal import Decimal
from typing import Callable, Dict

from app.database.accessor.breeze_buddy.wallets import apply_deduction, get_wallet
from app.schemas.breeze_buddy.wallets import WalletTransactionResponse
from app.services.breeze_buddy.wallet.exceptions import (
    UnknownEventTypeError,
    WalletNotFoundError,
)


def _voice_call_credits(duration_seconds: int, **_: object) -> Decimal:
    if duration_seconds <= 0:
        raise ValueError(
            f"voice_call billing requires a positive duration_seconds, "
            f"got {duration_seconds!r}"
        )
    return Decimal(math.ceil(duration_seconds / 30))


# Each rule receives whatever event-specific kwargs the caller passes into
# deduct(), and returns the (positive) number of credits the event costs.
# "chat_turn" needs no extra data (flat 1 credit per turn). "voice_call" is
# defined for completeness/future-readiness (per the finalized
# 1-credit-per-30-seconds billing rule) but is not yet wired into any voice
# call site -- unused until that feature is built.
BILLING_RULES: Dict[str, Callable[..., Decimal]] = {
    "chat_turn": lambda **_: Decimal(1),
    "voice_call": _voice_call_credits,
}


async def has_sufficient_credits(merchant_id: str) -> bool:
    """Cheap, unlocked read-only check: does this merchant currently have a
    positive balance?

    Deliberately does NOT take the wallet row lock -- this is a pre-flight
    gate, not the source of truth. The actual deduction (deduct(), below)
    is what enforces consistency; a race between this check and the actual
    deduction is acceptable here since zero/negative balance enforcement is
    out of scope for this phase (a merchant can still go slightly negative
    under concurrent turns, same as the wallet's existing recharge design).

    Raises:
        WalletNotFoundError: if no wallet exists for merchant_id.
    """
    wallet = await get_wallet(merchant_id)
    if wallet is None:
        raise WalletNotFoundError(merchant_id)
    return wallet.balance_credits > 0


async def deduct(
    merchant_id: str, event_type: str, ref_id: str, **event_kwargs: object
) -> WalletTransactionResponse:
    """Deduct credits from a merchant's wallet for a billable event.

    event_type selects the BILLING_RULES entry that computes how many
    credits to deduct. Any event-specific data the rule needs (e.g.
    voice_call's duration_seconds) is passed through via event_kwargs.
    ref_id is the caller-supplied idempotency key (e.g. the chat_message.id
    of the triggering user turn) -- combined with event_type as (gateway,
    gateway_ref_id), this reuses the existing UNIQUE(gateway, gateway_ref_id)
    constraint on wallet_transactions to give duplicate-deduction protection
    for free (same mechanism that protects recharges against webhook
    retries).

    Raises:
        UnknownEventTypeError: if event_type has no BILLING_RULES entry.
        ValueError: if no wallet exists for merchant_id, if the rule rejects
            the supplied event_kwargs (e.g. voice_call with a non-positive
            duration_seconds), or if a database connection could not be
            acquired.
        Exception: on database errors (including asyncpg.UniqueViolationError
            if ref_id was already used for this event_type -- duplicate call).
    """
    rule = BILLING_RULES.get(event_type)
    if rule is None:
        raise UnknownEventTypeError(event_type)

    credits = rule(**event_kwargs)

    return await apply_deduction(
        merchant_id=merchant_id,
        credits_delta=-credits,
        gateway=event_type,
        gateway_ref_id=ref_id,
    )


__all__ = ["BILLING_RULES", "has_sufficient_credits", "deduct"]
