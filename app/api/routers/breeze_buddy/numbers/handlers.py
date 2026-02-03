"""
Business logic handlers for outbound number operations.
All handlers perform database operations and enforce business rules.
"""

from typing import List, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor import (
    create_outbound_number,
    disable_outbound_number,
    get_all_outbound_numbers,
    get_outbound_number_by_id,
)
from app.schemas import (
    CreateOutboundNumberRequest,
    OutboundNumber,
    UserInfo,
)


async def create_number_handler(
    number: CreateOutboundNumberRequest, current_user: UserInfo
) -> OutboundNumber:
    """
    Create a new outbound number.

    Args:
        number: Outbound number creation request
        current_user: Current authenticated user (must be admin)

    Returns:
        Created outbound number object

    Raises:
        HTTPException: 400 if creation fails
    """
    logger.info(
        f"Admin {current_user.username} creating outbound number: {number.number}"
    )

    if not number.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant_id is required",
        )

    try:
        outbound_number = await create_outbound_number(
            id=str(uuid4()),
            number=number.number,
            provider=number.provider,
            status=number.status,
            merchant_id=number.merchant_id,
            shop_identifier=number.shop_identifier,
            channels=0,
            maximum_channels=number.maximum_channels,
        )

        if outbound_number:
            logger.info(
                f"Outbound number {number.number} created successfully with ID {outbound_number.id}"
            )
            return outbound_number
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create outbound number",
            )

    except Exception as e:
        logger.error(f"Error creating outbound number: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating outbound number: {str(e)}",
        )


async def list_numbers_handler(
    provider: Optional[str], status_filter: Optional[str], current_user: UserInfo
) -> List[OutboundNumber]:
    """
    List all outbound numbers with optional filters.

    Args:
        provider: Optional provider filter
        status_filter: Optional status filter
        current_user: Current authenticated user

    Returns:
        List of outbound numbers
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) listing outbound numbers "
        f"(provider={provider}, status={status_filter})"
    )

    try:
        numbers = await get_all_outbound_numbers()

        # Apply filters
        if provider:
            numbers = [n for n in numbers if n.provider == provider]

        if status_filter:
            numbers = [n for n in numbers if n.status == status_filter]

        logger.info(f"Found {len(numbers)} outbound numbers")
        return numbers

    except Exception as e:
        logger.error(f"Error listing outbound numbers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing outbound numbers: {str(e)}",
        )


async def get_number_handler(number_id: str, current_user: UserInfo) -> OutboundNumber:
    """
    Get a single outbound number by ID.

    Args:
        number_id: Outbound number UUID
        current_user: Current authenticated user

    Returns:
        Outbound number object

    Raises:
        HTTPException: 404 if not found
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) "
        f"requesting outbound number: {number_id}"
    )

    try:
        number = await get_outbound_number_by_id(number_id)

        if not number:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outbound number {number_id} not found",
            )

        return number

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting outbound number {number_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting outbound number: {str(e)}",
        )


async def delete_number_handler(
    number_id: str, current_user: UserInfo
) -> OutboundNumber:
    """
    Delete (disable) an outbound number.

    Note: This performs a soft delete by setting status to DISABLED.

    Args:
        number_id: Outbound number UUID
        current_user: Current authenticated user (must be admin)

    Returns:
        Disabled outbound number object

    Raises:
        HTTPException: 404 if not found
    """
    logger.info(f"Admin {current_user.username} disabling outbound number: {number_id}")

    try:
        outbound_number = await disable_outbound_number(number_id)

        if outbound_number:
            logger.info(f"Outbound number {number_id} disabled successfully")
            return outbound_number
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Outbound number {number_id} not found",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disabling outbound number {number_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error disabling outbound number: {str(e)}",
        )
