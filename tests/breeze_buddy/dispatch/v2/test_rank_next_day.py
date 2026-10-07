"""A live lead not called by closing is pile the next day (real Redis).

A lead may carry a next-day rank. ``bb:qn:{T}`` remembers its next-day score and
``bb:qnd:{T}`` the IST day it is live on. From the next IST day ``match`` gives it that
score (roll), and a lead queued after its live day goes straight to its next rank.
"""

from __future__ import annotations

import json

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Rank
from tests.breeze_buddy.dispatch.v2.conftest import seed_number
from tests.breeze_buddy.dispatch.v2.test_rank_scripts import (
    DAY_MS,
    IST_MS,
    NOW,
    open_lines,
    pscore,
    ranked_number,
)

pytestmark = pytest.mark.asyncio


def ist_day(ms: int) -> int:
    return (ms + IST_MS) // DAY_MS


async def queued_yesterday(rr, t, lead, ready_ms, next_score) -> None:
    """The keys as ENQUEUE left them yesterday for a rank-1 lead with a next-day rank."""
    await rr.zadd(f"bb:q:{t}", {lead: pscore(1, "f", ready_ms, live_day=True)})
    await rr.hset(f"bb:qn:{t}", lead, str(next_score))
    await rr.zadd(f"bb:qnd:{t}", {lead: ist_day(ready_ms)})


async def test_yesterdays_uncalled_live_lead_takes_its_pile_rank(rr):
    # Riya went live yesterday at 20:45 and was not called. Kavya goes live today. Two
    # KYC pile leads wait, one with a newer event than Riya's and one with an older.
    await ranked_number(rr, "N1", 0, {"T1": {}}, live_day=True)
    now = NOW()
    ist_midnight = ist_day(now) * DAY_MS - IST_MS
    riya_event = ist_midnight - DAY_MS + (20 * 60 + 30) * 60_000
    riya_ready = riya_event + 15 * 60_000
    await queued_yesterday(rr, "T1", "Riya", riya_ready, pscore(2, "n", 0, riya_event))
    await scripts.enqueue("T1", "Kavya", now - 1, rank=Rank(1, "f", now - 9, 3, "n"))
    await scripts.enqueue(
        "T1", "newer", now - 1, rank=Rank(2, "n", riya_event + 3_600_000)
    )
    await scripts.enqueue(
        "T1", "older", now - 1, rank=Rank(2, "n", riya_event - 3_600_000)
    )
    assert await open_lines(rr, "N1", 4) == ["Kavya", "newer", "Riya", "older"]
    assert await rr.hexists("bb:qn:T1", "Riya") is False  # her entry is dropped
    assert await rr.zscore("bb:qnd:T1", "Riya") is None
    assert await rr.zscore("bb:qnd:T1", "Kavya") == ist_day(now)  # today's: untouched


async def test_a_lead_with_no_next_rank_is_not_touched(rr):
    # yesterday's rank-1 lead with no next-day rank stays ahead of the pile, and a
    # remembered lead that has left the room is only dropped
    await ranked_number(rr, "N1", 0, {"T1": {}}, live_day=True)
    now = NOW()
    yesterday = now - DAY_MS
    await rr.zadd("bb:q:T1", {"plain": pscore(1, "f", yesterday, live_day=True)})
    await rr.hset("bb:qn:T1", "gone", str(pscore(2, "n", 0, yesterday)))
    await rr.zadd("bb:qnd:T1", {"gone": ist_day(yesterday)})
    await scripts.enqueue("T1", "pile", now - 1, rank=Rank(2, "n", now))
    assert await open_lines(rr, "N1", 3) == ["plain", "pile"]
    assert await rr.exists("bb:qn:T1", "bb:qnd:T1") == 0


async def test_the_roll_is_bounded_and_the_next_run_continues(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}}, live_day=True)
    now, cap = NOW(), scripts.PROMOTE_CAP
    yesterday = now - DAY_MS
    leads = [f"L{i:05d}" for i in range(cap + 3)]
    ready = {
        lead: pscore(1, "f", yesterday + i, live_day=True)
        for i, lead in enumerate(leads)
    }
    await rr.zadd("bb:q:T1", ready)
    await rr.hset(
        "bb:qn:T1", mapping={lead: str(pscore(2, "n", 0, yesterday)) for lead in leads}
    )
    await rr.zadd("bb:qnd:T1", {lead: ist_day(yesterday) for lead in leads})
    await open_lines(rr, "N1", 1)
    assert await rr.zcard("bb:qnd:T1") == 3  # left for the next run
    assert await rr.zscore("bb:due", "N1") <= NOW()  # full, but listed: more to roll
    await open_lines(rr, "N1", 2)
    assert await rr.zcard("bb:qnd:T1") == 0


async def test_a_waiting_lead_of_an_older_day_is_promoted_at_its_next_rank(rr):
    # still waiting for its time when the day turns: the score promote will give it
    await ranked_number(rr, "N1", 0, {"T1": {}}, live_day=True)
    now = NOW()
    yesterday = now - DAY_MS
    ns = pscore(2, "n", 0, yesterday)
    await rr.zadd("bb:q:T1", {"W": now + 60_000, "pile": pscore(3, "n", 0, now)})
    await rr.hset("bb:qp:T1", "W", str(pscore(1, "f", now + 60_000, live_day=True)))
    await rr.hset("bb:qn:T1", "W", str(ns))
    await rr.zadd("bb:qnd:T1", {"W": ist_day(yesterday)})
    await open_lines(rr, "N1", 1)
    assert await rr.hget("bb:qp:T1", "W") == str(ns)


async def test_enqueue_remembers_the_next_day_score(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}}, live_day=True)
    await seed_number(rr, "N2", 0, {"T2": {}})  # not ranked
    now = NOW()
    live = Rank(1, "f", now - 1_000, 2, "n")
    await scripts.enqueue("T1", "L", now - 1, rank=live)
    await scripts.enqueue("T2", "U", now - 1, rank=live)
    assert await rr.zscore("bb:q:T1", "L") < pscore(2, "n", 0, 0)  # in the rank-1 band
    assert await rr.hget("bb:qn:T1", "L") == str(pscore(2, "n", 0, now - 1_000))
    assert await rr.zscore("bb:qnd:T1", "L") == ist_day(now)
    assert [key async for key in rr.scan_iter("bb:qn*:T2")] == []  # no rank, no trace


async def test_a_lead_queued_after_its_live_day_goes_to_its_next_rank(rr):
    # a re-queue from the row on a later day: the row still says rank 1
    await ranked_number(rr, "N1", 0, {"T1": {}}, live_day=True)
    now = NOW()
    event = now - DAY_MS
    await scripts.enqueue("T1", "L", now - 1, rank=Rank(1, "f", event, 2, "n"))
    assert await rr.zscore("bb:q:T1", "L") == pscore(2, "n", 0, event)
    assert await rr.exists("bb:qn:T1", "bb:qnd:T1") == 0


async def test_the_reaper_requeue_keeps_the_next_day_rank(rr):
    await ranked_number(rr, "N1", 1, {"T1": {}}, live_day=True)
    now = NOW()
    live = Rank(1, "f", now - 1_000, 2, "n")
    await scripts.enqueue("T1", "L", now - 1, rank=Rank(0, "f", 0))  # takes the line
    tk = json.loads(await rr.hget("bb:inflight:N1", "L"))["tk"]
    await scripts.reap_lease("N1", "L", tk, "T1", now + 60_000, rank=live)
    assert await rr.hget("bb:qn:T1", "L") == str(pscore(2, "n", 0, now - 1_000))


async def test_the_row_carries_the_next_day_rank():
    row = {
        "rank": 1,
        "order": "first_ready",
        "event_ms": 7,
        "next_rank": 2,
        "next_order": "newest_event",
    }
    assert scripts.rank_from_priority(row) == Rank(1, "f", 7, 2, "n")
    del row["next_rank"]
    assert scripts.rank_from_priority(row) == Rank(1, "f", 7)
