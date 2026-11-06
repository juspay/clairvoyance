"""
Business logic handlers for telephony number operations.
All handlers perform database operations and enforce business rules.
"""

from typing import List, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor import (
    check_number_purchase_conflict,
    create_telephony_number,
    disable_telephony_number,
    get_all_telephony_numbers,
    get_telephony_number_by_id,
    update_telephony_number,
)
from app.schemas import (
    CreateTelephonyNumberRequest,
    TelephonyNumber,
    UpdateTelephonyNumberRequest,
    UserInfo,
)

from .rbac import resolve_ownership as _resolve_ownership


async def create_number_handler(
    number: CreateTelephonyNumberRequest, current_user: UserInfo
) -> TelephonyNumber:
    """
    Provision a new telephony number.

    Ownership is explicit: merchant_id (merchant-owned, umbrella auto-filled),
    reseller_id only (umbrella-owned), or shared_pool=true (shared platform
    pool — the dispatcher's legacy NULL/NULL fallback).

    Args:
        number: Telephony number creation request
        current_user: Current authenticated user (must be admin)

    Returns:
        Created telephony number object

    Raises:
        HTTPException: 400 if the ownership shape is invalid or creation fails
    """
    logger.info(
        f"Admin {current_user.username} creating telephony number: {number.number}"
    )

    if not number.merchant_id and not number.reseller_id and not number.shared_pool:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Pick an owner: pass merchant_id (merchant-owned) or "
                "reseller_id (umbrella-owned), or set shared_pool=true for "
                "the shared platform pool."
            ),
        )

    merchant_id, reseller_id = await _resolve_ownership(
        number.merchant_id, number.reseller_id
    )

    try:
        if await check_number_purchase_conflict(number.number) is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create telephony number",
            )

        telephony_number = await create_telephony_number(
            id=str(uuid4()),
            number=number.number,
            provider=number.provider,
            status=number.status,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            channels=0,
            maximum_channels=number.maximum_channels,
        )

        if telephony_number:
            logger.info(
                f"Telephony number {number.number} created successfully with ID {telephony_number.id}"
            )
            return telephony_number
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create telephony number",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating telephony number: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating telephony number: {str(e)}",
        )


async def update_number_handler(
    number_id: str, update: UpdateTelephonyNumberRequest, current_user: UserInfo
) -> TelephonyNumber:
    """
    Update a telephony number: assignment (merchant/umbrella), release to the
    shared pool (clear_ownership), status, or maximum_channels.

    Args:
        number_id: Telephony number UUID
        update: Partial update request (None fields = unchanged)
        current_user: Current authenticated user (must be admin)

    Returns:
        Updated telephony number object

    Raises:
        HTTPException: 404 if not found, 400 on invalid ownership
    """
    logger.info(
        f"Admin {current_user.username} updating telephony number {number_id}: "
        f"{update.model_dump(exclude_none=True)}"
    )

    existing = await get_telephony_number_by_id(number_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Telephony number {number_id} not found",
        )

    # Touching either ownership field is a full reassignment: both columns
    # get rewritten so e.g. a reseller-only PATCH on a merchant-owned number
    # nulls the stale merchant_id instead of leaving hybrid ownership behind.
    set_ownership = (
        update.merchant_id is not None or update.reseller_id is not None
    ) and not update.clear_ownership
    merchant_id, reseller_id = (None, None)
    if set_ownership:
        merchant_id, reseller_id = await _resolve_ownership(
            update.merchant_id, update.reseller_id
        )

    try:
        telephony_number = await update_telephony_number(
            number_id,
            status=update.status,
            maximum_channels=update.maximum_channels,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            clear_ownership=update.clear_ownership,
            set_ownership=set_ownership,
        )

        if telephony_number:
            logger.info(f"Telephony number {number_id} updated successfully")
            return telephony_number
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to update telephony number",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating telephony number {number_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating telephony number: {str(e)}",
        )


async def list_numbers_handler(
    provider: Optional[str], status_filter: Optional[str], current_user: UserInfo
) -> List[TelephonyNumber]:
    """
    List all telephony numbers with optional filters.

    Args:
        provider: Optional provider filter
        status_filter: Optional status filter
        current_user: Current authenticated user

    Returns:
        List of telephony numbers
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) listing telephony numbers "
        f"(provider={provider}, status={status_filter})"
    )

    try:
        numbers = await get_all_telephony_numbers()

        # Apply filters
        if provider:
            numbers = [n for n in numbers if n.provider == provider]

        if status_filter:
            numbers = [n for n in numbers if n.status == status_filter]

        logger.info(f"Found {len(numbers)} telephony numbers")
        return numbers

    except Exception as e:
        logger.error(f"Error listing telephony numbers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing telephony numbers: {str(e)}",
        )


async def get_number_handler(number_id: str, current_user: UserInfo) -> TelephonyNumber:
    """
    Get a single telephony number by ID.

    Args:
        number_id: Telephony number UUID
        current_user: Current authenticated user

    Returns:
        Telephony number object

    Raises:
        HTTPException: 404 if not found
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) "
        f"requesting telephony number: {number_id}"
    )

    try:
        number = await get_telephony_number_by_id(number_id)

        if not number:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Telephony number {number_id} not found",
            )

        # Ownership/visibility is enforced by the sole caller via
        # filter_numbers_by_rbac — the single number-visibility rule, which 404s
        # out-of-scope ids (PT-13 IDOR) without leaking existence and, unlike a
        # bare ownership gate, still surfaces numbers a caller's template pins.
        # This handler is a pure fetch, consistent with list_numbers_handler.
        return number

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting telephony number {number_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting telephony number: {str(e)}",
        )


async def delete_number_handler(
    number_id: str, current_user: UserInfo
) -> TelephonyNumber:
    """
    Delete (disable) an telephony number.

    Note: This performs a soft delete by setting status to DISABLED.

    Args:
        number_id: Telephony number UUID
        current_user: Current authenticated user (must be admin)

    Returns:
        Disabled telephony number object

    Raises:
        HTTPException: 404 if not found
    """
    logger.info(
        f"Admin {current_user.username} disabling telephony number: {number_id}"
    )

    try:
        telephony_number = await disable_telephony_number(number_id)

        if telephony_number:
            logger.info(f"Telephony number {number_id} disabled successfully")
            return telephony_number
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Telephony number {number_id} not found",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error disabling telephony number {number_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error disabling telephony number: {str(e)}",
        )
