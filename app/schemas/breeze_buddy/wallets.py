"""Request/response schemas for the wallet recharge endpoint."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


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
