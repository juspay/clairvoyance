"""
RBAC (Role-Based Access Control) utilities for analytics.
Handles hierarchical merchant + shop filtering based on JWT token.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def get_accessible_merchants_and_shops(current_user: UserInfo) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """
    Get user's accessible merchant IDs and shop identifiers from JWT token.

    Args:
        current_user: Current authenticated user with RBAC info

    Returns:
        Tuple of (accessible_merchants, accessible_shops)
        - None means wildcard access (["*"])
        - List[str] means specific access to those IDs

    Examples:
        Admin: (None, None) - full access to all merchants and shops
        Reseller: (["m1", "m2"], None) - specific merchants, all their shops
        Merchant: (["m1"], None) - single merchant, all its shops
        Shop: (["m1"], ["shop_123"]) - single merchant, single shop
    """
    # Check merchant access
    accessible_merchants = None  # Default to full access
    if current_user.merchant_ids and "*" not in current_user.merchant_ids:
        accessible_merchants = current_user.merchant_ids

    # Check shop access
    accessible_shops = None  # Default to full access
    if current_user.shop_identifiers and "*" not in current_user.shop_identifiers:
        accessible_shops = current_user.shop_identifiers

    return accessible_merchants, accessible_shops


def apply_hierarchical_filters(
    filters: Dict[str, Any],
    current_user: UserInfo
) -> Dict[str, Any]:
    """
    Apply hierarchical merchant + shop filtering based on user's JWT token.

    CRITICAL SECURITY: Always use merchant_ids and shop_identifiers from JWT token,
    NEVER from request parameters.

    Args:
        filters: Request filters (may contain shop/merchant filters)
        current_user: Current authenticated user

    Returns:
        Updated filters with validated shop/merchant access

    Raises:
        HTTPException: 403 if user tries to access unauthorized shops/merchants
    """
    accessible_merchants, accessible_shops = get_accessible_merchants_and_shops(current_user)

    # Apply merchant filtering
    if accessible_merchants is None:
        # Admin/wildcard access - can access all merchants
        # Keep any merchant filters from request (if admin specified specific merchants)
        pass
    else:
        # Non-admin user - enforce merchant access
        if "merchant_id" in filters:
            # Validate user has access to requested merchant
            if filters["merchant_id"] not in accessible_merchants:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchant: {filters['merchant_id']}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {filters['merchant_id']}"
                )
        elif "merchant_ids" in filters:
            # Validate user has access to all requested merchants
            if not all(m in accessible_merchants for m in filters["merchant_ids"]):
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchants"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to one or more requested merchants"
                )
        else:
            # No merchant filter - apply user's accessible merchants
            filters["merchant_ids"] = accessible_merchants

    # Apply shop filtering
    if accessible_shops is None:
        # Admin/wildcard shop access - can access all shops (within accessible merchants)
        # Keep any shop filters from request (if user specified specific shops)
        pass
    else:
        # Non-admin user - enforce shop access
        if "shop_identifier" in filters:
            # Validate user has access to requested shop
            if filters["shop_identifier"] not in accessible_shops:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shop: {filters['shop_identifier']}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to shop {filters['shop_identifier']}"
                )
        elif "shop_identifiers" in filters:
            # Validate user has access to all requested shops
            if not all(shop in accessible_shops for shop in filters["shop_identifiers"]):
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shops"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to one or more requested shops"
                )
        else:
            # No shop filter - apply user's accessible shops
            filters["shop_identifiers"] = accessible_shops

    return filters
