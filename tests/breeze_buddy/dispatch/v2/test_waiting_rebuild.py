"""Calls that wait for a line with no lead row are put back in their rooms (real Redis).

The CRM's page of parked runs is faked here; ``test_grant_end_to_end.py`` reads the real
one.
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    reconcile as RC,
    scripts,
    sweep as SW,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Rank
from tests.breeze_buddy.dispatch.v2.conftest import use_redis
from tests.breeze_buddy.dispatch.v2.test_grant_scripts import (
    BAND,
    NOW,
    granted,
    intents_number,
    lease,
)

pytestmark = pytest.mark.asyncio


def call(lead: str, run: str, rank: int = 2) -> dict:
    priority = {"rank": rank, "order": "first_ready", "event_ms": 0}
    return {"run_id": run, "lead_id": lead, "template_id": "T1", "priority": priority}


def crm_pages(monkeypatch, *pages) -> list:
    """``waiting_calls_page`` answers ``pages`` in turn; returns the ``after`` of each ask."""
    asked = []

    async def page(after, limit):
        asked.append(after)
        at = after or 0
        return list(pages[at]), (at + 1 if at + 1 < len(pages) else None)

    monkeypatch.setattr(RC, "waiting_calls_page", page)
    monkeypatch.setattr(RC, "_waiting_after", None)
    return asked


async def test_a_waiting_call_missing_from_its_room_goes_back_with_its_run_and_rank(
    rr, monkeypatch
):
    await intents_number(rr, "N1", 0, ranked=True)
    crm_pages(monkeypatch, [call("L1", "R1", rank=2)])
    await RC.requeue_waiting_calls()
    assert math.floor(await rr.zscore("bb:q:T1", "L1") / BAND) + 100 == 2
    assert await rr.hget("bb:qi:T1", "L1") == "R1"


async def test_a_template_whose_route_was_lost_is_resolved_first(rr, monkeypatch):
    await intents_number(rr, "N1", 0, ranked=True)
    route = await rr.hgetall("bb:route:T1")
    await rr.delete("bb:route:T1")

    async def ensure_route(template_id):
        await rr.hset(f"bb:route:{template_id}", mapping=route)

    monkeypatch.setattr(RC, "ensure_route", ensure_route)
    crm_pages(monkeypatch, [call("L1", "R1")])
    await RC.requeue_waiting_calls()
    assert await rr.zscore("bb:q:T1", "L1") is not None


async def test_a_call_already_queued_or_holding_a_line_is_left_as_it_is(
    rr, monkeypatch
):
    await intents_number(rr, "N1", 1, ranked=True)
    live = Rank(1, "f", 0)
    await granted(rr, "L1", "R1", rank=live)  # holds the only line, no lead row yet
    assert await scripts.enqueue("T1", "L2", NOW() - 5, rank=live, run_id="R2") == 0
    before = (
        await rr.zscore("bb:q:T1", "L2"),
        await lease(rr, "N1", "L1"),
        await rr.lrange("bb:grants", 0, -1),
    )
    crm_pages(monkeypatch, [call("L1", "R1", rank=3), call("L2", "R2", rank=3)])
    await RC.requeue_waiting_calls()
    assert before == (
        await rr.zscore("bb:q:T1", "L2"),
        await lease(rr, "N1", "L1"),
        await rr.lrange("bb:grants", 0, -1),
    )
    assert await rr.zrange("bb:q:T1", 0, -1) == ["L2"]


async def test_pages_per_run_are_bounded_and_the_next_run_goes_on(rr, monkeypatch):
    await intents_number(rr, "N1", 0, ranked=True)
    asked = crm_pages(
        monkeypatch, [call("L1", "R1")], [call("L2", "R2")], [call("L3", "R3")]
    )
    await RC.requeue_waiting_calls(page_size=1, max_pages=2)
    assert await rr.zcard("bb:q:T1") == 2 and asked == [None, 1]
    await RC.requeue_waiting_calls(page_size=1, max_pages=2)
    assert await rr.zcard("bb:q:T1") == 3 and asked == [None, 1, 2]
    await RC.requeue_waiting_calls(page_size=1, max_pages=1)
    assert asked[-1] is None  # the last page was reached: from the start again


async def test_the_job_runs_only_while_a_number_takes_calls_with_no_lead_row(
    rr, monkeypatch
):
    use_redis(monkeypatch, rr, SW)
    rebuild = AsyncMock()
    monkeypatch.setattr(SW, "requeue_waiting_calls", rebuild)
    await intents_number(rr, "N1", 1)
    await rr.sadd("bb:v2:active", "N1")
    await rr.hset("bb:num:N1", "intents", "0")
    await SW.waiting_calls_job()
    rebuild.assert_not_awaited()
    await rr.hset("bb:num:N1", "intents", "1")
    await SW.waiting_calls_job()
    rebuild.assert_awaited_once_with()
    assert {job.name: job.every for job in SW.JOBS}["waiting_calls"] == 60
