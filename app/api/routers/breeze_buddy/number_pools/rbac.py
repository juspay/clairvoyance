"""
RBAC utility functions for outbound number pool operations.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.schemas.breeze_buddy.auth import UserInfo, UserRole
from app.schemas.breeze_buddy.core import OutboundNumberPool


def require_admin_access(
    current_user: UserInfo, operation: str = "manage number pools"
):
    """Require admin role for pool management operations."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin access required to {operation}",
        )


def validate_pool_access(
    current_user: UserInfo,
    reseller_id: str,
    merchant_id: Optional[str],
):
    """Validate that the user has access to a pool's tenant scope.

    Admins and wildcard-scoped users pass unconditionally.
    Others must have the pool's reseller_id in their scope,
    and if the pool has a merchant_id, it must also be in scope.
    Raises 404 (not 403) to avoid leaking resource existence.
    """
    if current_user.role == UserRole.ADMIN:
        return

    if "*" in current_user.reseller_ids and "*" in current_user.merchant_ids:
        return

    has_reseller_access = ("*" in current_user.reseller_ids) or (
        reseller_id in current_user.reseller_ids
    )
    has_merchant_access = ("*" in current_user.merchant_ids) or (
        merchant_id is None or merchant_id in current_user.merchant_ids
    )

    if not (has_reseller_access and has_merchant_access):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pool not found",
        )


def filter_pools_by_rbac(
    pools: List[OutboundNumberPool], current_user: UserInfo
) -> List[OutboundNumberPool]:
    """Filter pools based on user's RBAC scope.

    Admins and wildcard-scoped users see all pools.
    Other users only see pools whose reseller_id and merchant_id
    fall within their granted scope.
    """
    if current_user.role == UserRole.ADMIN:
        return pools

    if "*" in current_user.reseller_ids and "*" in current_user.merchant_ids:
        return pools

    filtered = []
    for pool in pools:
        has_reseller_access = ("*" in current_user.reseller_ids) or (
            pool.reseller_id in current_user.reseller_ids
        )
        has_merchant_access = ("*" in current_user.merchant_ids) or (
            pool.merchant_id is None or pool.merchant_id in current_user.merchant_ids
        )
        if has_reseller_access and has_merchant_access:
            filtered.append(pool)

    return filtered
