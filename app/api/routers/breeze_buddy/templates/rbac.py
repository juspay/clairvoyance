"""
RBAC (Role-Based Access Control) utilities for templates.
Handles merchant + shop access control based on JWT token.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def validate_template_access(
    current_user: UserInfo,
    merchant_id: str,
    shop_identifier: Optional[str],
    operation: str = "access",
) -> None:
    """
    Validate user has access to template for given merchant and shop.

    Args:
        current_user: Current authenticated user with RBAC info
        merchant_id: Merchant ID to validate access for
        shop_identifier: Shop identifier to validate access for (optional)
        operation: Operation being performed (for logging)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    # Admin has full access
    if current_user.role == "admin":
        return

    # Check merchant access
    if (
        merchant_id not in current_user.merchant_ids
        and "*" not in current_user.merchant_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} template "
            f"for unauthorized merchant: {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_id}",
        )

    # Check shop access (if shop_identifier is specified)
    if shop_identifier:
        if (
            shop_identifier not in current_user.shop_identifiers
            and "*" not in current_user.shop_identifiers
        ):
            logger.warning(
                f"User {current_user.username} attempted to {operation} template "
                f"for unauthorized shop: {shop_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shop {shop_identifier}",
            )


def filter_templates_by_rbac(templates: List, current_user: UserInfo) -> List:
    """
    Filter templates based on user's RBAC permissions.

    Args:
        templates: List of template objects
        current_user: Current authenticated user

    Returns:
        Filtered list of templates user has access to
    """
    # Admin sees all
    if current_user.role == "admin":
        return templates

    # Wildcard access
    if "*" in current_user.merchant_ids and "*" in current_user.shop_identifiers:
        return templates

    filtered = []
    for template in templates:
        # Check merchant access
        has_merchant_access = (
            "*" in current_user.merchant_ids
            or template.merchant_id in current_user.merchant_ids
        )

        # Check shop access (templates might not have shop_identifier)
        if template.shop_identifier:
            has_shop_access = (
                "*" in current_user.shop_identifiers
                or template.shop_identifier in current_user.shop_identifiers
            )
        else:
            # Templates without shop_identifier are accessible if merchant access granted
            has_shop_access = True

        if has_merchant_access and has_shop_access:
            filtered.append(template)

    return filtered


def require_admin_or_merchant_owner(
    current_user: UserInfo, merchant_id: str, operation: str = "perform this operation"
) -> None:
    """
    Require user to be admin or merchant owner.

    Used for create/update operations on templates.

    Args:
        current_user: Current authenticated user
        merchant_id: Merchant ID being modified
        operation: Operation being performed (for error message)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    # Admin has full access
    if current_user.role == "admin":
        return

    # Merchant owners can manage their own templates
    if merchant_id in current_user.merchant_ids or "*" in current_user.merchant_ids:
        return

    logger.warning(
        f"User {current_user.username} (role: {current_user.role}) "
        f"attempted to {operation} for unauthorized merchant: {merchant_id}"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Access denied to {operation} for merchant {merchant_id}",
    )


def require_admin(
    current_user: UserInfo, operation: str = "perform this operation"
) -> None:
    """
    Require user to have admin role.

    Used for destructive operations like template deletion.

    Args:
        current_user: Current authenticated user
        operation: Operation being performed (for error message)

    Raises:
        HTTPException: 403 if user is not admin
    """
    if current_user.role == "admin":
        return

    logger.warning(
        f"User {current_user.username} (role: {current_user.role}) "
        f"attempted to {operation} without admin privileges"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Admin access required to {operation}",
    )


def apply_hierarchical_template_filters(
    filters: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Apply hierarchical merchant + shop filtering based on user's JWT token.

    CRITICAL SECURITY: Always use merchant_ids and shop_identifiers from JWT token,
    NEVER from request parameters alone.

    Similar to analytics RBAC pattern, this function:
    1. Extracts accessible merchants/shops from JWT
    2. Validates any merchant/shop filters in the request
    3. Injects user's accessible merchants/shops if not specified
    4. Returns 403 if user tries to access unauthorized resources

    Args:
        filters: Request filters (may contain merchant_id/shop_identifier)
        current_user: Current authenticated user with RBAC info

    Returns:
        Updated filters with validated merchant/shop access

    Raises:
        HTTPException: 403 if user tries to access unauthorized merchants/shops
    """
    # Determine accessible merchants
    if "*" in current_user.merchant_ids:
        accessible_merchants = None  # Wildcard access (admin)
    else:
        accessible_merchants = current_user.merchant_ids

    # Determine accessible shops
    if "*" in current_user.shop_identifiers:
        accessible_shops = None  # Wildcard access
    else:
        accessible_shops = current_user.shop_identifiers

    # Apply merchant filtering
    if accessible_merchants is None:
        # Admin/wildcard access - can access all merchants
        # Keep any merchant filters from request (if admin specified specific merchants)
        pass
    else:
        # Non-admin user - enforce merchant access
        # Check if user has no merchant access (empty list)
        if len(accessible_merchants) == 0:
            logger.warning(
                f"User {current_user.username} has no merchant access (empty merchant_ids)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no merchant assignments",
            )

        if "merchant_id" in filters:
            # Validate user has access to requested merchant
            if filters["merchant_id"] not in accessible_merchants:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchant: {filters['merchant_id']}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {filters['merchant_id']}",
                )
            # Keep the single merchant_id filter (no change needed)
        else:
            # No merchant filter - apply user's accessible merchants as array
            filters["merchant_ids"] = accessible_merchants

    # Apply shop filtering
    if accessible_shops is None:
        # Admin/wildcard shop access - can access all shops (within accessible merchants)
        # Keep any shop filters from request (if user specified specific shops)
        pass
    else:
        # Non-admin user - enforce shop access
        # Check if user has no shop access (empty list)
        if len(accessible_shops) == 0:
            logger.warning(
                f"User {current_user.username} has no shop access (empty shop_identifiers)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no shop assignments",
            )

        if "shop_identifier" in filters:
            # Validate user has access to requested shop
            if filters["shop_identifier"] not in accessible_shops:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shop: {filters['shop_identifier']}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to shop {filters['shop_identifier']}",
                )
            # Keep the single shop_identifier filter (no change needed)
        else:
            # No shop filter - apply user's accessible shops as array
            filters["shop_identifiers"] = accessible_shops

    logger.info(
        f"Applied hierarchical filters for user {current_user.username}: {filters}"
    )

    return filters
