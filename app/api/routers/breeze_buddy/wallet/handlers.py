"""
Handlers for wallet endpoints.
"""

import asyncpg
from fastapi import HTTPException

from app.core.logger import logger
from app.database.accessor.breeze_buddy import merchants as merchant_accessors
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.wallets import (
    WalletRechargeRequest,
    WalletTransactionResponse,
)
from app.services.breeze_buddy.wallet import recharge
from app.services.breeze_buddy.wallet.exceptions import (
    InvalidRechargeAmountError,
    UnsupportedCurrencyError,
)


def _check_recharge_access(current_user: UserInfo) -> None:
    """Only admins can recharge a merchant's wallet in this phase."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403, detail="Only admins can recharge a merchant's wallet"
        )


async def recharge_wallet_handler(
    merchant_id: str,
    recharge_data: WalletRechargeRequest,
    current_user: UserInfo,
) -> WalletTransactionResponse:
    """Add credits to a merchant's wallet and record the ledger entry.

    Admin-only. The resulting credits are computed by
    services.breeze_buddy.wallet.recharge as amount * currency-map *
    wallet.conversion_rate.
    """
    _check_recharge_access(current_user)

    merchant = await merchant_accessors.get_merchant_by_merchant_identifier(merchant_id)
    if not merchant:
        raise HTTPException(
            status_code=404, detail=f"Merchant '{merchant_id}' not found"
        )

    try:
        return await recharge(
            merchant_id=merchant_id,
            amount=recharge_data.amount,
            currency=recharge_data.currency,
            made_by=current_user.id,
        )
    except UnsupportedCurrencyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InvalidRechargeAmountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409, detail="Duplicate wallet transaction reference"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error recharging wallet for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to recharge wallet")
