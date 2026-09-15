"""
Generic RBAC authorization utilities.
Provides role and permission checking that can be used across all agents.

Role hierarchy: admin > reseller > merchant > user

Scope resolution (resolve_merchant_ids, resolve_reseller_ids, etc.)
has been moved to app.core.security.scope.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo, UserRole


def merchant_scope_permitted(
    current_user: UserInfo, merchant_id: Optional[str]
) -> bool:
    """
    Decide whether the caller's merchant scope covers an object with this
    merchant_id, assuming the caller already passed the reseller-scope check.

    A null/empty merchant_id marks a reseller-scoped row. It must NEVER be
    treated as "no merchant restriction" for merchant/user-role callers — only a
    reseller-role account (or admin) may touch reseller-scoped rows.
    Merchant/user roles must own the specific merchant_id.

    Admin is answered here rather than relied upon at the call site. Every
    current caller does return early for admin, so this changes no behaviour
    today; it means a future caller that forgets the guard fails by denying an
    admin nothing rather than by denying admins reseller-scoped rows.
    """
    if current_user.role == UserRole.ADMIN:
        return True
    if merchant_id:
        return (
            merchant_id in current_user.merchant_ids or "*" in current_user.merchant_ids
        )
    return current_user.role == UserRole.RESELLER


def check_permission(current_user: UserInfo, required_permission: str) -> bool:
    """
    Check if user has a specific permission.

    Args:
        current_user: Current authenticated user
        required_permission: Permission to check (e.g., "read:all", "write:own_data")

    Returns:
        True if user has the permission, False otherwise
    """
    # Admins have all permissions
    if current_user.role == UserRole.ADMIN:
        return True

    # Check if user has the specific permission
    return required_permission in current_user.permissions


def require_permission(current_user: UserInfo, required_permission: str) -> None:
    """
    Require user to have a specific permission.

    Args:
        current_user: Current authenticated user
        required_permission: Permission required

    Raises:
        HTTPException: 403 Forbidden if user doesn't have permission
    """
    if not check_permission(current_user, required_permission):
        logger.warning(
            f"User {current_user.username} lacks permission: {required_permission}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions. Required: {required_permission}",
        )


def require_role(current_user: UserInfo, required_roles: List[UserRole]) -> None:
    """
    Require user to have one of the specified roles.

    Args:
        current_user: Current authenticated user
        required_roles: List of acceptable roles

    Raises:
        HTTPException: 403 Forbidden if user doesn't have required role
    """
    if current_user.role not in required_roles:
        logger.warning(
            f"User {current_user.username} (role: {current_user.role}) "
            f"attempted action requiring roles: {required_roles}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient role. Required one of: {[r.value for r in required_roles]}",
        )


def require_admin(current_user: UserInfo) -> None:
    """
    Require user to be an admin.

    Args:
        current_user: Current authenticated user

    Raises:
        HTTPException: 403 Forbidden if user is not admin
    """
    if current_user.role != UserRole.ADMIN:
        logger.warning(
            f"Non-admin user {current_user.username} attempted admin-only action"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


def is_admin(current_user: UserInfo) -> bool:
    """
    Check if user is an admin.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is admin, False otherwise
    """
    return current_user.role == UserRole.ADMIN


def is_reseller(current_user: UserInfo) -> bool:
    """
    Check if user is a reseller.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is reseller, False otherwise
    """
    return current_user.role == UserRole.RESELLER


def is_merchant(current_user: UserInfo) -> bool:
    """
    Check if user is a merchant.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is merchant, False otherwise
    """
    return current_user.role == UserRole.MERCHANT


def is_user(current_user: UserInfo) -> bool:
    """
    Check if user is a user-level (shop) account.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is a regular user, False otherwise
    """
    return current_user.role == UserRole.USER
