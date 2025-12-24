"""
RBAC (Role-Based Access Control) utilities for leads.
Handles merchant + shop access control based on JWT token.
"""

from typing import Any, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def validate_lead_access(
    current_user: UserInfo,
    merchant_id: str,
    shop_identifier: Optional[str],
    operation: str = "access",
) -> None:
    """
    Validate user has access to leads for given merchant and shop.

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
            f"User {current_user.username} attempted to {operation} leads "
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
                f"User {current_user.username} attempted to {operation} leads "
                f"for unauthorized shop: {shop_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shop {shop_identifier}",
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

    # Check merchant access
    if (
        lead.merchant_id not in current_user.merchant_ids
        and "*" not in current_user.merchant_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} lead {lead.id} "
            f"for unauthorized merchant: {lead.merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Lead not found"
        )

    # Check shop access
    if lead.shop_identifier:
        if (
            lead.shop_identifier not in current_user.shop_identifiers
            and "*" not in current_user.shop_identifiers
        ):
            logger.warning(
                f"User {current_user.username} attempted to {operation} lead {lead.id} "
                f"for unauthorized shop: {lead.shop_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Lead not found"
            )


def validate_recording_access(
    current_user: UserInfo,
    call_sid: str,
    merchant_id: str,
    shop_identifier: Optional[str],
) -> None:
    """
    Validate user has access to a call recording.
    Returns 404 to avoid leaking existence of recordings.

    Args:
        current_user: Current authenticated user with RBAC info
        call_sid: Call SID being accessed (for logging)
        merchant_id: Merchant ID of the call
        shop_identifier: Shop identifier of the call (optional)

    Raises:
        HTTPException: 404 if user lacks permission (to avoid leaking existence)
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
            f"User {current_user.username} attempted to access recording "
            f"for unauthorized merchant: {merchant_id} (call_sid: {call_sid})"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording not found for call_sid: {call_sid}",
        )

    # Check shop access (if shop_identifier is specified)
    if shop_identifier:
        if (
            shop_identifier not in current_user.shop_identifiers
            and "*" not in current_user.shop_identifiers
        ):
            logger.warning(
                f"User {current_user.username} attempted to access recording "
                f"for unauthorized shop: {shop_identifier} (call_sid: {call_sid})"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recording not found for call_sid: {call_sid}",
            )
