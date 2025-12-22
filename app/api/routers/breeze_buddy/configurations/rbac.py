"""
RBAC (Role-Based Access Control) utilities for configurations.
Handles merchant + shop access control based on JWT token.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


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


def filter_configs_by_rbac(configs: List, current_user: UserInfo) -> List:
    """
    Filter configurations based on user's RBAC permissions.

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

    filtered = []
    for config in configs:
        # Check merchant access
        has_merchant_access = (
            "*" in current_user.merchant_ids
            or config.merchant_id in current_user.merchant_ids
        )

        # Check shop access
        has_shop_access = (
            "*" in current_user.shop_identifiers
            or config.shop_identifier in current_user.shop_identifiers
        )

        if has_merchant_access and has_shop_access:
            filtered.append(config)

    return filtered
