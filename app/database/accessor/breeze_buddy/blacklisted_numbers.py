"""
Database accessor functions for blacklisted numbers.
"""

import os
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
    normalize_phone_number,
)
from app.schemas import BlacklistedNumber
from app.services.redis.client import get_redis_service

BLACKLIST_CACHE_TTL = int(os.getenv("BLACKLIST_CACHE_TTL", "300"))  # Default 5 minutes


def _cache_key(phone_number: str, merchant_id: Optional[str] = None) -> str:
    """Build Redis cache key for blacklist lookup.

    Key includes merchant_id because the DB query checks both per-merchant
    and global entries — the result is specific to a (phone, merchant) pair.
    """
    normalized = normalize_phone_number(phone_number)
    if merchant_id:
        return f"blacklist:{normalized}:{merchant_id}"
    return f"blacklist:{normalized}"


async def is_number_blacklisted(
    phone_number: str,
    merchant_id: Optional[str] = None,
) -> bool:
    """
    Check if a phone number is blacklisted. Uses Redis cache for performance.

    Fails closed (returns True) on DB errors to prevent calling blocked numbers.
    """
    if not phone_number:
        return False

    cache_key = _cache_key(phone_number, merchant_id)

    try:
        redis = await get_redis_service()
        cached = await redis.get(cache_key)
        if cached is not None:
            return cached == "1"
    except Exception as e:
        logger.warning(f"Redis cache error for blacklist check: {e}")

    try:
        query_text, values = is_number_blacklisted_query(phone_number, merchant_id)
        result = await run_parameterized_query(query_text, values)

        if result is None:
            # DB error — fail closed to avoid calling blocked numbers
            logger.error(
                f"DB returned None for blacklist check (phone: {mask_phone(phone_number)}), "
                "failing closed"
            )
            return True

        is_blocked = bool(result and result[0]["is_blacklisted"])

        # Cache the result
        try:
            redis = await get_redis_service()
            await redis.setex(
                key=cache_key,
                value="1" if is_blocked else "0",
                ttl_seconds=BLACKLIST_CACHE_TTL,
            )
        except Exception as e:
            logger.warning(f"Redis cache set error for blacklist: {e}")

        return is_blocked
    except Exception as e:
        logger.error(f"Error checking blacklist for {mask_phone(phone_number)}: {e}")
        # Fail closed — if we can't check, assume blacklisted
        return True


async def add_blacklisted_number(
    id: str,
    phone_number: str,
    merchant_id: Optional[str] = None,
    reason: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Optional[BlacklistedNumber]:
    """
    Add a phone number to the blacklist.
    """
    masked = mask_phone(phone_number)
    logger.info(f"Adding {masked} to blacklist (merchant: {merchant_id})")

    try:
        query_text, values = insert_blacklisted_number_query(
            id=id,
            phone_number=phone_number,
            merchant_id=merchant_id,
            reason=reason,
            created_by=created_by,
        )
        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            decoded = decode_blacklisted_number(result[0])
            # Invalidate cache for all merchant contexts
            await _invalidate_cache(phone_number, merchant_id)
            logger.info(f"Blacklisted number added: {masked}")
            return decoded

        logger.error("Failed to add blacklisted number")
        return None
    except Exception as e:
        logger.error(f"Error adding blacklisted number: {e}")
        return None


async def remove_blacklisted_number(
    phone_number: str,
    merchant_id: Optional[str] = None,
) -> Optional[BlacklistedNumber]:
    """
    Remove a phone number from the blacklist.
    """
    masked = mask_phone(phone_number)
    logger.info(f"Removing {masked} from blacklist (merchant: {merchant_id})")

    try:
        query_text, values = delete_blacklisted_number_query(phone_number, merchant_id)
        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            decoded = decode_blacklisted_number(result[0])
            # Invalidate cache for all merchant contexts
            await _invalidate_cache(phone_number, merchant_id)
            logger.info(f"Blacklisted number removed: {masked}")
            return decoded

        logger.error("Blacklisted number not found for removal")
        return None
    except Exception as e:
        logger.error(f"Error removing blacklisted number: {e}")
        return None


async def get_all_blacklisted_numbers(
    merchant_id: Optional[str] = None,
) -> List[BlacklistedNumber]:
    """
    Get all blacklisted numbers with optional merchant filter.
    """
    logger.info(f"Getting all blacklisted numbers (merchant: {merchant_id})")

    try:
        query_text, values = get_all_blacklisted_numbers_query(merchant_id)
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


async def _invalidate_cache(
    phone_number: str, merchant_id: Optional[str] = None
) -> None:
    """Invalidate Redis cache entries for a phone number."""
    try:
        redis = await get_redis_service()
        # Delete both the specific merchant key and the general key
        await redis.delete(_cache_key(phone_number, merchant_id))
        await redis.delete(_cache_key(phone_number))
    except Exception as e:
        logger.warning(f"Redis cache invalidation error: {e}")
