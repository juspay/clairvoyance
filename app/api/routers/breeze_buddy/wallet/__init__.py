"""
Wallet API endpoints.

Admin-only endpoints for managing merchant wallet balances.
"""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.wallets import (
    WalletRechargeRequest,
    WalletTransactionResponse,
)

from .handlers import recharge_wallet_handler

router = APIRouter()


@router.post("/wallet/{merchant_id}/recharge", response_model=WalletTransactionResponse)
async def recharge_wallet(
    merchant_id: str,
    recharge_data: WalletRechargeRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Add credits to a merchant's wallet.

    **RBAC rules:**
    - Admin only.

    Args:
        merchant_id: The merchant identifier whose wallet to recharge
        recharge_data: Amount and currency of the recharge

    Returns:
        The created wallet_transactions ledger row

    Raises:
        HTTPException: 403 if not admin, 404 if merchant not found,
            409 on duplicate ledger reference
    """
    return await recharge_wallet_handler(merchant_id, recharge_data, current_user)
