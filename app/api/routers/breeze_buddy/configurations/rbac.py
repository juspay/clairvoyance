"""
RBAC (Role-Based Access Control) utilities for configurations.
Handles merchant + shop access control based on JWT token.
"""

from typing import List, Optional, Set

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor import get_templates_list
from app.schemas import UserInfo


async def get_accessible_template_ids(current_user: UserInfo) -> Set[str]:
    """
    Get set of template IDs accessible to the user based on RBAC.

    Args:
        current_user: Current authenticated user

    Returns:
        Set of template IDs the user has access to
    """
    # Admin and wildcard access - return empty set to indicate "all access"
    if current_user.role == "admin":
        return set()  # Empty set means all access

    if "*" in current_user.merchant_ids and "*" in current_user.shop_identifiers:
        return set()  # Empty set means all access

    # Build filters based on user's RBAC
    filters = {}
    if current_user.merchant_ids and "*" not in current_user.merchant_ids:
        filters["merchant_ids"] = current_user.merchant_ids
    if current_user.shop_identifiers and "*" not in current_user.shop_identifiers:
        filters["shop_identifiers"] = current_user.shop_identifiers

    templates = await get_templates_list(filters)
    return {t.id for t in templates}


def validate_config_access(
    current_user: UserInfo,
    merchant_id: str,
    shop_identifier: Optional[str],
    operation: str = "access",
) -> None:
    """
    Validate user has access to configuration for given merchant and shop.

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
            f"User {current_user.username} attempted to {operation} configuration "
            f"for unauthorized merchant: {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_id}",
        )

    # Check shop access (only if shop_identifier is provided)
    if (
        shop_identifier
        and shop_identifier not in current_user.shop_identifiers
        and "*" not in current_user.shop_identifiers
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} configuration "
            f"for unauthorized shop: {shop_identifier}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to shop {shop_identifier}",
        )


async def filter_configs_by_rbac(configs: List, current_user: UserInfo) -> List:
    """
    Filter configurations based on user's RBAC permissions.
    Configs are filtered by checking if their template_id belongs to
    a template the user has access to.

    Args:
        configs: List of configuration objects
        current_user: Current authenticated user

    Returns:
        Filtered list of configurations user has access to
    """
    # Admin sees all
    if current_user.role == "admin":
        return configs

    # Wildcard access
    if "*" in current_user.merchant_ids and "*" in current_user.shop_identifiers:
        return configs

    # Get accessible template IDs
    accessible_template_ids = await get_accessible_template_ids(current_user)

    # If empty set returned, user has access to all templates
    if not accessible_template_ids:
        return configs

    # Filter configs by template_id
    return [c for c in configs if c.template_id in accessible_template_ids]
