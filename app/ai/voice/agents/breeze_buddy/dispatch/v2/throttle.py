"""Plivo's rate limit on the v2 dial (spec 2026-10-05 §10.6, design card rule 51).

A 429 proves Plivo placed nothing, so the same request (same dial_ref, same dial row) is
sent again after a jittered wait whose upper bound doubles per resend, from twice
BB_V2_THROTTLE_WAIT_MIN_S up to BB_V2_THROTTLE_MAX_STEP_S (or Retry-After, if longer), for
at most BB_V2_THROTTLE_MAX_WAIT_S. The doubling keeps a long storm of 429s to a handful of
requests per dial instead of one every second or two, each holding a thread. The coroutine keeps its line, lock and
lease the whole time and writes nothing per 429. After the limit, or as soon as the pod
is stopping, the answer is None: today's not-placed path, once.

Not a pacer and not a cap: a dial that is never refused never waits. The learning pacer
and the share of Plivo's budget kept for live calls (decision D11) wait until prod shows
429s (none in the dialler pods, 1-5 Oct).
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    DIAL_OUTCOME_THROTTLED,
)
from app.core.config.static import (
    BB_V2_THROTTLE_MAX_STEP_S,
    BB_V2_THROTTLE_MAX_WAIT_S,
    BB_V2_THROTTLE_WAIT_MIN_S,
)
from app.core.logger import logger

DialAttempt = Callable[[], Awaitable[Optional[Dict[str, Any]]]]


async def _stopped_within(stopping: asyncio.Event, seconds: float) -> bool:
    """Wait ``seconds``; True as soon as ``stopping`` is set."""
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def _wait_s(resend: int, retry_after_s: Optional[float]) -> float:
    """The wait before resend number ``resend`` (0 = the first): full jitter between the
    base and an upper bound that doubles each time, capped; Retry-After wins if longer.
    """
    upper = min(
        BB_V2_THROTTLE_MAX_STEP_S, BB_V2_THROTTLE_WAIT_MIN_S * 2 ** min(resend + 1, 30)
    )
    return max(random.uniform(BB_V2_THROTTLE_WAIT_MIN_S, upper), retry_after_s or 0.0)


class Throttle:
    """The 429 loop. Its clock and its wait are passed in (tests give fakes), as
    ``TTLMemo`` takes its clock; the dialler keeps one per pod."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[asyncio.Event, float], Awaitable[bool]] = _stopped_within,
    ) -> None:
        self._clock = clock
        self._wait = wait

    async def dial_until_not_throttled(
        self, dial: DialAttempt, lead_id: str, stopping: asyncio.Event
    ) -> Optional[Dict[str, Any]]:
        """Run ``dial`` (one provider request) until its answer is not a 429 and return
        that answer. None after BB_V2_THROTTLE_MAX_WAIT_S of 429s, or once ``stopping``
        is set (both: the last answer was a 429, so nothing was placed)."""
        deadline = self._clock() + BB_V2_THROTTLE_MAX_WAIT_S
        requests = 0
        while True:
            call = await dial()
            requests += 1
            if not call or call.get("status") != DIAL_OUTCOME_THROTTLED:
                return call
            wait_s = _wait_s(requests - 1, call.get("retry_after_s"))
            if self._clock() + wait_s > deadline:
                why = f"for {BB_V2_THROTTLE_MAX_WAIT_S:g} s"
            elif await self._wait(stopping, wait_s):
                why = "while the pod is stopping"
            else:
                continue
            logger.warning(
                f"v2 dial for lead {lead_id}: Plivo still refusing after {requests} "
                f"requests ({why}): not placed"
            )
            return None
