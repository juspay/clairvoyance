"""
Wallet entity decoders - response builders for wallet-related queries.
"""

from app.schemas.breeze_buddy.wallets import WalletResponse, WalletTransactionResponse


def decode_wallet(row) -> WalletResponse:
    """Build WalletResponse from a wallets database row.

    Args:
        row: Database row containing a wallet

    Returns:
        WalletResponse instance
    """
    return WalletResponse(
        merchant_id=row["merchant_id"],
        reseller_id=row.get("reseller_id"),
        balance_credits=row["balance_credits"],
        conversion_rate=row["conversion_rate"],
        monthly_credit_limit=row.get("monthly_credit_limit") or 0,
        subscription_status=row.get("subscription_status") or "NONE",
        last_reset_date=row.get("last_reset_date"),
        trial_ends_at=row.get("trial_ends_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def decode_wallet_transaction(row) -> WalletTransactionResponse:
    """Build WalletTransactionResponse from a wallet_transactions database row.

    Args:
        row: Database row containing a wallet ledger entry

    Returns:
        WalletTransactionResponse instance
    """
    return WalletTransactionResponse(
        id=row["id"],
        merchant_id=row["merchant_id"],
        type=row["type"],
        credits_delta=row["credits_delta"],
        credit_balance_after=row["credit_balance_after"],
        amount=row.get("amount"),
        currency=row.get("currency"),
        gateway=row.get("gateway"),
        gateway_ref_id=row.get("gateway_ref_id"),
        made_by=row.get("made_by"),
        created_at=row["created_at"],
    )
