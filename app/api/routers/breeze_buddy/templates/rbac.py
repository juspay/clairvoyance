"""
RBAC (Role-Based Access Control) utilities for templates.
Handles merchant + shop access control based on JWT token.
"""

from typing import List, Optional

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
    Require user to be admin.

    Args:
        current_user: Current authenticated user
        operation: Operation being performed (for error message)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    if current_user.role == "admin":
        return

    logger.warning(
        f"User {current_user.username} (role: {current_user.role}) "
        f"attempted to {operation} without admin access"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Access denied to {operation}",
    )
