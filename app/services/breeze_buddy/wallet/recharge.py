"""
Wallet recharge orchestration.

Owns the full recharge operation: fetching the merchant's conversion_rate,
computing the credits to add via services.breeze_buddy.wallet.conversion, and persisting
the recharge through the database accessor layer. This mirrors the pattern
used by services.knowledge_base.ingestion, which similarly owns a full
operation and calls the accessor layer directly.
"""

from decimal import Decimal
from typing import Optional
from uuid import uuid4

from app.database.accessor.breeze_buddy import wallets as wallets_accessor
from app.schemas.breeze_buddy.wallets import WalletTransactionResponse
from app.services.breeze_buddy.wallet import conversion
from app.services.breeze_buddy.wallet.exceptions import InvalidRechargeAmountError


async def recharge(
    merchant_id: str,
    amount: Decimal,
    currency: str,
    made_by: Optional[str] = None,
) -> WalletTransactionResponse:
    """Add credits to a merchant's wallet and record the ledger entry.

    Raises:
        InvalidRechargeAmountError: if amount is not strictly positive.
        ValueError: if no wallet exists for merchant_id.
        UnsupportedCurrencyError: if currency has no credits mapping.
        Exception: on database errors.
    """
    # Re-check the positive-amount invariant here even though
    # WalletRechargeRequest already enforces gt=0 -- recharge() is a public
    # service function and can be called directly, bypassing that request
    # schema's validation.
    if amount <= 0:
        raise InvalidRechargeAmountError(amount)

    wallet = await wallets_accessor.get_wallet(merchant_id)
    if not wallet:
        raise ValueError(f"No wallet found for merchant '{merchant_id}'")

    # Normalize once here so the same value is used for both the credits
    # computation and what gets persisted to the immutable ledger row.
    currency = currency.upper()
    credits_delta = conversion.compute_credits(amount, currency, wallet.conversion_rate)

    # This is currently the only recharge path (admin-triggered, no payment
    # gateway yet), so gateway/gateway_ref_id are generated here directly.
    # When a real gateway (e.g. Juspay/Stripe) is added, this is the only
    # place that needs to change -- the accessor layer stays untouched.
    gateway = "manual"
    gateway_ref_id = str(uuid4())

    return await wallets_accessor.apply_recharge(
        merchant_id=merchant_id,
        credits_delta=credits_delta,
        amount=amount,
        currency=currency,
        gateway=gateway,
        gateway_ref_id=gateway_ref_id,
        made_by=made_by,
    )
