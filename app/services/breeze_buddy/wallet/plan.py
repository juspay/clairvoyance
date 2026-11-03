"""Monthly plan credits -- the same rules as Breeze Buddy voice's monthly
call limit (nautilus ``checkMonthlyLimit`` / ``reset_monthly_order_counters``).

A wallet on a plan holds "monthly_credit_limit minus what was used this month":

- the 1st of a calendar month (UTC): back to monthly_credit_limit, on the first
  credit check of the month -- voice resets on the first order of the month;
- plan change: moves by (new - old) at once, used credits stay used, so a
  downgrade can leave it at or below 0 -- voice keeps the used count and
  swaps the limit;
- before trial_ends_at: unlimited, even when FROZEN; otherwise FROZEN
  blocks -- as voice (the trial is a date checked on every use, there is no
  "trial ended" event).

A wallet with no plan (subscription_status 'NONE') is a prepaid wallet, untouched by
any of this.

The billing platform (nautilus for Shopify) owns which plan a merchant is
on and pushes it with set_plan().
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from app.database.accessor.breeze_buddy import wallets as wallets_accessor
from app.schemas.breeze_buddy.wallets import WalletResponse

NO_PLAN = "NONE"
ACTIVE = "ACTIVE"
FROZEN = "FROZEN"

#: Ledger gateways for plan-driven balance moves.
PLAN_CHANGE_GATEWAY = "plan_change"
PLAN_RESET_GATEWAY = "plan_reset"


@dataclass(frozen=True)
class PlanUpdate:
    """What to write: a balance move (0 = none) and the new plan state."""

    credits_delta: Decimal
    monthly_credit_limit: Decimal
    subscription_status: str
    last_reset_date: date
    trial_ends_at: Optional[datetime]


def current_period(now: Optional[datetime] = None) -> date:
    """First day of the current calendar month, in UTC."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date().replace(day=1)


def needs_reset(wallet: WalletResponse, period: date) -> bool:
    """A wallet on a plan whose balance belongs to an earlier month."""
    return wallet.subscription_status != NO_PLAN and (
        wallet.last_reset_date is None or wallet.last_reset_date < period
    )


def in_trial(wallet: WalletResponse, now: Optional[datetime] = None) -> bool:
    """Before the wallet's trial end."""
    if wallet.trial_ends_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now < wallet.trial_ends_at


def plan_gate(wallet: WalletResponse, now: Optional[datetime] = None) -> Optional[bool]:
    """Allow (True) or block (False) regardless of balance; None = check the
    balance. Trial first: voice lets a trial through even when FROZEN."""
    if in_trial(wallet, now):
        return True
    if wallet.subscription_status == FROZEN:
        return False
    return None


def decide_plan_change(
    wallet: WalletResponse,
    monthly_credit_limit: Decimal,
    subscription_status: str,
    trial_ends_at: Optional[datetime],
    period: date,
) -> PlanUpdate:
    """The plan becomes ``monthly_credit_limit`` / ``subscription_status`` / ``trial_ends_at``.

    - first plan: add the plan's credits (nothing used yet);
    - balance from an earlier month: start this month on the new plan;
    - same month: move by (new - old), used credits stay used.

    ``period`` is read before the wallet lock, so a reset that ran in between
    may already be on a later month: never move last_reset_date back, or the
    next check would redo that month's reset and hit its ledger ref.
    """
    if wallet.last_reset_date is not None and wallet.last_reset_date > period:
        period = wallet.last_reset_date
    if wallet.subscription_status == NO_PLAN:
        delta = monthly_credit_limit
    elif needs_reset(wallet, period):
        delta = monthly_credit_limit - wallet.balance_credits
    else:
        delta = monthly_credit_limit - wallet.monthly_credit_limit
    return PlanUpdate(
        delta, monthly_credit_limit, subscription_status, period, trial_ends_at
    )


def decide_reset(wallet: WalletResponse, period: date) -> Optional[PlanUpdate]:
    """New month: balance back to monthly_credit_limit. None if not due."""
    if not needs_reset(wallet, period):
        return None
    return PlanUpdate(
        wallet.monthly_credit_limit - wallet.balance_credits,
        wallet.monthly_credit_limit,
        wallet.subscription_status,
        period,
        wallet.trial_ends_at,
    )


async def set_plan(
    merchant_id: str,
    monthly_credit_limit: Decimal,
    subscription_status: str,
    trial_ends_at: Optional[datetime] = None,
    made_by: Optional[str] = None,
) -> WalletResponse:
    """Put the merchant on a plan (or change it). Repeating the same plan
    in the same month moves nothing.

    Raises:
        ValueError: if no wallet exists for merchant_id.
    """
    period = current_period()
    return await wallets_accessor.apply_plan_update(
        merchant_id=merchant_id,
        decide=lambda w: decide_plan_change(
            w, monthly_credit_limit, subscription_status, trial_ends_at, period
        ),
        gateway=PLAN_CHANGE_GATEWAY,
        gateway_ref_id=str(uuid4()),
        made_by=made_by,
    )


async def reset_month(merchant_id: str) -> WalletResponse:
    """Start the new month if it is due (no-op otherwise). Safe to call
    concurrently: the wallet lock makes exactly one caller reset, and the
    ledger ref (merchant + month) is unique.

    Raises:
        ValueError: if no wallet exists for merchant_id.
    """
    period = current_period()
    return await wallets_accessor.apply_plan_update(
        merchant_id=merchant_id,
        decide=lambda w: decide_reset(w, period),
        gateway=PLAN_RESET_GATEWAY,
        gateway_ref_id=f"{merchant_id}:{period.isoformat()}",
    )
