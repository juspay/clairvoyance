"""
RBAC (Role-Based Access Control) utilities for configurations
Handles reseller + shop access control based on JWT token.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.core.security.authorization import merchant_scope_permitted
from app.schemas import UserInfo


def validate_config_access(
    current_user: UserInfo,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    operation: str = "access",
    allow_merchant_only: bool = False,
) -> None:
    """
    Validate that user has access to a specific reseller/merchant.

    Args:
        current_user: Current authenticated user with RBAC info
        reseller_id: Reseller ID to validate access for
        merchant_id: Merchant ID to validate access for (optional)
        operation: Operation being performed (for logging)
        allow_merchant_only: If True, allows validation with only merchant_id
                             (no reseller_id required). Used for merchant-only toggle.

    Raises:
        HTTPException: 403 if user lacks permission
    """
    # Admin has full access
    if current_user.role == "admin":
        return

    # Merchant-only access check (when reseller_id is None but merchant_id is provided)
    if allow_merchant_only and reseller_id is None and merchant_id is not None:
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

    # Check merchant identifier access. A null merchant_id marks a
    # reseller-scoped (reseller-wide) config: only reseller-role accounts (or
    # admin, handled above) may act on it — a merchant/user role must not fall
    # through the old `if merchant_id:` skip (PT-14/PT-15).
    if not merchant_scope_permitted(current_user, merchant_id):
        logger.warning(
            f"User {current_user.username} attempted to {operation} configuration "
            f"for unauthorized merchant: {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied to merchant {merchant_id}"
                if merchant_id
                else "Access denied to reseller-scoped configuration"
            ),
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

    if "*" in current_user.reseller_ids and "*" in current_user.merchant_ids:
        return configs

    filtered = []
    for config in configs:
        # Support both new and old field names for config
        config_reseller_id = config.reseller_id
        config_merchant_id = config.merchant_id
        has_reseller_access = ("*" in current_user.reseller_ids) or (
            config_reseller_id in current_user.reseller_ids
        )
        has_merchant_access = ("*" in current_user.merchant_ids) or (
            config_merchant_id in current_user.merchant_ids
        )
        if has_reseller_access and has_merchant_access:
            filtered.append(config)

    return filtered
