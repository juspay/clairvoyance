"""
RBAC (Role-Based Access Control) utilities for analytics.
Handles hierarchical merchant + shop filtering based on JWT token.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def get_accessible_merchants_and_shops(
    current_user: UserInfo,
) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """
    Get user's accessible merchant IDs and shop identifiers from JWT token.

    Args:
        current_user: Current authenticated user with RBAC info

    Returns:
        Tuple of (accessible_merchants, accessible_shops)
        - None means wildcard access (["*"])
        - List[str] means specific access to those IDs
        - Empty list [] means no access (will match nothing in queries)

    Examples:
        Admin: (None, None) - full access to all merchants and shops
        Reseller: (["m1", "m2"], None) - specific merchants, all their shops
        Merchant: (["m1"], None) - single merchant, all its shops
        Shop: (["m1"], ["shop_123"]) - single merchant, single shop
        Unscoped: ([], []) - no access to any merchants or shops
    """
    # Get reseller IDs
    reseller_ids = current_user.reseller_ids

    # Get merchant identifiers
    merchant_ids = current_user.merchant_ids

    # Check merchant access
    # Distinguish between:
    # - ["*"] -> None (wildcard/full access)
    # - ["merchant1", "merchant2"] -> specific list
    # - [] -> empty list (no access)
    if "*" in reseller_ids:
        accessible_merchants = None  # Wildcard access
    else:
        accessible_merchants = reseller_ids  # Specific access (or empty for no access)

    # Check shop access
    # Same logic as merchant access
    if "*" in merchant_ids:
        accessible_shops = None  # Wildcard access
    else:
        accessible_shops = merchant_ids  # Specific access (or empty for no access)

    return accessible_merchants, accessible_shops


def apply_hierarchical_filters(
    filters: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Apply hierarchical merchant + shop filtering based on user's JWT token.

    CRITICAL SECURITY: Always use reseller_ids and merchant_ids from JWT token,
    NEVER from request parameters.

    Args:
        filters: Request filters (may contain shop/merchant filters)
        current_user: Current authenticated user

    Returns:
        Updated filters with validated shop/merchant access

    Raises:
        HTTPException: 403 if user tries to access unauthorized shops/merchants
    """
    accessible_merchants, accessible_shops = get_accessible_merchants_and_shops(
        current_user
    )

    # Apply merchant filtering
    if accessible_merchants is None:
        # Admin/wildcard access - can access all merchants
        # Keep any merchant filters from request (if admin specified specific merchants)
        pass
    else:
        # Non-admin user - enforce merchant access
        # Check if user has no merchant access (empty list)
        if len(accessible_merchants) == 0:
            # User has no merchant access - should not be able to access any data
            logger.warning(
                f"User {current_user.username} has no merchant access (empty reseller_ids)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no merchant assignments",
            )

        # Support both new (reseller_id/reseller_ids) and old (merchant_id/merchant_ids) field names
        filter_reseller_id = filters.get("reseller_id")
        filter_reseller_ids = filters.get("reseller_ids")

        if filter_reseller_id:
            # Validate user has access to requested reseller
            if filter_reseller_id not in accessible_merchants:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized reseller: {filter_reseller_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to reseller {filter_reseller_id}",
                )
        elif filter_reseller_ids:
            # Validate user has access to all requested resellers
            if not all(m in accessible_merchants for m in filter_reseller_ids):
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized resellers"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to one or more requested merchants",
                )
        else:
            # No merchant filter - apply user's accessible merchants
            filters["reseller_ids"] = accessible_merchants

    # Apply shop filtering
    if accessible_shops is None:
        # Admin/wildcard shop access - can access all shops (within accessible merchants)
        # Keep any shop filters from request (if user specified specific shops)
        pass
    else:
        # Non-admin user - enforce shop access
        # Check if user has no shop access (empty list)
        if len(accessible_shops) == 0:
            # User has no shop access - should not be able to access any data
            logger.warning(
                f"User {current_user.username} has no shop access (empty merchant_ids)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no shop assignments",
            )

        # Support both new (merchant_id/merchant_ids) and old (shop_id/shop_ids) field names
        filter_merchant_id = filters.get("merchant_id")
        filter_merchant_ids = filters.get("merchant_ids")

        if filter_merchant_id:
            # Validate user has access to requested shop
            if filter_merchant_id not in accessible_shops:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shop: {filter_merchant_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to shop {filter_merchant_id}",
                )
        elif filter_merchant_ids:
            # Validate user has access to all requested shops
            if not all(shop in accessible_shops for shop in filter_merchant_ids):
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shops"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to one or more requested shops",
                )
        else:
            # No shop filter - apply user's accessible shops
            filters["merchant_ids"] = accessible_shops

    return filters
