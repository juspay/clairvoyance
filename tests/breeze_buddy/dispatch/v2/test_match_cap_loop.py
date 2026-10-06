"""BB_V2_MATCH_CAP bounds one script's run time, not a number: a caller whose match hit
the cap runs it again until a run issues less (spec 2026-10-05 §4.8). Without the loop,
5,000 free lines at a window opening fill at 100 a second."""

import time
from datetime import datetime, timezone

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import queue as Q
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio


async def _pile(rr, n_leads: int, max_lines: int) -> None:
    await seed_number(rr, "N1", max_lines, {"T1": {}})
    now = int(time.time() * 1000)
    await rr.zadd("bb:q:T1", {f"L{i}": now - 10_000 + i for i in range(n_leads)})


async def test_match_all_fills_every_free_line_in_one_call(rr):
    await _pile(rr, 2_500, 2_500)
    assert await scripts.match_all("N1") == 2_500
    assert await rr.llen("bb:tickets") == 2_500
    assert await rr.scard("bb:busy:N1") == 2_500


async def test_match_all_stops_at_the_free_lines(rr):
    await _pile(rr, 2_500, 250)
    assert await scripts.match_all("N1") == 250
    assert await rr.zcard("bb:q:T1") == 2_250


async def test_match_all_answers_none_when_its_first_run_fails(rr):
    await rr.set("bb:busy:N1", "not-a-set")  # SCARD fails inside the script
    await _pile(rr, 10, 10)
    assert await scripts.match_all("N1") is None


async def test_an_enqueue_that_hits_the_cap_fills_the_rest(rr, monkeypatch):
    use_redis(monkeypatch, rr)
    # 450 due on 1,000 free lines: the enqueue's own match stops at 100
    await _pile(rr, 450, 1_000)
    assert await Q._schedule_v2("NEW", datetime.now(timezone.utc), "T1") is True
    assert await rr.scard("bb:busy:N1") == 451
