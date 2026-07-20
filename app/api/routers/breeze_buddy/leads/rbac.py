"""
RBAC (Role-Based Access Control) utilities for leads.
Handles reseller + shop access control based on JWT token.
"""

from typing import Any, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.core.security.authorization import merchant_scope_permitted
from app.schemas import UserInfo


def validate_lead_access(
    current_user: UserInfo,
    reseller_id: str,
    merchant_id: Optional[str],
    operation: str = "access",
) -> None:
    """
    Validate user has access to leads for given reseller and merchant.

    Args:
        current_user: Current authenticated user with RBAC info
        reseller_id: Reseller ID to validate access for
        merchant_id: Merchant identifier to validate access for (optional)
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
            f"User {current_user.username} attempted to {operation} leads "
            f"for unauthorized reseller: {reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {reseller_id}",
        )

    # Check merchant scope. A null merchant_id marks a reseller-scoped write:
    # only reseller-role accounts (or admin, above) may create it — never every
    # merchant sharing the reseller (PT-15). Mirrors validate_lead_read_access so
    # the write path can't be looser than the read path.
    if not merchant_scope_permitted(current_user, merchant_id):
        logger.warning(
            f"User {current_user.username} attempted to {operation} leads "
            f"for unauthorized merchant: {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_id}",
        )


def validate_lead_read_access(
    current_user: UserInfo, lead: Any, operation: str = "access"
) -> None:
    """
    Validate user has access to read a specific lead.

    Args:
        current_user: Current authenticated user
        lead: Lead object to validate access for
        operation: Operation being performed (for logging)

    Raises:
        HTTPException: 404 if user lacks permission (to avoid leaking existence)
    """
    # Admin has full access
    if current_user.role == "admin":
        return

    # Check reseller access
    if (
        lead.reseller_id not in current_user.reseller_ids
        and "*" not in current_user.reseller_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} lead {lead.id} "
            f"for unauthorized reseller: {lead.reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Lead not found"
        )

    # Check shop access. A null merchant_id marks a reseller-scoped lead: only
    # reseller-role accounts (or admin, above) may read it — not every merchant
    # sharing the reseller (PT-15).
    if not merchant_scope_permitted(current_user, lead.merchant_id):
        logger.warning(
            f"User {current_user.username} attempted to {operation} lead {lead.id} "
            f"for unauthorized merchant: {lead.merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found"
        )


def validate_recording_access(
    current_user: UserInfo,
    call_sid: str,
    reseller_id: str,
    merchant_id: Optional[str],
) -> None:
    """
    Validate user has access to a call recording.
    Returns 404 to avoid leaking existence of recordings.

    Args:
        current_user: Current authenticated user with RBAC info
        call_sid: Call SID being accessed (for logging)
        reseller_id: Reseller ID of the call
        merchant_id: Merchant ID of the call (optional)

    Raises:
        HTTPException: 404 if user lacks permission (to avoid leaking existence)
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
            f"User {current_user.username} attempted to access recording "
            f"for unauthorized reseller: {reseller_id} (call_sid: {call_sid})"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording not found for call_sid: {call_sid}",
        )

    # Check merchant access. Null merchant_id (reseller-scoped recording) is
    # reachable only by reseller-role accounts or admin, never every merchant in
    # the reseller (PT-15).
    if not merchant_scope_permitted(current_user, merchant_id):
        logger.warning(
            f"User {current_user.username} attempted to access recording "
            f"for unauthorized merchant: {merchant_id} (call_sid: {call_sid})"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording not found for call_sid: {call_sid}",
        )
