"""
Database accessor functions for blacklisted numbers.
"""

from typing import List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.blacklisted_numbers import (
    decode_blacklisted_number,
    decode_blacklisted_number_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.blacklisted_numbers import (
    delete_blacklisted_number_query,
    get_all_blacklisted_numbers_query,
    get_blacklisted_number_by_phone_query,
    insert_blacklisted_number_query,
    is_number_blacklisted_query,
    mask_phone,
)
from app.schemas import BlacklistedNumber


async def is_number_blacklisted(
    phone_number: str,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> Optional[bool]:
    """
    Check if a phone number is blocked for this caller: by a global entry, an
    entry for its reseller, or an entry for its merchant. Returns None when
    the check itself failed (DB error).
    """
    if not phone_number:
        return False

    try:
        query_text, values = is_number_blacklisted_query(
            phone_number, reseller_id, merchant_id
        )
        result = await run_parameterized_query(query_text, values)
        return len(result) > 0

    except Exception as e:
        logger.error(f"Error checking blacklist for {mask_phone(phone_number)}: {e}")
        return None


async def add_blacklisted_number(
    id: str,
    phone_number: str,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Optional[BlacklistedNumber]:
    """
    Add a phone number to the blacklist.
    """
    masked = mask_phone(phone_number)
    logger.info(f"Adding {masked} to blacklist (merchant: {reseller_id})")

    try:
        query_text, values = insert_blacklisted_number_query(
            id=id,
            phone_number=phone_number,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            reason=reason,
            created_by=created_by,
        )
        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            decoded = decode_blacklisted_number(result[0])
            logger.info(f"Blacklisted number added: {masked}")
            return decoded

        logger.error("Failed to add blacklisted number")
        return None
    except Exception as e:
        logger.error(f"Error adding blacklisted number: {e}")
        return None


async def remove_blacklisted_number(
    phone_number: str,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> Optional[BlacklistedNumber]:
    """
    Remove a phone number from the blacklist.
    """
    masked = mask_phone(phone_number)
    logger.info(f"Removing {masked} from blacklist (merchant: {reseller_id})")

    try:
        query_text, values = delete_blacklisted_number_query(
            phone_number, reseller_id, merchant_id
        )
        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            decoded = decode_blacklisted_number(result[0])
            logger.info(f"Blacklisted number removed: {masked}")
            return decoded

        logger.error("Blacklisted number not found for removal")
        return None
    except Exception as e:
        logger.error(f"Error removing blacklisted number: {e}")
        return None


async def get_all_blacklisted_numbers(
    reseller_id: Optional[str] = None,
) -> List[BlacklistedNumber]:
    """
    Get all blacklisted numbers with optional reseller filter.
    """
    logger.info(f"Getting all blacklisted numbers (reseller: {reseller_id})")

    try:
        query_text, values = get_all_blacklisted_numbers_query(reseller_id)
        result = await run_parameterized_query(query_text, values)
        if result:
            return decode_blacklisted_number_list(result)
        return []
    except Exception as e:
        logger.error(f"Error getting blacklisted numbers: {e}")
        return []


async def check_blacklisted_number(
    phone_number: str,
) -> List[BlacklistedNumber]:
    """
    Get all blacklist entries for a specific phone number.
    """
    logger.info(f"Checking blacklist entries for {mask_phone(phone_number)}")

    try:
        query_text, values = get_blacklisted_number_by_phone_query(phone_number)
        result = await run_parameterized_query(query_text, values)
        if result:
            return decode_blacklisted_number_list(result)
        return []
    except Exception as e:
        logger.error(f"Error checking blacklisted number: {e}")
        return []
