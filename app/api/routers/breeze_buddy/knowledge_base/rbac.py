"""
RBAC utilities for knowledge base endpoints.
Mirrors the templates router: reseller + merchant scoping from the JWT,
never from request parameters alone.
"""

from typing import List, Optional, Tuple

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def validate_kb_access(
    current_user: UserInfo,
    reseller_id: str,
    merchant_id: Optional[str],
    operation: str = "access",
) -> None:
    """Validate the user can touch a KB belonging to reseller/merchant.

    Raises:
        HTTPException: 403 if the user lacks permission
    """
    if current_user.role == "admin":
        return

    if (
        reseller_id not in current_user.reseller_ids
        and "*" not in current_user.reseller_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} knowledge "
            f"base for unauthorized reseller: {reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {reseller_id}",
        )

    if merchant_id:
        if (
            merchant_id not in current_user.merchant_ids
            and "*" not in current_user.merchant_ids
        ):
            logger.warning(
                f"User {current_user.username} attempted to {operation} knowledge "
                f"base for unauthorized merchant: {merchant_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {merchant_id}",
            )


def require_admin_or_reseller_owner(
    current_user: UserInfo, reseller_id: str, operation: str = "perform this operation"
) -> None:
    """Write-gate: admin, or a user owning the target reseller.

    Raises:
        HTTPException: 403 if the user lacks permission
    """
    if current_user.role == "admin":
        return

    if reseller_id in current_user.reseller_ids or "*" in current_user.reseller_ids:
        return

    logger.warning(
        f"User {current_user.username} (role: {current_user.role}) attempted "
        f"to {operation} for unauthorized reseller: {reseller_id}"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Access denied to {operation} for reseller {reseller_id}",
    )


def accessible_scope_filters(
    current_user: UserInfo,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """Resolve list filters from the JWT (+ optional narrowing params).

    Returns (reseller_ids, merchant_ids) where None means unrestricted
    (admin/wildcard). Requested narrowing filters are validated against the
    JWT scope.

    Raises:
        HTTPException: 403 on out-of-scope filter requests
    """
    if current_user.role == "admin" or "*" in current_user.reseller_ids:
        reseller_ids = [reseller_id] if reseller_id else None
    else:
        if reseller_id:
            if reseller_id not in current_user.reseller_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to reseller {reseller_id}",
                )
            reseller_ids = [reseller_id]
        else:
            if not current_user.reseller_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: user has no reseller assignments",
                )
            reseller_ids = current_user.reseller_ids

    if current_user.role == "admin" or "*" in current_user.merchant_ids:
        merchant_ids = [merchant_id] if merchant_id else None
    else:
        if merchant_id:
            if merchant_id not in current_user.merchant_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {merchant_id}",
                )
            merchant_ids = [merchant_id]
        else:
            # Empty list means NO assignments — mirror templates RBAC and
            # deny, never widen to unrestricted (None) visibility.
            if not current_user.merchant_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: user has no merchant assignments",
                )
            merchant_ids = current_user.merchant_ids

    return reseller_ids, merchant_ids
