"""
Redis state for the Daily warm-transfer telephony bridge.

The AI bot (running in a Daily room) writes a bridge flag keyed by the dialed
agent leg's call_sid. When the provider's answer webhook fires for that leg,
the dispatcher looks up the flag and routes the WebSocket to the bridge
handler instead of the standard AI bot path. The handler updates the flag
status as it joins / fails so the AI bot can decide whether to hand off or
inform the LLM that the transfer failed.
"""

import json
import time
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.services.redis.client import get_redis_service

BRIDGE_FLAG_PREFIX = "bridge:"
BRIDGE_FLAG_TTL_SECONDS = 7200  # 2h, matches transfer flag

STATUS_DIALING = "dialing"
STATUS_JOINED = "joined"
STATUS_FAILED = "failed"
STATUS_DISCONNECTED = "disconnected"

# Once a flag reaches one of these states it must not be overwritten.
# The warm-transfer poller and the bridge handler can both write status
# concurrently (timeout-failure vs joined/disconnected); monotonic
# enforcement ensures the first terminal write wins.
_TERMINAL_STATUSES = {STATUS_JOINED, STATUS_FAILED}


def _key(call_sid: str) -> str:
    return f"{BRIDGE_FLAG_PREFIX}{call_sid}"


async def set_bridge_flag(
    call_sid: str,
    room_url: str,
    room_name: str,
    lead_id: str,
    provider: str,
    outbound_number: str,
    agent_phone: str,
    daily_token: str,
    outbound_number_id: Optional[str] = None,
    claimed: bool = False,
    ttl_seconds: int = BRIDGE_FLAG_TTL_SECONDS,
) -> bool:
    """Write the initial bridge flag in `dialing` status.

    ``outbound_number_id`` and ``claimed`` are set when the Daily warm-transfer
    handler picked a number from the pool. The bridge's finally block reads
    these to release the number when the call ends.
    """
    payload = {
        "room_url": room_url,
        "room_name": room_name,
        "lead_id": lead_id,
        "provider": provider,
        "outbound_number": outbound_number,
        "outbound_number_id": outbound_number_id,
        "claimed": claimed,
        "agent_phone": agent_phone,
        "daily_token": daily_token,
        "status": STATUS_DIALING,
        "failure_reason": None,
        "created_at": time.time(),
    }
    redis = await get_redis_service()
    ok = await redis.setex(_key(call_sid), json.dumps(payload), ttl_seconds)
    if ok:
        logger.info(f"[BRIDGE REDIS] Set flag for call {call_sid} (room={room_name})")
    else:
        logger.error(f"[BRIDGE REDIS] Failed to set flag for call {call_sid}")
    return ok


async def get_bridge_flag(call_sid: str) -> Optional[Dict[str, Any]]:
    """Return bridge flag payload or None."""
    redis = await get_redis_service()
    raw = await redis.get(_key(call_sid))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"[BRIDGE REDIS] Invalid JSON for call {call_sid}: {e}")
        return None


async def update_bridge_status(
    call_sid: str,
    status: str,
    failure_reason: Optional[str] = None,
) -> bool:
    """Update status (and optional failure_reason) on an existing flag.

    Monotonic: once the flag is in a terminal state (joined / failed) no
    further status writes are accepted.  This prevents the warm-transfer
    timeout path from overwriting a ``joined`` written by the bridge handler
    (or vice-versa) when both race to write after a near-simultaneous event.
    """
    flag = await get_bridge_flag(call_sid)
    if not flag:
        logger.warning(
            f"[BRIDGE REDIS] update_bridge_status: no flag for call {call_sid}"
        )
        return False

    current = flag.get("status")
    if current in _TERMINAL_STATUSES:
        logger.debug(
            f"[BRIDGE REDIS] Ignoring status={status} for call {call_sid}: "
            f"already terminal ({current})"
        )
        return False

    flag["status"] = status
    if failure_reason is not None:
        flag["failure_reason"] = failure_reason

    redis = await get_redis_service()
    ok = await redis.setex(_key(call_sid), json.dumps(flag), BRIDGE_FLAG_TTL_SECONDS)
    if ok:
        logger.info(
            f"[BRIDGE REDIS] Updated call {call_sid} status={status} "
            f"reason={failure_reason}"
        )
    return ok


async def clear_bridge_flag(call_sid: str) -> bool:
    """Delete the bridge flag."""
    redis = await get_redis_service()
    return await redis.delete(_key(call_sid))
