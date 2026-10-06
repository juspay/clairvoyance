"""The v2 dial's answer to Plivo's 429 (spec 2026-10-05 §10.6): the same request again
after a wait that doubles from 1-2 s up to 8 s, with jitter (or Retry-After), for at most
60 s, then "not placed" once. Nothing but the request runs per 429. Time is a fake clock
moved by the fake waits."""

import asyncio
from typing import Any, List, Optional

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import throttle as T

pytestmark = pytest.mark.asyncio

PLACED = {"status": "call_initiated", "sid": "RU-1"}


def _throttled(retry_after_s: Optional[float] = None) -> dict:
    return {"status": "throttled", "sid": None, "retry_after_s": retry_after_s}


class Clock:
    """Fake time for the loop: each wait advances it and answers "stopping" once it
    reaches ``stop_at``."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: List[float] = []
        self.stop_at: Optional[float] = None

    def __call__(self) -> float:
        return self.now

    async def wait(self, stopping: asyncio.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        return self.stop_at is not None and self.now >= self.stop_at


class Dial:
    """One provider request per call: the scripted answers, then 429 for ever."""

    def __init__(self, clock: Clock, *replies: Any) -> None:
        self.clock = clock
        self.replies = list(replies)
        self.sent: List[float] = []

    async def __call__(self) -> Optional[dict]:
        self.sent.append(self.clock.now)
        return self.replies.pop(0) if self.replies else _throttled()


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(T, "BB_V2_THROTTLE_WAIT_MIN_S", 1.0)
    monkeypatch.setattr(T, "BB_V2_THROTTLE_MAX_STEP_S", 8.0)
    monkeypatch.setattr(T, "BB_V2_THROTTLE_MAX_WAIT_S", 60.0)
    return c


async def _run(dial: Dial) -> Optional[dict]:
    throttle = T.Throttle(clock=dial.clock, wait=dial.clock.wait)
    return await throttle.dial_until_not_throttled(dial, "L1", asyncio.Event())


async def test_an_answer_that_is_not_a_429_is_returned_after_one_request(clock):
    for reply in (PLACED, None, {"status": "unknown", "sid": None}):
        dial = Dial(clock, reply)
        assert await _run(dial) == reply
        assert len(dial.sent) == 1 and clock.waits == []


async def test_a_429_is_sent_again_after_a_jittered_wait_until_plivo_accepts(clock):
    dial = Dial(clock, _throttled(), _throttled(), PLACED)
    assert await _run(dial) == PLACED
    assert len(dial.sent) == 3
    assert 1.0 <= clock.waits[0] <= 2.0 and 1.0 <= clock.waits[1] <= 4.0


async def test_the_waits_double_up_to_the_cap(clock, monkeypatch):
    # the upper bound of each jittered wait: 2, 4, 8, then 8 (BB_V2_THROTTLE_MAX_STEP_S)
    monkeypatch.setattr(T.random, "uniform", lambda low, high: high)
    dial = Dial(clock)
    assert await _run(dial) is None
    assert clock.waits[:5] == [2.0, 4.0, 8.0, 8.0, 8.0]
    assert max(clock.waits) == 8.0


async def test_a_429_storm_gives_up_once_within_60_s_with_few_requests(clock):
    # Phase 1 gate (b13, Plivo at 60/s): a fixed 1-2 s retry sent ~11 requests per
    # placed dial and filled the 400-thread pool; doubling waits keep it to a handful
    dial = Dial(clock)  # 429 for ever
    assert await _run(dial) is None  # today's not-placed path, once
    assert clock.now <= 60  # never past the limit
    assert len(dial.sent) <= 16  # at least 1 s per wait, at most 8 s from the 3rd on


async def test_a_retry_after_longer_than_the_jitter_is_honoured(clock):
    dial = Dial(clock, _throttled(5.0), PLACED)
    assert await _run(dial) == PLACED
    assert clock.waits == [5.0]


async def test_a_retry_after_past_the_limit_gives_up_without_waiting(clock):
    dial = Dial(clock, _throttled(90.0))
    assert await _run(dial) is None
    assert len(dial.sent) == 1 and clock.waits == []


async def test_a_stopping_pod_stops_re_sending_at_once(clock, monkeypatch):
    monkeypatch.setattr(T.random, "uniform", lambda low, high: 2.0)  # every wait 2 s
    clock.stop_at = 2.5  # the pod starts stopping during the second wait
    dial = Dial(clock)
    assert await _run(dial) is None
    assert len(dial.sent) == 2 and len(clock.waits) == 2


async def test_the_real_wait_ends_when_the_pod_stops():
    stopping = asyncio.Event()
    asyncio.get_running_loop().call_soon(stopping.set)
    assert await T._stopped_within(stopping, 30.0) is True
    assert await T._stopped_within(asyncio.Event(), 0.0) is False
