"""
Merchants API endpoints.

Admin-only endpoints for managing and viewing merchants (shop_identifiers).
"""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.merchants import MerchantsResponse

from .handlers import list_merchants_handler

router = APIRouter()


@router.get("/merchants", response_model=MerchantsResponse)
async def list_merchants(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all unique merchants (shop_identifiers).

    **Admin-only endpoint.**

    Returns all unique shop_identifiers from call_execution_config.
    Each shop_identifier represents a distinct merchant in the system.

    This assumes every shop has at least one call execution config.

    Returns:
        MerchantsResponse with list of merchant identifiers and total count

    Raises:
        403: If user is not admin
        500: If there's an error retrieving merchants
    """
    return await list_merchants_handler(current_user)
