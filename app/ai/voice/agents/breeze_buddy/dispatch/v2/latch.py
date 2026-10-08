"""Has v2 ever been in use in this process? (design card rule 23)

Until v2 is first enabled, or any number is in a v2-accounted mode, every v2 hook on
today's code paths is a no-op, so today's dialler runs exactly as before. The answer is
re-checked at most every ``REFRESH_S`` seconds and, once true, stays true for the life of
the process: after that, callers read each number's mode instead.

Also per process: has it seen ``bb:epoch`` set? Only then is a missing epoch a Redis loss
(``epoch_lost``); before, it is first use.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k
from app.core.config import dynamic as dyn_cfg
from app.core.logger import logger
from app.services.redis import get_redis_service

REFRESH_S = 5.0

_seen: bool = False
_checked_at: float = float("-inf")
_epoch_seen: bool = False
_check_lock = asyncio.Lock()


_CHECK_TIMEOUT_S = 1.0


async def _read_in_use() -> bool:
    if await dyn_cfg.BB_DISPATCH_V2_ENABLED():
        return True
    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())
    return bool(await client.scard(k.V2_ACTIVE_KEY))


async def v2_seen() -> bool:
    global _seen, _checked_at
    if _seen:
        return True
    if time.monotonic() - _checked_at < REFRESH_S:
        return False
    # One check at a time: callers that arrive while a check is reading wait for its
    # answer. Marking "checked" before the answer was known sent every caller of that
    # moment down today's path (49 of 50 concurrent schedule_lead calls at a pod start,
    # measured on the load-test node), including v2_owns_number's dial guard.
    async with _check_lock:
        if _seen:
            return True
        if time.monotonic() - _checked_at < REFRESH_S:
            return False  # a check finished meanwhile: its answer stands
        try:
            # bounded: callers are today's paths too (a hung Redis must not stall them)
            _seen = await asyncio.wait_for(_read_in_use(), timeout=_CHECK_TIMEOUT_S)
        except (
            Exception
        ) as e:  # noqa: BLE001 — stay on today's path, re-check next window
            logger.warning(f"v2 latch check failed: {e}")
        _checked_at = time.monotonic()
    return _seen


def epoch_lost(present: bool) -> bool:
    """Note one read of ``bb:epoch``. True when it is missing although this process has
    seen it set: Redis lost v2's state (a restart, failover or flush). Missing before this
    process ever saw it is first use: v2 never ran, so today's counters are its own. A
    process started after a loss can't tell the two apart."""
    global _epoch_seen
    _epoch_seen = _epoch_seen or present
    return _epoch_seen and not present


def epoch_seen() -> bool:
    return _epoch_seen


def _reset_for_tests() -> None:
    global _seen, _checked_at, _epoch_seen, _check_lock
    _seen = False
    _checked_at = float("-inf")
    _epoch_seen = False
    _check_lock = asyncio.Lock()
