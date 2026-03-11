"""
Unified inbound call policy enforcement.

Single check function that evaluates all inbound blocking rules
from CallExecutionConfig: master toggle, business hours, blacklist, rate limit.

Also provides helpers for Exotel block-redirect flow:
  - Exotel cannot redirect at HTTP answer-time (only returns WebSocket URL).
  - For REDIRECT action on Exotel, we accept the call, store redirect info in
    Redis, and let the WebSocket agent play the block message then close.
  - Exotel applet detects stream end → /dial-up callback → dials redirect number.
"""

import json
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from app.core.logger import logger
from app.database.accessor import is_number_blacklisted
from app.schemas import CallExecutionConfig, InboundBlockAction
from app.services.redis.client import get_redis_service

DEFAULT_TIMEZONE = "Asia/Kolkata"
BLOCK_REDIRECT_PREFIX = "inbound_block_redirect:"
BLOCK_REDIRECT_TTL = 120  # 2 minutes — enough for WS to connect


@dataclass
class PolicyResult:
    allowed: bool
    action: Optional[InboundBlockAction] = None
    message: Optional[str] = None
    redirect_number: Optional[str] = None
    reason: Optional[str] = None


def _blocked(config: CallExecutionConfig, reason: str) -> PolicyResult:
    return PolicyResult(
        allowed=False,
        action=config.inbound_block_action or InboundBlockAction.REJECT,
        message=config.inbound_block_message,
        redirect_number=config.inbound_redirect_number,
        reason=reason,
    )


def _is_within_business_hours(start: time, end: time, tz_name: Optional[str]) -> bool:
    try:
        tz = ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except (KeyError, Exception):
        logger.warning(
            f"[INBOUND_POLICY] Invalid timezone '{tz_name}', falling back to {DEFAULT_TIMEZONE}"
        )
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    now = datetime.now(tz).time()
    if start <= end:
        return start <= now <= end
    # Overnight range (e.g., 22:00 - 06:00)
    return now >= start or now <= end


async def check_inbound_policy(
    config: CallExecutionConfig,
    caller_number: str,
    skip_rate_limit: bool = False,
) -> PolicyResult:
    """
    Evaluate all inbound blocking rules in order:
    1. Master toggle (enable_inbound)
    2. Business hours
    3. Blacklist
    4. Rate limit (unless skip_rate_limit=True)
    """
    reseller_id = config.reseller_id or config.merchant_id

    # 1. Master toggle
    if not config.enable_inbound:
        logger.info(
            f"[INBOUND_POLICY] Inbound disabled for {reseller_id}/{config.template}"
        )
        return _blocked(config, "inbound_disabled")

    # 2. Business hours
    if config.inbound_call_start_time and config.inbound_call_end_time:
        if not _is_within_business_hours(
            config.inbound_call_start_time,
            config.inbound_call_end_time,
            config.inbound_call_timezone,
        ):
            logger.info(
                f"[INBOUND_POLICY] Outside business hours for {reseller_id}/{config.template}"
            )
            return _blocked(config, "outside_business_hours")

    # 3. Blacklist
    if config.enforce_blacklist:
        if await is_number_blacklisted(caller_number, reseller_id):
            logger.info(
                f"[INBOUND_POLICY] Caller blacklisted for {reseller_id}/{config.template}"
            )
            return _blocked(config, "blacklisted")

    # 4. Rate limit (skip if caller is whitelisted)
    if config.rate_limit_enabled and not skip_rate_limit:
        if config.rate_limit_whitelist and caller_number:
            whitelist = {
                n.strip() for n in config.rate_limit_whitelist.split(",") if n.strip()
            }
            if caller_number in whitelist:
                logger.info(
                    f"[INBOUND_POLICY] Caller ***{caller_number[-4:]} whitelisted from rate limit for {reseller_id}/{config.template}"
                )
                return PolicyResult(allowed=True)
        if config.rate_limit_max_calls and config.rate_limit_window_seconds:
            try:
                redis = await get_redis_service()
                key = f"inbound_rate_limit:{reseller_id}:{config.template}:{caller_number}"
                # SET NX EX ensures the key always has a TTL even if we crash after INCR
                await redis.set(key, "0", nx=True, ex=config.rate_limit_window_seconds)
                count = await redis.incr(key)
                if count > config.rate_limit_max_calls:
                    logger.info(
                        f"[INBOUND_POLICY] Rate limit exceeded for {reseller_id}/{config.template} "
                        f"({count}/{config.rate_limit_max_calls})"
                    )
                    return _blocked(config, "rate_limit_exceeded")
            except Exception as e:
                # Fail open on Redis errors for rate limiting
                logger.warning(
                    f"[INBOUND_POLICY] Redis error during rate limit check: {e}"
                )

    return PolicyResult(allowed=True)


async def set_block_redirect(
    call_sid: str,
    redirect_number: str,
    message: Optional[str] = None,
    reseller_id: Optional[str] = None,
    merchant_identifier: Optional[str] = None,
) -> bool:
    """
    Store block info in Redis for Exotel answer-time blocking.

    Used for both REDIRECT and REJECT on Exotel (which only accepts WS URLs
    at answer-time). The WS agent checks this key on connect and, if present,
    plays the block message then closes the WS. For REDIRECT (redirect_number
    is non-empty), it also sets a transfer flag so Exotel /dial-up callback
    dials the redirect number. For REJECT (redirect_number is empty), /dial-up
    returns 404 and the call simply ends.
    """
    try:
        redis = await get_redis_service()
        key = f"{BLOCK_REDIRECT_PREFIX}{call_sid}"
        value = json.dumps(
            {
                "redirect_number": redirect_number,
                "message": message,
                "reseller_id": reseller_id,
                "merchant_identifier": merchant_identifier,
            }
        )
        await redis.setex(key, value, BLOCK_REDIRECT_TTL)
        logger.info(f"[INBOUND_POLICY] Block redirect set for call {call_sid}")
        return True
    except Exception as e:
        logger.error(f"[INBOUND_POLICY] Failed to set block redirect: {e}")
        return False


async def get_block_redirect(call_sid: str) -> Optional[Dict[str, Any]]:
    """
    Check if a block-redirect was set for this call at answer-time.

    Returns the redirect info dict or None.
    Deletes the key after reading (one-shot).
    """
    try:
        redis = await get_redis_service()
        key = f"{BLOCK_REDIRECT_PREFIX}{call_sid}"
        value = await redis.get(key)
        if not value:
            return None
        await redis.delete(key)
        return json.loads(value)
    except Exception as e:
        logger.error(f"[INBOUND_POLICY] Failed to get block redirect: {e}")
        return None
