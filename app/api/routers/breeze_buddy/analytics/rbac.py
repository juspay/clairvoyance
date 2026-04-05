"""
RBAC (Role-Based Access Control) utilities for analytics.
Handles hierarchical reseller + merchant filtering based on JWT token.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def get_accessible_resellers_and_merchants(
    current_user: UserInfo,
) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """
    Get user's accessible resellers and merchants from JWT token.

    Args:
        current_user: Current authenticated user with RBAC info

    Returns:
        Tuple of (accessible_resellers, accessible_merchants)
        - None means wildcard access (["*"])
        - List[str] means specific access to those IDs
        - Empty list [] means no access (will match nothing in queries)

    Examples:
        Admin: (None, None) - full access to all resellers and merchants
        Reseller: (["m1", "m2"], None) - specific resellers, all their merchants
        Merchant: (["m1"], None) - single reseller, all its merchants
        Shop: (["m1"], ["shop_123"]) - single reseller, single merchant
        Unscoped: ([], []) - no access to any resellers or merchants
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
        accessible_resellers = None  # Wildcard access
    else:
        accessible_resellers = reseller_ids  # Specific access (or empty for no access)

    # Check merchant access
    # Same logic as reseller access
    if "*" in merchant_ids:
        accessible_merchants = None  # Wildcard access
    else:
        accessible_merchants = merchant_ids  # Specific access (or empty for no access)

    return accessible_resellers, accessible_merchants


def apply_hierarchical_filters(
    filters: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Apply hierarchical reseller + merchant filtering based on user's JWT token.

    CRITICAL SECURITY: Always use reseller_ids and merchant_ids from JWT token,
    NEVER from request parameters.

    Args:
        filters: Request filters (may contain reseller/merchant filters)
        current_user: Current authenticated user

    Returns:
        Updated filters with validated reseller/merchant access

    Raises:
        HTTPException: 403 if user tries to access unauthorized resellers/merchants
    """
    accessible_resellers, accessible_merchants = get_accessible_resellers_and_merchants(
        current_user
    )

    # Apply merchant filtering
    if accessible_resellers is None:
        # Admin/wildcard access - can access all merchants
        # Keep any merchant filters from request (if admin specified specific merchants)
        pass
    else:
        # Non-admin user - enforce merchant access
        # Check if user has no reseller access (empty list)
        if len(accessible_resellers) == 0:
            # User has no reseller access - should not be able to access any data
            logger.warning(
                f"User {current_user.username} has no reseller access (empty reseller_ids)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no reseller assignments",
            )

        # Support both new (reseller_id/reseller_ids) and old (merchant_id/merchant_ids) field names
        filter_reseller_id = filters.get("reseller_id")
        filter_reseller_ids = filters.get("reseller_ids")

        if filter_reseller_id:
            # Validate user has access to requested reseller
            if filter_reseller_id not in accessible_resellers:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized reseller: {filter_reseller_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to reseller {filter_reseller_id}",
                )
        elif filter_reseller_ids:
            # Validate user has access to all requested resellers
            if not all(m in accessible_resellers for m in filter_reseller_ids):
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized resellers"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to one or more requested resellers",
                )
        else:
            # No reseller filter - apply user's accessible resellers
            filters["reseller_ids"] = accessible_resellers

    # Apply merchant filtering
    if accessible_merchants is None:
        # Admin/wildcard merchant access - can access all merchants (within accessible resellers)
        # Keep any merchant filters from request (if user specified specific merchants)
        pass
    else:
        # Non-admin user - enforce merchant access
        # Check if user has no merchant access (empty list)
        if len(accessible_merchants) == 0:
            # User has no merchant access - should not be able to access any data
            logger.warning(
                f"User {current_user.username} has no merchant access (empty merchant_ids)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no merchant assignments",
            )

        # Support (merchant_id/merchant_ids) field names for merchant filtering
        filter_merchant_id = filters.get("merchant_id")
        filter_merchant_ids = filters.get("merchant_ids")

        if filter_merchant_id:
            # Validate user has access to requested merchant
            if filter_merchant_id not in accessible_merchants:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchant: {filter_merchant_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {filter_merchant_id}",
                )
        elif filter_merchant_ids:
            # Validate user has access to all requested merchants
            if not all(
                merchant in accessible_merchants for merchant in filter_merchant_ids
            ):
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchants"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to one or more requested merchants",
                )
        else:
            # No merchant filter - apply user's accessible merchants
            filters["merchant_ids"] = accessible_merchants

    return filters
