"""
Redis operations for transfer flow.

Domain-specific Redis operations for managing transfer state.
"""

import json
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.services.redis.client import get_redis_service


async def set_transfer_flag(
    call_sid: str,
    reseller_id: str,
    merchant_id: str,
    transfer_number: Optional[str] = None,
    customer_phone_number: Optional[str] = None,
    ttl_seconds: int = 7200,  # 2 hours
) -> bool:
    """
    Set transfer flag in Redis with reseller/merchant context.

    Stores a flag indicating that this call should trigger transfer
    when Exotel callbacks to fetch the dial-up number.

    Args:
        call_sid: Call SID from telephony provider
        reseller_id: Reseller ID
        merchant_id: Merchant ID
        agent_id: Optional pre-locked agent ID
        customer_phone_number: Optional customer phone number for Plivo dial-back
        ttl_seconds: Time to live in seconds (default: 86400 = 1 day)

    Returns:
        True if flag was set, False otherwise
    """
    key = f"transfer:{call_sid}"
    value = json.dumps(
        {
            "reseller_id": reseller_id,
            "merchant_id": merchant_id,
            "transfer_number": transfer_number,
            "customer_phone_number": customer_phone_number,
        }
    )

    redis_service = await get_redis_service()
    success = await redis_service.setex(key, value, ttl_seconds)

    if success:
        logger.info(
            f"[TRANSFER REDIS] Set flag for call {call_sid}: "
            f"reseller={reseller_id}, merchant={merchant_id}, ttl={ttl_seconds}s"
        )
    else:
        logger.error(f"[TRANSFER REDIS] Failed to set flag for call {call_sid}")

    return success


async def get_transfer_flag(call_sid: str) -> Optional[Dict[str, Any]]:
    """
    Get transfer flag from Redis.

    Args:
        call_sid: Call SID from telephony provider

    Returns:
        Dict with reseller_id, merchant_id, agent_id or None if not found
    """
    key = f"transfer:{call_sid}"
    redis_service = await get_redis_service()
    value = await redis_service.get(key)

    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        logger.error(f"[TRANSFER REDIS] Invalid JSON for call {call_sid}: {e}")
        return None


async def clear_transfer_flag(call_sid: str) -> bool:
    """
    Clear transfer flag from Redis.

    Args:
        call_sid: Call SID from telephony provider

    Returns:
        True if flag was cleared, False otherwise
    """
    key = f"transfer:{call_sid}"
    redis_service = await get_redis_service()
    deleted = await redis_service.delete(key)
    if deleted:
        logger.info(f"[TRANSFER REDIS] Cleared flag for call {call_sid}")
    return bool(deleted)


async def clear_transfer_flag_safely(call_sid: str, reason: str) -> bool:
    """Best-effort transfer-flag cleanup for failure/callback paths."""
    try:
        cleared = await clear_transfer_flag(call_sid)
        if not cleared:
            logger.info(
                f"[TRANSFER REDIS] No flag to clear for call {call_sid} ({reason})"
            )
        return cleared
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=exc).warning(
            f"[TRANSFER REDIS] Failed to clear flag for call {call_sid} " f"({reason})"
        )
        return False


async def acquire_cleanup_lock(call_sid: str, ttl_seconds: int = 300) -> bool:
    """
    Acquire an idempotency lock for transfer cleanup via SETNX.

    Both ``conclude`` and ``conference-end`` callbacks may fire for the same
    call.  This lock ensures only the first one performs resource release.

    Returns True if lock acquired (caller should proceed), False if already held.
    """
    key = f"transfer_cleanup_lock:{call_sid}"
    redis_service = await get_redis_service()
    return await redis_service.set(key, "1", nx=True, ex=ttl_seconds)
