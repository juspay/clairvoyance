"""
Database accessor functions for blacklisted numbers.
"""

from typing import List, Optional

from app.core.config.static import BLACKLIST_CACHE_TTL
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


def _cache_key(phone_number: str, reseller_id: Optional[str] = None) -> str:
    """Build Redis cache key for blacklist lookup.

    Key includes reseller_id because the DB query checks both per-merchant
    and global entries — the result is specific to a (phone, merchant) pair.
    """
    normalized = normalize_phone_number(phone_number)
    if reseller_id:
        return f"blacklist:{normalized}:{reseller_id}"
    return f"blacklist:{normalized}"


async def is_number_blacklisted(
    phone_number: str,
    reseller_id: Optional[str] = None,
) -> bool:
    """
    Check if a phone number is blacklisted. Uses Redis cache for performance.
    Fails closed (returns True) on DB errors.
    """
    if not phone_number:
        return False

    global_key = _cache_key(phone_number)
    merchant_key = _cache_key(phone_number, reseller_id) if reseller_id else None

    try:
        redis = await get_redis_service()

        # 1️⃣ Check global cache first
        global_cached = await redis.get(global_key)
        if global_cached == "1":
            return True

        # 2️⃣ Only check merchant cache if global cache exists and says "allow" ("0")
        # If global cache is None (miss), merchant cache might be stale (global was just added)
        if global_cached == "0" and merchant_key:
            merchant_cached = await redis.get(merchant_key)
            if merchant_cached is not None:
                return merchant_cached == "1"

    except Exception as e:
        logger.warning(f"Redis cache error for blacklist check: {e}")

    # 3️⃣ Query DB
    try:
        query_text, values = is_number_blacklisted_query(phone_number, reseller_id)
        result = await run_parameterized_query(query_text, values)

        if result is None:
            logger.error(
                f"DB returned None for blacklist check (phone: {mask_phone(phone_number)})"
            )
            return True

        is_blocked = bool(result and result[0]["is_blacklisted"])
        is_global_block = bool(result and result[0].get("reseller_id") is None)

        # 4️⃣ Cache result
        try:
            redis = await get_redis_service()

            cache_key = global_key if is_global_block else merchant_key
            value = "1" if is_blocked else "0"

            if cache_key:
                await redis.setex(cache_key, value, ttl_seconds=BLACKLIST_CACHE_TTL)

        except Exception as e:
            logger.warning(f"Redis cache set error for blacklist: {e}")

        return is_blocked

    except Exception as e:
        logger.error(f"Error checking blacklist for {mask_phone(phone_number)}: {e}")
        return True


async def add_blacklisted_number(
    id: str,
    phone_number: str,
    reseller_id: Optional[str] = None,
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
            reason=reason,
            created_by=created_by,
        )
        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            decoded = decode_blacklisted_number(result[0])
            # Invalidate cache for all merchant contexts
            await _invalidate_cache(phone_number, reseller_id)
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
) -> Optional[BlacklistedNumber]:
    """
    Remove a phone number from the blacklist.
    """
    masked = mask_phone(phone_number)
    logger.info(f"Removing {masked} from blacklist (merchant: {reseller_id})")

    try:
        query_text, values = delete_blacklisted_number_query(phone_number, reseller_id)
        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            decoded = decode_blacklisted_number(result[0])
            # Invalidate cache for all merchant contexts
            await _invalidate_cache(phone_number, reseller_id)
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


async def _invalidate_cache(
    phone_number: str, reseller_id: Optional[str] = None
) -> None:
    """Invalidate Redis cache entries for a phone number.

    With the global-first lookup logic:
    - Merchant change: delete merchant key only
    - Global change: delete global key only (merchant cache is ignored when global is None)
    """
    try:
        redis = await get_redis_service()

        if reseller_id:
            # Merchant-specific change - invalidate only that merchant's key
            await redis.delete(_cache_key(phone_number, reseller_id))
        else:
            # Global change - invalidate only the global key
            # Merchant cache is ignored when global cache is None (see is_number_blacklisted)
            await redis.delete(_cache_key(phone_number))

    except Exception as e:
        logger.warning(f"Redis cache invalidation error: {e}")
