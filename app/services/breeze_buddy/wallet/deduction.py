"""Credit deduction for billable events (chat turns, voice calls, ...).

Strategy pattern: BILLING_RULES maps an event_type to a callable that
computes how many credits that event costs. deduct() looks up the rule by
event_type and dispatches -- adding a new billable event type (e.g. a
different chat surface, or a new voice product) is a one-line addition to
this dict, never a change to deduct() itself or its callers.
"""

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Optional

from app.database.accessor.breeze_buddy.wallets import (
    DeductionPlan,
    apply_deduction,
    get_wallet,
)
from app.schemas.breeze_buddy.wallets import WalletResponse, WalletTransactionResponse
from app.services.breeze_buddy.wallet import plan
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


#: Credits one generated try-on image costs. A flat price, kept here with
#: the rule that applies it — the same place chat_turn's 1 and
#: voice_call's 30-second block live. The wallet check reads it too, so
#: the balance a shopper is refused on is the price they would be charged.
TRY_ON_CREDITS = 5


# Each rule receives whatever event-specific kwargs the caller passes into
# deduct(), and returns the (positive) number of credits the event costs.
# "chat_turn" needs no extra data (flat 1 credit per turn). "voice_call" is
# defined for completeness/future-readiness (per the finalized
# 1-credit-per-30-seconds billing rule) but is not yet wired into any voice
# call site -- unused until that feature is built.
BILLING_RULES: Dict[str, Callable[..., Decimal]] = {
    "chat_turn": lambda **_: Decimal(1),
    "voice_call": _voice_call_credits,
    "try_on": lambda **_: Decimal(TRY_ON_CREDITS),
}


async def has_sufficient_credits(
    merchant_id: str,
    event_type: Optional[str] = None,
    floor: int = 0,
    **event_kwargs: object,
) -> bool:
    """Cheap, unlocked read-only check: does this merchant currently have
    credits -- the month's plan credits left plus the prepaid balance? With
    ``event_type``, whether they cover that event's price and still leave
    ``floor`` credits; ``event_kwargs`` are passed to the rule exactly as
    deduct() passes them. A plan trial allows and FROZEN blocks, whatever
    the credits (services.breeze_buddy.wallet.plan).

    Deliberately does NOT take the wallet row lock -- this is a pre-flight
    gate, not the source of truth. The actual deduction (deduct(), below)
    is what enforces consistency; a race between this check and the actual
    deduction is acceptable here since zero/negative balance enforcement is
    out of scope for this phase (a merchant can still go slightly negative
    under concurrent turns, same as the wallet's existing recharge design).

    Raises:
        WalletNotFoundError: if no wallet exists for merchant_id.
        UnknownEventTypeError: if event_type has no BILLING_RULES entry.
    """
    wallet = await get_wallet(merchant_id)
    if wallet is None:
        raise WalletNotFoundError(merchant_id)
    rule = None
    if event_type is not None:
        rule = BILLING_RULES.get(event_type)
        if rule is None:
            raise UnknownEventTypeError(event_type)
    gate = plan.plan_gate(wallet)
    if gate is not None:
        return gate
    credits = plan.available(wallet, plan.current_period())
    if rule is None:
        return credits > 0
    return credits >= rule(**event_kwargs) + floor


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

    The charge takes the month's plan credits first, then the prepaid
    balance, decided on the locked wallet; a plan trial makes it free
    (services.breeze_buddy.wallet.plan). The ledger row records the
    balance move.

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

    cost = rule(**event_kwargs)
    now = datetime.now(timezone.utc)
    period = plan.current_period(now)

    def decide(wallet: WalletResponse) -> DeductionPlan:
        charge = plan.split_charge(wallet, cost, period, now)
        if wallet.subscription_status == plan.NO_PLAN:
            return DeductionPlan(balance_delta=-charge.from_balance)
        return DeductionPlan(
            balance_delta=-charge.from_balance,
            plan_used_credits=plan.used_this_month(wallet, period) + charge.from_plan,
            last_reset_date=period,
        )

    return await apply_deduction(
        merchant_id=merchant_id,
        gateway=event_type,
        gateway_ref_id=ref_id,
        decide=decide,
    )


__all__ = ["BILLING_RULES", "has_sufficient_credits", "deduct"]
