"""Request/response schemas for the wallet endpoints."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import AwareDatetime, BaseModel, Field


class WalletRechargeRequest(BaseModel):
    """Admin request to add credits to a merchant's wallet."""

    amount: Decimal = Field(
        ...,
        gt=Decimal(0),
        description="Amount paid, in the given currency. Must be > 0.",
    )
    currency: str = Field(
        ..., min_length=3, max_length=3, description="ISO currency code, e.g. INR"
    )


class WalletResponse(BaseModel):
    """A merchant's wallet (current cached balance state)."""

    merchant_id: str
    reseller_id: Optional[str] = None
    balance_credits: Decimal
    conversion_rate: Decimal
    monthly_credit_limit: Decimal = Decimal(0)
    plan_used_credits: Decimal = Decimal(0)
    subscription_status: str = "NONE"
    last_reset_date: Optional[date] = None
    trial_ends_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WalletTransactionResponse(BaseModel):
    """A single wallet ledger entry (row from wallet_transactions)."""

    id: int
    merchant_id: str
    type: str
    credits_delta: Decimal
    credit_balance_after: Decimal
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    gateway: Optional[str] = None
    gateway_ref_id: Optional[str] = None
    made_by: Optional[str] = None
    created_at: Optional[datetime] = None


class WalletPlanRequest(BaseModel):
    """Set a merchant's plan: its monthly credits and billing status."""

    monthly_credit_limit: Decimal = Field(
        ...,
        ge=Decimal(0),
        le=Decimal(1_000_000_000),
        description="Credits per calendar month (the plan's limit).",
    )
    subscription_status: Literal["ACTIVE", "FROZEN"] = Field(
        ...,
        description="ACTIVE: use credits normally. FROZEN: block (billing on hold).",
    )
    trial_ends_at: Optional[AwareDatetime] = Field(
        None,
        description=(
            "Unlimited before this instant, even when FROZEN (voice's trial). "
            "Must include a timezone. Omit or null for no trial."
        ),
    )
