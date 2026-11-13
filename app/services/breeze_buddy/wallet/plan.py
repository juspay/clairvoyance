"""Monthly plan credits -- the same rules as Breeze Buddy voice's monthly
call limit (nautilus ``checkMonthlyLimit``): a limit, and a counter of what
was used this month.

A wallet has two pots:

- the plan: ``monthly_credit_limit`` a calendar month (UTC), with
  ``plan_used_credits`` counting what this month used. The counter belongs
  to the month in ``last_reset_date``; an older month counts as 0 used, so
  the plan refills on the 1st with no reset write -- voice's counter resets
  on the first order of the month the same way;
- ``balance_credits``: prepaid credits (recharges). Never reset.

A charge takes from the plan first, then from the balance. A plan change
only sets the new limit: used credits stay used, so an upgrade adds the
difference and a downgrade can leave nothing for the month -- voice swaps
the limit and keeps its counter. Before ``trial_ends_at`` everything is
free and allowed, even when FROZEN; otherwise FROZEN blocks.

A wallet with no plan (subscription_status 'NONE', limit 0) is a prepaid
wallet: every charge comes from the balance, as before.

The billing platform (nautilus for Shopify) owns which plan a merchant is
on and pushes it with set_plan().
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from app.database.accessor.breeze_buddy import wallets as wallets_accessor
from app.schemas.breeze_buddy.wallets import WalletResponse

NO_PLAN = "NONE"
ACTIVE = "ACTIVE"
FROZEN = "FROZEN"


@dataclass(frozen=True)
class Charge:
    """How a charge splits: from the month's plan credits, and from the
    prepaid balance. Both 0 during a trial."""

    from_plan: Decimal
    from_balance: Decimal


def current_period(now: Optional[datetime] = None) -> date:
    """First day of the current calendar month, in UTC."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date().replace(day=1)


def in_trial(wallet: WalletResponse, now: Optional[datetime] = None) -> bool:
    """Before the wallet's trial end."""
    if wallet.trial_ends_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now < wallet.trial_ends_at


def used_this_month(wallet: WalletResponse, period: date) -> Decimal:
    """Plan credits used in ``period``. A counter from an earlier month is 0."""
    if wallet.last_reset_date is None or wallet.last_reset_date < period:
        return Decimal(0)
    return wallet.plan_used_credits


def plan_left(wallet: WalletResponse, period: date) -> Decimal:
    """Plan credits still to use this month (never below 0)."""
    return max(
        wallet.monthly_credit_limit - used_this_month(wallet, period), Decimal(0)
    )


def plan_gate(wallet: WalletResponse, now: Optional[datetime] = None) -> Optional[bool]:
    """Allow (True) or block (False) regardless of credits; None = check the
    credits. Trial first: voice lets a trial through even when FROZEN."""
    if in_trial(wallet, now):
        return True
    if wallet.subscription_status == FROZEN:
        return False
    return None


def available(wallet: WalletResponse, period: date) -> Decimal:
    """What a charge can take now: the plan's rest for the month plus the
    prepaid balance."""
    return plan_left(wallet, period) + wallet.balance_credits


def split_charge(
    wallet: WalletResponse, cost: Decimal, period: date, now: datetime
) -> Charge:
    """Split ``cost`` on the locked wallet: plan first, the rest from the
    balance (which may go below 0, as before). Free during a trial."""
    if in_trial(wallet, now):
        return Charge(Decimal(0), Decimal(0))
    from_plan = min(cost, plan_left(wallet, period))
    return Charge(from_plan, cost - from_plan)


async def set_plan(
    merchant_id: str,
    monthly_credit_limit: Decimal,
    subscription_status: str,
    trial_ends_at: Optional[datetime] = None,
    made_by: Optional[str] = None,
) -> WalletResponse:
    """Put the merchant on a plan (or change it). Only the plan is stored:
    no credits move, the month's used count stays, so repeating the same
    plan changes nothing.

    Raises:
        ValueError: if no wallet exists for merchant_id.
    """
    return await wallets_accessor.set_wallet_plan(
        merchant_id=merchant_id,
        monthly_credit_limit=monthly_credit_limit,
        subscription_status=subscription_status,
        trial_ends_at=trial_ends_at,
        made_by=made_by,
    )
