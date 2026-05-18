import asyncio
import hashlib
import time
from typing import Tuple

from app.core.config import dynamic as dyn_cfg
from app.core.logger import logger
from app.services.redis.client import get_redis_service, is_redis_configured
from app.services.slack.alert import slack_alert

OUTBOUND_RATE_LIMIT_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window)
end
return count
"""


def _token_for_phone(phone: str) -> str:
    """Return a deterministic one-way token for a phone number to avoid
    storing raw PII in Redis keys."""
    return hashlib.sha256(phone.encode()).hexdigest()


async def check_outbound_limit_for_number(
    customer_mobile_number: str,
    lead_id: str,
    max_calls: int,
    window_seconds: int,
) -> Tuple[bool, int]:
    """
    Track an outbound call and check if the rate limit is exceeded.

    The Lua script only ZADDs (records the attempt and refreshes the TTL)
    while the count is below the limit; once the limit is reached, blocked
    attempts are NOT recorded so they cannot inflate the sliding window
    further. Returns whether the limit has been exceeded and the count of
    calls already in the window (before this one).

    Returns:
        (exceeded, count_before) — exceeded is True when count_before >= limit.
    """
    if not is_redis_configured():
        return False, 0

    try:
        redis = await get_redis_service()
        token = _token_for_phone(customer_mobile_number)
        key = f"breeze_buddy:outbound_rate_limit:{token}"
        now = time.time()
        member = f"{now}:{lead_id}"
        count_before = await redis.run_script(
            OUTBOUND_RATE_LIMIT_LUA,
            keys=[key],
            args=[
                now,
                window_seconds,
                max_calls,
                member,
            ],
        )
        exceeded = int(count_before) >= max_calls
        return exceeded, int(count_before)
    except Exception as e:
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Redis error: {e}")
        return False, 0


async def check_outbound_rate_limit_and_alert(
    customer_phone: str,
    lead_id: str,
    reseller_id: str,
) -> Tuple[bool, int]:
    """
    Track an outbound call and check if the rate limit is exceeded.

    Returns (allow, defer_seconds):
      - allow=True, defer_seconds=0 — call may proceed.
      - allow=False, defer_seconds>0 — call blocked; the caller should push
        next_attempt_at out by defer_seconds so the dispatcher does not
        immediately re-pick the lead and re-trigger the alert.
      - allow=True, defer_seconds=0 (alert-only mode) — limit exceeded but
        block is disabled; call may proceed without deferral.

    Always sends a Slack alert when the limit is exceeded.

    The block/allow decision is made before the Slack alert is sent so that
    any Slack failure never causes a call to be blocked or allowed
    unexpectedly.
    """
    try:
        block_enabled, max_calls, window_seconds = await asyncio.gather(
            dyn_cfg.OUTBOUND_RATE_LIMIT_BLOCK_ENABLED(),
            dyn_cfg.OUTBOUND_RATE_LIMIT_MAX_CALLS(),
            dyn_cfg.OUTBOUND_RATE_LIMIT_WINDOW_SECONDS(),
        )

        exceeded, count = await check_outbound_limit_for_number(
            customer_mobile_number=customer_phone,
            lead_id=lead_id,
            max_calls=max_calls,
            window_seconds=window_seconds,
        )
        if exceeded:
            masked_phone = (
                f"***{customer_phone[-4:]}" if len(customer_phone) >= 4 else "***"
            )
            action = "BLOCKED" if block_enabled else "ALERT_ONLY"
            logger.warning(
                f"[OUTBOUND_RATE_LIMIT] Limit exceeded for "
                f"{masked_phone} - {count + 1}/{max_calls} "
                f"calls in {window_seconds}s "
                f"(lead: {lead_id}, action: {action})"
            )
            # Determine allow/defer BEFORE sending the Slack alert so that any
            # Slack exception cannot affect the call decision.
            allow = not block_enabled
            defer_seconds = 0 if allow else window_seconds
            try:
                await slack_alert.send(
                    title=f"Outbound Rate Limit Exceeded ({action})",
                    fields=[
                        {"name": "Phone (last 4)", "value": masked_phone},
                        {
                            "name": "Calls in window",
                            "value": f"{count + 1}/{max_calls}",
                        },
                        {
                            "name": "Window",
                            "value": f"{window_seconds}s",
                        },
                        {"name": "Lead ID", "value": lead_id},
                        {"name": "Reseller", "value": reseller_id},
                        {"name": "Action", "value": action},
                    ],
                )
            except Exception as slack_exc:
                logger.warning(f"[OUTBOUND_RATE_LIMIT] Slack alert failed: {slack_exc}")
            return allow, defer_seconds
        return True, 0
    except Exception as e:
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Error in rate limit check: {e}")
        return True, 0
