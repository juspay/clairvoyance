"""
Wallet API endpoints.

Admin-only endpoints for managing merchant wallet balances.
"""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.wallets import (
    WalletPlanRequest,
    WalletRechargeRequest,
    WalletResponse,
    WalletTransactionResponse,
)

from .handlers import recharge_wallet_handler, set_wallet_plan_handler

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


@router.put("/wallet/{merchant_id}/plan", response_model=WalletResponse)
async def set_wallet_plan(
    merchant_id: str,
    plan_data: WalletPlanRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Put a merchant on a plan, or change it.

    Same rules as voice's monthly call limit: monthly_credit_limit per calendar
    month (UTC), reset on the first credit check of a month; a change moves
    the balance by (new - old) at once; FROZEN blocks; before trial_ends_at
    it is unlimited, even when FROZEN.

    **RBAC rules:**
    - Admin only.

    Args:
        merchant_id: The merchant whose plan to set
        plan_data: Monthly credits and billing status

    Returns:
        The wallet after the change

    Raises:
        HTTPException: 403 if not admin, 404 if merchant not found
    """
    return await set_wallet_plan_handler(merchant_id, plan_data, current_user)
