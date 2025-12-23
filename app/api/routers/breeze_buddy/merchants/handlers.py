"""
Handlers for merchant endpoints.
"""

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.call_execution_config import get_all_merchants
from app.schemas import UserInfo
from app.schemas.breeze_buddy.merchants import MerchantsResponse

from .rbac import require_admin_access


async def list_merchants_handler(current_user: UserInfo) -> MerchantsResponse:
    """
    List all unique merchants (shop_identifiers).

    Admin-only endpoint that returns all unique shop_identifiers from call_execution_config.
    Each shop_identifier represents a distinct merchant in the system.

    Args:
        current_user: Current authenticated user (must be admin)

    Returns:
        MerchantsResponse with list of merchant identifiers

    Raises:
        HTTPException: 403 if user is not admin, 500 on error
    """
    # Validate admin access
    require_admin_access(current_user, "list merchants")

    logger.info(f"Admin user {current_user.username} requesting merchants list")

    try:
        # Get all unique merchants from database
        merchants = await get_all_merchants()

        logger.info(
            f"Returning {len(merchants)} merchants to admin {current_user.username}"
        )

        return MerchantsResponse(merchants=merchants, total=len(merchants))

    except Exception as e:
        logger.error(f"Error listing merchants: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing merchants: {str(e)}",
        )
