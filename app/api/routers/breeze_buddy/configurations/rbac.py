"""
RBAC (Role-Based Access Control) utilities for configurations
Handles reseller + shop access control based on JWT token.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def validate_config_access(
    current_user: UserInfo,
    reseller_id: Optional[str],
    merchant_identifier: Optional[str],
    operation: str = "access",
) -> None:
    """
    Validate that user has access to a specific reseller/merchant.

    Args:
        current_user: Current authenticated user with RBAC info
        reseller_id: Reseller ID to validate access for
        merchant_identifier: Shop identifier to validate access for (optional)
        operation: Operation being performed (for logging)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    # Admin has full access
    if current_user.role == "admin":
        return

    # Check reseller access
    if (
        reseller_id not in current_user.reseller_ids
        and "*" not in current_user.reseller_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} configuration "
            f"for unauthorized reseller: {reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {reseller_id}",
        )

    # Check merchant identifier access (only if merchant_identifier is provided)
    if (
        merchant_identifier
        and merchant_identifier not in current_user.merchant_identifiers
        and "*" not in current_user.merchant_identifiers
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} configuration "
            f"for unauthorized merchant: {merchant_identifier}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_identifier}",
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

    if "*" in current_user.reseller_ids and "*" in current_user.merchant_identifiers:
        return configs

    filtered = []
    for config in configs:
        # Support both new and old field names for config
        config_reseller_id = config.reseller_id
        config_merchant_identifier = config.merchant_identifier
        has_reseller_access = ("*" in current_user.reseller_ids) or (
            config_reseller_id in current_user.reseller_ids
        )
        has_merchant_access = ("*" in current_user.merchant_identifiers) or (
            config_merchant_identifier in current_user.merchant_identifiers
        )
        if has_reseller_access and has_merchant_access:
            filtered.append(config)

    return filtered
