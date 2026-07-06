"""One-time Loom dashboard launch codes (Redis-backed).

Nautilus mints a code over S2S after it has verified the Shopify session, so the
shop identity is already trusted. Loom later redeems the code exactly once to
obtain a normal session token. Codes are single-use and short-lived, so a leaked
launch URL is worthless (already redeemed or expired).
"""

import json
import secrets
from typing import Optional, Tuple

from app.core.logger import logger
from app.services.redis.client import get_redis_service, is_redis_configured

_KEY_PREFIX = "bb:loom_launch:"
_TTL_SECONDS = 60

# Atomic GET+DEL so a code can only ever be redeemed once, even under concurrent
# requests. The RedisService exposes EVAL via run_script(); there is no getdel.
_GETDEL_LUA = (
    "local v = redis.call('GET', KEYS[1]) "
    "if v then redis.call('DEL', KEYS[1]) end "
    "return v"
)


def _key(code: str) -> str:
    return f"{_KEY_PREFIX}{code}"


async def mint_launch_code(merchant_id: str, target: str) -> str:
    """Create a single-use launch code bound to a merchant + target.

    Raises RuntimeError if Redis is unavailable — minting must not silently
    succeed, or Loom would later fail to redeem.
    """
    if not is_redis_configured():
        raise RuntimeError("Redis is not configured; cannot mint launch codes")

    code = secrets.token_urlsafe(32)
    payload = json.dumps({"merchant_id": merchant_id, "target": target})
    redis = await get_redis_service()
    ok = await redis.set(_key(code), payload, ex=_TTL_SECONDS)
    if not ok:
        raise RuntimeError("Failed to store launch code in Redis")
    return code


async def redeem_launch_code(code: str) -> Optional[Tuple[str, str]]:
    """Atomically consume a launch code.

    Returns (merchant_id, target) on success, or None if the code is unknown,
    expired, or already used.
    """
    if not is_redis_configured() or not code:
        return None

    redis = await get_redis_service()
    raw = await redis.run_script(_GETDEL_LUA, [_key(code)], [])
    if not raw:
        return None

    try:
        data = json.loads(raw)
        return data["merchant_id"], data.get("target", "dashboard")
    except (ValueError, KeyError) as exc:
        logger.warning(f"launch code payload decode failed: {exc}")
        return None
