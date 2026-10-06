"""Ranks inside the one waiting room per template (real Redis).

On a number whose ``bb:num:{N}.ranked`` is 1, a lead that is ready now waits at a score
below zero built from its rank and a time, so ``match`` still takes the front of the
room. A lead waiting for a later time keeps its due time as its score and ``bb:qp:{T}``
remembers its ready score. A number without the flag behaves as before.
"""

from __future__ import annotations

import asyncio
import time
from typing import List

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Enqueue, Rank
from tests.breeze_buddy.dispatch.v2.conftest import (
    claim_next,
    seed_number,
    tickets_of,
)

pytestmark = pytest.mark.asyncio

BAND = 10**13
IST_MS = 19_800_000
DAY_MS = 86_400_000
EVENT = 1_791_522_600_123  # 13 digits: its last digit must survive in a 15-digit score


def NOW() -> int:
    return int(time.time() * 1000)


def pscore(rank, order, ready_ms=0, event_ms=0, live_day=False) -> int:
    """The test's own copy of the ready-score formula."""
    if order == "n":
        t = (BAND - 1) - event_ms
    elif live_day:
        ist = ready_ms + IST_MS
        t = (100_000 - ist // DAY_MS) * 10**8 + ist % DAY_MS
    else:
        t = ready_ms
    return (rank - 100) * BAND + t


def band(score: float) -> int:
    return int(score // BAND) + 100


async def ranked_number(rr, n, max_lines, templates, *, live_day=False) -> None:
    await seed_number(rr, n, max_lines, templates)
    await rr.hset(
        f"bb:num:{n}", mapping={"ranked": "1", "live_day": "1" if live_day else "0"}
    )


async def open_lines(rr, n: str, lines: int) -> List[str]:
    """Give ``n`` ``lines`` lines, match once, and answer the leads in issue order."""
    await rr.hset(f"bb:num:{n}", "max", lines)
    await scripts.match(n)
    return await tickets_of(rr, n)


async def test_unranked_number_is_untouched(rr):
    await seed_number(rr, "N1", 0, {"T1": {}})
    now = NOW()
    assert await scripts.enqueue("T1", "L1", now - 5, rank=Rank(3, "n", now)) == 0
    assert await scripts.enqueue("T1", "L2", now - 9) == 0
    assert await scripts.enqueue("T1", "L3", now + 60_000, rank=Rank(1, "f", 0)) == 0
    assert await rr.zrange("bb:q:T1", 0, -1, withscores=True) == [
        ("L2", now - 9),
        ("L1", now - 5),
        ("L3", now + 60_000),
    ]  # scores are due times, whatever rank was passed
    assert [key async for key in rr.scan_iter("bb:qp:*")] == []
    assert await open_lines(rr, "N1", 5) == ["L2", "L1"]  # due order, as before


async def test_rank_order_is_strict(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}, "T2": {}})
    now = NOW()
    await scripts.enqueue("T1", "A3", now - 9_000, rank=Rank(3, "n", now - 1_000))
    await scripts.enqueue("T2", "B1", now - 1, rank=Rank(1, "f", 0))
    await scripts.enqueue("T1", "C2", now - 1, rank=Rank(2, "n", now - 9_000))
    await scripts.enqueue("T2", "D3", now - 1, rank=Rank(3, "n", now - 500))
    await asyncio.sleep(0.003)  # a later ready time than B1's
    await scripts.enqueue("T1", "E1", now - 1, rank=Rank(1, "f", 0))
    assert await open_lines(rr, "N1", 10) == ["B1", "E1", "C2", "D3", "A3"]


async def test_first_ready_order_inside_rank_1(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}, "T2": {}})
    for i, (lead, template) in enumerate((("L1", "T1"), ("L2", "T2"), ("L3", "T1"))):
        # the last one queued was due the longest ago: what orders them is when each
        # became ready, which is when it was queued
        due = NOW() - 60_000 - i * 5_000
        await scripts.enqueue(template, lead, due, rank=Rank(1, "f", 0))
        await asyncio.sleep(0.003)
    assert await open_lines(rr, "N1", 3) == ["L1", "L2", "L3"]


async def test_newest_event_first_inside_pile(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}, "T2": {}})
    now = NOW()
    await scripts.enqueue("T1", "old", now - 1, rank=Rank(2, "n", now - 9_000_000))
    await scripts.enqueue("T2", "new", now - 1, rank=Rank(2, "n", now - 1_000))
    await scripts.enqueue("T1", "mid", now - 1, rank=Rank(2, "n", now - 5_000_000))
    # a newer event in a lower rank does not jump the rank above it
    await scripts.enqueue("T2", "pile3", now - 1, rank=Rank(3, "n", now))
    assert await open_lines(rr, "N1", 4) == ["new", "mid", "old", "pile3"]


@pytest.mark.parametrize("live_day", [True, False])
async def test_live_day_puts_today_before_yesterday(rr, live_day):
    # Riya became ready two IST days ago at 20:45 and was never called; Kavya a day
    # later at 10:20. Both take the default rank (1), first ready first.
    await ranked_number(rr, "N1", 0, {"T1": {}}, live_day=live_day)
    now = NOW()
    ist_midnight = ((now + IST_MS) // DAY_MS) * DAY_MS - IST_MS
    riya = ist_midnight - 2 * DAY_MS + (20 * 60 + 45) * 60_000
    kavya = ist_midnight - DAY_MS + (10 * 60 + 20) * 60_000
    await rr.zadd("bb:q:T1", {"Riya": riya, "Kavya": kavya})
    await scripts.enqueue("T1", "pile", now - 1, rank=Rank(2, "n", now))
    first_two = ["Kavya", "Riya"] if live_day else ["Riya", "Kavya"]
    assert await open_lines(rr, "N1", 3) == first_two + ["pile"]


@pytest.mark.parametrize("rank", [1, 50, 99])
async def test_scores_are_exact_integers(rr, rank):
    await ranked_number(rr, "N1", 0, {"T1": {}, "T2": {}})
    await ranked_number(rr, "N2", 0, {"T3": {}}, live_day=True)
    before = NOW()
    want = pscore(rank, "n", event_ms=EVENT)  # known to its last digit
    await scripts.enqueue("T1", "N", before - 1, rank=Rank(rank, "n", EVENT))
    assert await rr.zscore("bb:q:T1", "N") == want
    await scripts.enqueue("T1", "W", before + 60_000, rank=Rank(rank, "n", EVENT))
    assert await rr.hget("bb:qp:T1", "W") == str(want)  # as text, while it waits
    await scripts.enqueue("T2", "F", before - 5_000, rank=Rank(rank, "f", 0))
    await scripts.enqueue("T3", "D", before - 5_000, rank=Rank(rank, "f", 0))
    after = NOW()
    plain, day = await rr.zscore("bb:q:T2", "F"), await rr.zscore("bb:q:T3", "D")
    assert pscore(rank, "f", before) <= plain <= pscore(rank, "f", after)
    lo, hi = (pscore(rank, "f", t, live_day=True) for t in (before, after))
    assert lo <= day <= hi or (before + IST_MS) // DAY_MS != (after + IST_MS) // DAY_MS
    for score in (want, plain, day):
        assert score == int(score) and band(score) == rank and score < 0


async def test_out_of_range_values_stay_inside_a_band(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}})
    now = NOW()
    await scripts.enqueue("T1", "big", now - 1, rank=Rank(250, "n", EVENT))
    await scripts.enqueue("T1", "evt", now - 1, rank=Rank(2, "n", 10**15))
    await scripts.enqueue("T1", "past", now - 1, rank=Rank(2, "n", -5))
    assert band(await rr.zscore("bb:q:T1", "big")) == 99  # never a score above zero
    assert await rr.zscore("bb:q:T1", "evt") == pscore(2, "n", event_ms=BAND - 1)
    assert await rr.zscore("bb:q:T1", "past") == pscore(2, "n", event_ms=0)
    assert await scripts.enqueue("T1", "neg", now - 1, rank=Rank(-3, "n", 0)) == -4


async def test_waiting_member_keeps_due_time_and_remembers_rank(rr):
    await ranked_number(rr, "N1", 2, {"T1": {}})
    due = NOW() + 60_000
    assert (
        await scripts.enqueue("T1", "W", due, rank=Rank(1, "f", 0)) == 0
    )  # not issued
    assert await rr.zscore("bb:q:T1", "W") == due  # its due time, as today
    assert await rr.hget("bb:qp:T1", "W") == str(pscore(1, "f", due))
    assert await rr.zscore("bb:due", "N1") == due  # the sweep looks again then


async def test_promote_gives_rank_back_before_the_pick(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}})
    now = NOW()
    await scripts.enqueue("T1", "W", now + 60_000, rank=Rank(1, "f", 0))
    await scripts.enqueue("T1", "pile", now - 5_000, rank=Rank(3, "n", now))
    await rr.zadd("bb:q:T1", {"W": now - 1})  # W's time has come (after the pile's)
    assert await open_lines(rr, "N1", 1) == ["W"]  # rank 1 again, in this same match
    assert await rr.hexists("bb:qp:T1", "W") is False


async def test_promote_is_bounded_and_lists_the_number_due(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}})
    now, cap = NOW(), scripts.PROMOTE_CAP
    await rr.zadd("bb:q:T1", {f"L{i:05d}": now - 60_000 + i for i in range(cap + 7)})
    assert await open_lines(rr, "N1", 1) == ["L00000"]  # the earliest due of them
    assert await rr.zcount("bb:q:T1", 0, "+inf") == 7  # left for the next run
    assert await rr.zscore("bb:due", "N1") <= NOW()  # full, but listed: more to promote


async def test_promote_runs_for_a_closed_window(rr):
    ist = (int(time.time()) + 19800) % 86400
    closed = {"start": str((ist + 3600) % 86400), "end": str((ist + 7200) % 86400)}
    await ranked_number(rr, "N1", 5, {"T1": closed})
    now = NOW()
    await scripts.enqueue("T1", "W", now + 60_000, rank=Rank(2, "n", EVENT))
    await rr.zadd("bb:q:T1", {"W": now - 1})
    assert await scripts.match("N1") == 0  # the window is closed: nothing is issued
    assert await rr.zscore("bb:q:T1", "W") == pscore(2, "n", event_ms=EVENT)


async def test_full_number_skips_promote(rr):
    await ranked_number(rr, "N1", 1, {"T1": {}})
    await rr.sadd("bb:busy:N1", "call:X")
    now = NOW()
    await rr.zadd("bb:q:T1", {"L1": now - 2})
    await rr.config_resetstat()
    assert await scripts.match("N1") == 0
    stats = await rr.info("commandstats")
    assert "cmdstat_zrangebyscore" not in stats and "cmdstat_hmget" not in stats
    assert await rr.zscore("bb:q:T1", "L1") == now - 2


async def test_entry_with_no_rank_on_ranked_number_gets_default(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}, "T2": {}})
    now = NOW()
    # queued before the number was ranked: plain due times
    await rr.zadd("bb:q:T1", {"O1": now - 2_000, "O2": now - 1_000})
    await scripts.enqueue("T1", "pile", now - 1, rank=Rank(3, "n", now))
    # "this lead has no rank" (a pushed lead): the number's default rank
    assert await scripts.enqueue("T2", "pushed", now - 1, rank=Rank(0, "f", 0)) == 0
    assert await open_lines(rr, "N1", 4) == ["O1", "O2", "pushed", "pile"]
    await rr.hset("bb:num:N1", mapping={"max": 0, "default_rank": 7})
    await scripts.enqueue("T2", "later", NOW() - 1, rank=Rank(0, "f", 0))
    await scripts.enqueue("T1", "pile2", NOW() - 1, rank=Rank(3, "n", now))
    await rr.hset("bb:num:N1", "max", 4)
    await scripts.release("N1", "lead:O1")  # one line frees: rank 3 before rank 7
    assert (await tickets_of(rr, "N1"))[-1] == "pile2"


async def test_need_rank_writes_nothing(rr):
    await ranked_number(rr, "N1", 3, {"T1": {}})
    await rr.delete("bb:numtpl:N1")
    now = NOW()
    assert await scripts.enqueue("T1", "L1", now - 1) == Enqueue.NEED_RANK == -4
    # a pod still sending the five-argument call gets the same answer
    old_pod = ["T1", "L2", now - 1, scripts.BB_V2_MATCH_CAP, "0"]
    assert await scripts._run(scripts.ENQUEUE_LUA, old_pod, int) == -4
    assert not await rr.exists("bb:q:T1", "bb:qp:T1", "bb:due", "bb:numtpl:N1")


async def test_only_if_absent_leaves_a_ranked_entry_alone(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}})
    now = NOW()
    await scripts.enqueue("T1", "L1", now - 1, rank=Rank(3, "n", EVENT))
    many = await scripts.enqueue_many(
        [
            ("T1", "L1", now - 1, Rank(1, "f", 0)),
            ("T1", "L2", now - 1, Rank(2, "n", 5)),
        ],
        only_if_absent=True,
    )
    assert many == [0, 0]
    assert await rr.zscore("bb:q:T1", "L1") == pscore(3, "n", event_ms=EVENT)
    assert await rr.zscore("bb:q:T1", "L2") == pscore(2, "n", event_ms=5)
    assert await scripts.enqueue_many([("T1", "L3", now - 1)]) == [Enqueue.NEED_RANK]


async def test_reap_requeue_keeps_rank(rr):
    await ranked_number(rr, "N1", 3, {"T1": {}})
    for lead in ("L1", "L2", "L3"):
        assert await scripts.enqueue("T1", lead, NOW() - 1, rank=Rank(3, "n", EVENT))
    await rr.set("bb:dispatch:enabled", "0")  # keep the re-queued leads in the room
    rank = Rank(3, "n", EVENT)
    tk = (await claim_next("N1") or ("", 0))[1]
    assert await scripts.reap_lease("N1", "L1", tk, "T1", NOW() - 1, rank=rank) == 0
    assert await rr.zscore("bb:q:T1", "L1") == pscore(3, "n", event_ms=EVENT)
    tk = (await claim_next("N1") or ("", 0))[
        1
    ]  # re-queued for later: its due time, rank remembered
    later = NOW() + 30_000
    assert await scripts.reap_lease("N1", "L2", tk, "T1", later, rank=rank) == 0
    assert await rr.zscore("bb:q:T1", "L2") == later
    assert await rr.hget("bb:qp:T1", "L2") == str(pscore(3, "n", event_ms=EVENT))
    tk = (await claim_next("N1") or ("", 0))[1]  # no rank given: its due time, as today
    assert await scripts.reap_lease("N1", "L3", tk, "T1", later) == 0
    assert await rr.zscore("bb:q:T1", "L3") == later
    assert not await rr.hexists("bb:qp:T1", "L3")


async def test_move_room_turns_ready_scores_into_now(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}})
    before = NOW()
    await scripts.enqueue("T1", "ready", before - 1, rank=Rank(1, "f", 0))
    await scripts.enqueue("T1", "waits", before + 60_000, rank=Rank(2, "n", EVENT))
    assert await scripts.move_room_to_schedule("T1") == 2
    moved = dict(await rr.zrange(scripts.SCHEDULE_ZSET, 0, -1, withscores=True))
    assert moved["waits"] == before + 60_000  # a waiting lead keeps its due time
    assert before <= moved["ready"] <= NOW()  # never a negative score in the schedule
    assert not await rr.exists("bb:q:T1", "bb:qp:T1")


async def test_tier_boost_only_reorders_inside_a_rank(rr):
    await ranked_number(rr, "N1", 0, {"TH": {"tier": "high"}, "TN": {}})
    now = NOW()
    await scripts.enqueue("TN", "normal-1", now - 1, rank=Rank(1, "f", 0))
    await asyncio.sleep(0.003)
    await scripts.enqueue("TH", "high-1", now - 1, rank=Rank(1, "f", 0))
    await scripts.enqueue("TH", "high-2", now - 1, rank=Rank(2, "n", now))
    # inside rank 1 the high tier's later lead goes first; its rank 2 never passes rank 1
    assert await open_lines(rr, "N1", 3) == ["high-1", "normal-1", "high-2"]


async def test_ranked_flag_off_again_drains_in_order(rr):
    await ranked_number(rr, "N1", 0, {"T1": {}})
    now = NOW()
    await scripts.enqueue("T1", "pile", now - 1, rank=Rank(2, "n", EVENT))
    await scripts.enqueue("T1", "live", now - 1, rank=Rank(1, "f", 0))
    await rr.hset("bb:num:N1", "ranked", "0")  # the flag is taken off again
    assert await scripts.enqueue("T1", "new", now - 5) == 0  # no rank needed any more
    assert await rr.zscore("bb:q:T1", "new") == now - 5  # a due time again
    assert await open_lines(rr, "N1", 3) == ["live", "pile", "new"]
