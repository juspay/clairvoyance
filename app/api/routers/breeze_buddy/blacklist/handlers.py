"""
Business logic handlers for blacklist operations.
"""

from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor import (
    add_blacklisted_number,
    check_blacklisted_number,
    get_all_blacklisted_numbers,
    mask_phone,
    remove_blacklisted_number,
)
from app.schemas import BlacklistedNumber, CreateBlacklistNumberRequest, UserInfo


async def add_blacklist_handler(
    request: CreateBlacklistNumberRequest, current_user: UserInfo
) -> BlacklistedNumber:
    """
    Add a phone number to the blacklist.
    """
    masked = mask_phone(request.phone_number)
    logger.info(f"Admin {current_user.username} adding {masked} to blacklist")

    try:
        result = await add_blacklisted_number(
            id=str(uuid4()),
            phone_number=request.phone_number,
            reseller_id=request.reseller_id,
            reason=request.reason,
            created_by=current_user.username,
        )

        if result:
            return result

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to add number to blacklist. It may already be blacklisted.",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding to blacklist: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while adding to blacklist",
        )


async def list_blacklist_handler(
    reseller_id: Optional[str], current_user: UserInfo
) -> List[BlacklistedNumber]:
    """
    List all blacklisted numbers.
    """
    logger.info(
        f"Admin {current_user.username} listing blacklisted numbers "
        f"(reseller: {reseller_id})"
    )

    try:
        return await get_all_blacklisted_numbers(reseller_id)
    except Exception as e:
        logger.error(f"Error listing blacklisted numbers: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while listing blacklisted numbers",
        )


async def check_blacklist_handler(phone_number: str, current_user: UserInfo) -> Dict:
    """
    Check if a phone number is blacklisted.
    """
    masked = mask_phone(phone_number)
    logger.info(f"Admin {current_user.username} checking blacklist for {masked}")

    try:
        entries = await check_blacklisted_number(phone_number)
        return {
            "phone_number": phone_number,
            "is_blacklisted": len(entries) > 0,
            "entries": [entry.model_dump() for entry in entries],
        }
    except Exception as e:
        logger.error(f"Error checking blacklist: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while checking blacklist",
        )


async def remove_blacklist_handler(
    phone_number: str, reseller_id: Optional[str], current_user: UserInfo
) -> Dict:
    """
    Remove a phone number from the blacklist.
    """
    masked = mask_phone(phone_number)
    logger.info(
        f"Admin {current_user.username} removing {masked} from blacklist "
        f"(reseller: {reseller_id})"
    )

    try:
        result = await remove_blacklisted_number(phone_number, reseller_id)

        if result:
            return {
                "status": "removed",
                "phone_number": phone_number,
                "reseller_id": reseller_id,
                "message": "Number removed from blacklist",
            }

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Blacklist entry not found",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing from blacklist: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while removing from blacklist",
        )
