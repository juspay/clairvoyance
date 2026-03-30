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
local limit  = tonumber(ARGV[3]) -- reserved for Phase 2 blocking
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)
return count
"""


async def check_outbound_rate_limit(
    customer_mobile_number: str,
    lead_id: str,
) -> Tuple[bool, int]:
    """
    Track an outbound call and check if the rate limit is exceeded.

    Always records the call in the sliding window. Returns whether the limit
    has been exceeded and the count of calls already in the window (before
    this one).

    Returns:
        (exceeded, count_before) — exceeded is True when count_before >= limit.
    """
    if not is_redis_configured():
        return False, 0

    try:
        max_calls = await dyn_cfg.OUTBOUND_RATE_LIMIT_MAX_CALLS()
        window_seconds = await dyn_cfg.OUTBOUND_RATE_LIMIT_WINDOW_SECONDS()

        redis = await get_redis_service()
        key = f"breeze_buddy:outbound_rate_limit:{customer_mobile_number}"
        now = time.time()
        member = f"{now}:{lead_id}"
        count_before = await redis.eval_script(
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


async def process_outbound_rate_limit_alert(
    customer_phone: str,
    lead_id: str,
    reseller_id: str,
) -> None:
    """
    Fire-and-forget: track an outbound call and send a Slack alert if the
    rate limit is exceeded. Callers should wrap this in asyncio.create_task().
    """
    try:
        max_calls = await dyn_cfg.OUTBOUND_RATE_LIMIT_MAX_CALLS()
        window_seconds = await dyn_cfg.OUTBOUND_RATE_LIMIT_WINDOW_SECONDS()

        exceeded, count = await check_outbound_rate_limit(
            customer_mobile_number=customer_phone,
            lead_id=lead_id,
        )
        if exceeded:
            masked_phone = (
                f"***{customer_phone[-4:]}" if len(customer_phone) >= 4 else "***"
            )
            logger.warning(
                f"[OUTBOUND_RATE_LIMIT] Limit exceeded for "
                f"{masked_phone} - {count + 1}/{max_calls} "
                f"calls in {window_seconds}s "
                f"(lead: {lead_id})"
            )
            await slack_alert.send(
                title="Outbound Rate Limit Exceeded",
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
                ],
            )
    except Exception as e:
        logger.warning(f"[OUTBOUND_RATE_LIMIT] Error in background alert task: {e}")
