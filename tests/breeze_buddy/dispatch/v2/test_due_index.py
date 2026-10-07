"""bb:due: when match can next issue on each number (design card rule 55). The 1 s sweep
matches only the numbers due now, so every write that can make a number dialable must
list it, and match keeps its own number's entry exact."""

import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    reconcile as RC,
    routes,
    scripts,
    sweep as SW,
)
from tests.breeze_buddy.dispatch.v2.conftest import claim_next, seed_number, use_redis


def NOW() -> int:
    return int(time.time() * 1000)


def _ist_sec() -> int:
    return (int(time.time()) + 19800) % 86400


async def _due(r, n: str):
    return await r.zscore("bb:due", n)


# -- match keeps its own entry --------------------------------------------------------------


async def test_leads_due_later_list_the_number_for_then(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    due = NOW() + 60_000
    assert await scripts.enqueue("T1", "L1", due) == 0
    assert await _due(rr, "N1") == due


async def test_the_earliest_room_decides(rr):
    await seed_number(rr, "N1", 2, {"T1": {}, "T2": {}})
    await scripts.enqueue("T1", "L1", NOW() + 120_000)
    soon = NOW() + 30_000
    await scripts.enqueue("T2", "L2", soon)
    assert await _due(rr, "N1") == soon


async def test_a_closed_window_lists_the_number_for_its_opening(rr):
    start = (_ist_sec() + 600) % 86400
    end = (start + 3600) % 86400
    await seed_number(rr, "N1", 2, {"T1": {"start": str(start), "end": str(end)}})
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 0
    assert abs(await _due(rr, "N1") - (NOW() + 600_000)) < 2_000


async def test_a_paused_reseller_is_looked_at_again_after_the_recheck(rr):
    await seed_number(rr, "N1", 2, {"T1": {"reseller": "RP"}})
    await rr.set("bb:reseller:paused:RP", "1")  # today's key, removed by hand
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 0
    recheck_ms = scripts.BB_V2_DUE_RECHECK_S * 1000
    assert abs(await _due(rr, "N1") - (NOW() + recheck_ms)) < 1_000


async def test_a_disabled_route_waits_for_the_write_that_enables_it(rr):
    await seed_number(rr, "N1", 2, {"T1": {"enabled": "0"}})
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 0
    assert await _due(rr, "N1") is None  # no timer: routes.py lists it on enable


async def test_a_full_number_leaves_the_index(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    assert await scripts.enqueue("T1", "L1", NOW() - 2) == 1
    assert await scripts.enqueue("T1", "L2", NOW() - 1) == 0
    assert await _due(rr, "N1") is None  # full: the release runs match
    assert await scripts.release("N1", "lead:L1") == [1, 1]  # L2 gets the line
    assert await _due(rr, "N1") is None  # full again


async def test_a_freed_line_lists_the_next_lead_s_time(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 1
    later = NOW() + 60_000
    assert await scripts.enqueue("T1", "L2", later) == 0
    assert await _due(rr, "N1") is None
    assert await scripts.release("N1", "lead:L1") == [1, 0]
    assert await _due(rr, "N1") == later


async def test_a_run_stopped_at_the_cap_stays_due_now(rr):
    await seed_number(rr, "N1", 200, {"T1": {}})
    await rr.zadd("bb:q:T1", {f"L{i}": NOW() - 10 for i in range(150)})
    assert await scripts.match("N1") == scripts.BB_V2_MATCH_CAP
    assert await _due(rr, "N1") <= NOW()


async def test_no_waiting_lead_means_no_entry(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    assert await scripts.enqueue("T1", "L1", NOW() - 1) == 1
    assert await _due(rr, "N1") is None


# -- every other write that can make a number dialable -----------------------------------


async def test_a_pending_number_is_due_when_its_earliest_lead_is(rr):
    await seed_number(rr, "N1", 2, {"T1": {}}, mode="v2_pending")
    first = NOW() + 30_000
    assert await scripts.enqueue("T1", "L1", first) == 0
    assert await scripts.enqueue("T1", "L2", NOW() + 90_000) == 0
    assert await _due(rr, "N1") == first  # a later lead never moves it later


async def test_an_overdue_switching_number_is_looked_at_again_later(rr):
    # Match issues nothing on a v2_pending / draining number, so an overdue
    # entry would stay at the front of every tick's range until the switch flips it
    for n, mode in (("N1", "v2_pending"), ("N2", "draining")):
        await seed_number(rr, n, 2, {f"T{n}": {}}, mode=mode)
        await rr.zadd("bb:due", {n: NOW() - 60_000})
        assert await scripts.match(n) == 0
        recheck_ms = scripts.BB_V2_DUE_RECHECK_S * 1000
        assert await _due(rr, n) >= NOW() + recheck_ms - 2_000
    await seed_number(rr, "N3", 2, {"TN3": {}}, mode="v2_pending")
    assert await scripts.match("N3") == 0
    assert await _due(rr, "N3") is None  # never listed: not added by the look-again


async def test_a_template_now_routed_elsewhere_makes_that_number_due(rr):
    await seed_number(rr, "N1", 2, {"T1": {}})
    await seed_number(rr, "N2", 2, {"T1": {}})  # T1 moved; still listed on N1
    await rr.zadd("bb:q:T1", {"L1": NOW() + 60_000})
    assert await scripts.match("N1") == 0
    assert await _due(rr, "N2") <= NOW()  # N2's own match then sets the lead's time


async def test_a_reaped_lead_makes_its_route_s_number_due(rr):
    await seed_number(rr, "N1", 1, {"T1": {}})
    await scripts.enqueue("T1", "L1", NOW() - 1)
    got = await claim_next("N1")
    assert got is not None
    _, tk = got
    await seed_number(rr, "N2", 1, {"T1": {}})  # T1 moved meanwhile
    await rr.srem("bb:numtpl:N1", "T1")  # (so N1's own match does not list N2 too)
    requeue_at = NOW() + 60_000
    assert await scripts.reap_lease("N1", "L1", tk, "T1", requeue_at) == 0
    assert await _due(rr, "N2") == requeue_at


async def test_a_route_write_makes_its_number_due_if_leads_wait(rr, monkeypatch):
    use_redis(monkeypatch, rr)
    await seed_number(rr, "N1", 2, {"T1": {"enabled": "0"}})
    route = routes.Route("T1", "N1", "normal", None, None, True, "R1")
    await routes._write(route, None)
    assert await _due(rr, "N1") is None  # an empty room: nothing to look at
    await rr.zadd("bb:q:T1", {"L1": NOW() - 1})  # waited while the route was disabled
    await routes._write(route, None)  # enabled again
    assert await _due(rr, "N1") <= NOW()


async def test_a_raised_max_makes_the_number_due(rr, monkeypatch):
    use_redis(monkeypatch, rr)
    await seed_number(rr, "N1", 2, {"T1": {}})

    def row(max_lines: int):
        return NS(
            id="N1", status="AVAILABLE", provider="PLIVO", maximum_channels=max_lines
        )

    await routes.refresh_number(row(2))
    await routes.refresh_number(row(1))
    assert await _due(rr, "N1") is None  # same or fewer lines: nothing new to issue
    await routes.refresh_number(row(3))
    assert await _due(rr, "N1") <= NOW()


async def test_the_kill_switch_back_on_makes_every_active_number_due(rr, monkeypatch):
    use_redis(monkeypatch, rr, SW)
    await rr.sadd("bb:v2:active", "N1", "N2")
    await rr.zadd("bb:due", {"N2": NOW() - 60_000})
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(SW.dyn_cfg, "BB_DISPATCH_ENABLED", switch)
    await SW.refresh_enabled_mirror()  # on and on: nothing to do
    assert await _due(rr, "N1") is None
    switch.return_value = False
    await SW.refresh_enabled_mirror()
    switch.return_value = True
    await SW.refresh_enabled_mirror()  # match did nothing while it was off
    assert await _due(rr, "N1") <= NOW()
    assert await _due(rr, "N2") < NOW() - 50_000  # an earlier entry stays


# -- the tick -----------------------------------------------------------------------------


@pytest.fixture
async def sweep(rr, monkeypatch):
    use_redis(monkeypatch, rr, SW, RC)
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "JOBS", ())
    await rr.set("bb:epoch", "x")
    calls = []
    real = scripts.match_many

    async def spy(ids):
        calls.append(sorted(ids))
        return await real(ids)

    monkeypatch.setattr(SW.scripts, "match_many", spy)
    yield rr, calls


async def test_the_tick_matches_only_numbers_due_now(sweep):
    r, calls = sweep
    for i in range(20):
        n = f"LATER{i}"
        await seed_number(r, n, 1, {f"T-{n}": {}})
        assert await scripts.enqueue(f"T-{n}", f"{n}-L", NOW() + 60_000) == 0
        await r.sadd("bb:v2:active", n)
    await seed_number(r, "NOW1", 1, {"T-NOW1": {}})
    await r.zadd("bb:q:T-NOW1", {"L": NOW() - 1})
    await r.zadd("bb:due", {"NOW1": NOW() - 1})
    await SW.Sweeper(redis_client=r).tick()
    assert calls == [["NOW1"]]
    assert await r.scard("bb:busy:NOW1") == 1


async def test_a_tick_matches_at_most_the_batch_earliest_first(sweep, monkeypatch):
    r, calls = sweep
    monkeypatch.setattr(SW, "BB_V2_DUE_BATCH", 2)
    await r.zadd("bb:due", {"A": NOW() - 3, "B": NOW() - 2, "C": NOW() - 1})
    await SW.Sweeper(redis_client=r).tick()
    assert calls == [["A", "B"]]


async def test_switching_numbers_never_crowd_a_due_one_out_of_the_tick(
    sweep, monkeypatch
):
    r, _ = sweep
    monkeypatch.setattr(SW, "BB_V2_DUE_BATCH", 2)
    for i in range(4):
        n = f"P{i}"
        await seed_number(r, n, 1, {f"T-{n}": {}}, mode="v2_pending")
        assert await scripts.enqueue(f"T-{n}", f"{n}-L", NOW() - 10_000 + i) == 0
    await seed_number(r, "V", 1, {"T-V": {}})
    await r.zadd("bb:q:T-V", {"V-L": NOW() - 1})
    await r.zadd("bb:due", {"V": NOW() - 1})
    sw = SW.Sweeper(redis_client=r)
    for _ in range(3):
        await sw.tick()
    assert await r.smembers("bb:busy:V") == {"lead:V-L"}


async def test_the_full_pass_finds_a_number_bb_due_lost(sweep, monkeypatch):
    # the safety net: a missed bb:due write (here: deleted by hand) costs at most one pass
    r, calls = sweep
    await seed_number(r, "N1", 1, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    await r.zadd("bb:q:T1", {"L1": NOW() - 1})
    warn = []
    monkeypatch.setattr(SW.logger, "warning", lambda m, *a, **k: warn.append(m))
    alert = AsyncMock()
    monkeypatch.setattr(SW, "raise_v2_due_write_missed", alert)
    sw = SW.Sweeper(redis_client=r)
    sw._ticks = SW.BB_V2_DUE_FULL_PASS_TICKS - 2
    await sw.tick()
    assert await r.scard("bb:busy:N1") == 0  # not due, not a full pass
    await sw.tick()
    assert calls[-1] == ["N1"]
    assert await r.smembers("bb:busy:N1") == {"lead:L1"}
    assert any("['N1']" in m and "bb:due" in m for m in warn)
    # A missed write is a code bug, and nothing else watches for it now
    alert.assert_awaited_once_with(["N1"])


async def test_a_due_number_past_the_batch_cap_is_not_called_missed(sweep, monkeypatch):
    # More numbers due than BB_V2_DUE_BATCH is not a missed write
    r, _ = sweep
    monkeypatch.setattr(SW, "BB_V2_DUE_BATCH", 2)
    for i, n in enumerate(("A", "B", "C")):
        await seed_number(r, n, 1, {f"T{n}": {}})
        await r.sadd("bb:v2:active", n)
        await r.zadd(f"bb:q:T{n}", {f"{n}-L": NOW() - 1})
        await r.zadd("bb:due", {n: NOW() - 10 + i})  # C is the latest: past the cap
    warn = []
    monkeypatch.setattr(SW.logger, "warning", lambda m, *a, **k: warn.append(m))
    sw = SW.Sweeper(redis_client=r)
    sw._ticks = SW.BB_V2_DUE_FULL_PASS_TICKS - 1
    await sw.tick()
    assert await r.smembers("bb:busy:C") == {"lead:C-L"}  # the full pass took it
    assert warn == []


async def test_a_full_pass_that_finds_nothing_missed_logs_nothing(sweep, monkeypatch):
    r, _ = sweep
    await seed_number(r, "N1", 1, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    await r.zadd("bb:q:T1", {"L1": NOW() - 1})
    await r.zadd("bb:due", {"N1": NOW() - 1})  # listed: the index did its job
    warn = []
    monkeypatch.setattr(SW.logger, "warning", lambda m, *a, **k: warn.append(m))
    alert = AsyncMock()
    monkeypatch.setattr(SW, "raise_v2_due_write_missed", alert)
    sw = SW.Sweeper(redis_client=r)
    sw._ticks = SW.BB_V2_DUE_FULL_PASS_TICKS - 1
    await sw.tick()
    assert await r.smembers("bb:busy:N1") == {"lead:L1"}
    assert warn == []
    alert.assert_not_awaited()


# -- a correct bb:due entry is not a missed write --------------------------------------------
# The entries are written by Redis TIME inside the scripts; the sweep reads the pod's
# clock. An entry a moment ahead of the pod's clock is an index doing its job.


async def _full_pass(r, monkeypatch):
    """One full-pass tick; returns the P1 alert mock."""
    alert = AsyncMock()
    monkeypatch.setattr(SW, "raise_v2_due_write_missed", alert)
    sw = SW.Sweeper(redis_client=r)
    sw._ticks = SW.BB_V2_DUE_FULL_PASS_TICKS - 1
    await sw.tick()
    return alert


async def test_a_pod_clock_behind_redis_is_not_a_missed_write(sweep, monkeypatch):
    r, _ = sweep
    await seed_number(r, "N1", 1, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    assert await scripts.enqueue("T1", "L0", NOW() - 1) == 1  # takes the only line
    await scripts.enqueue("T1", "L1", NOW() - 1)
    await r.srem("bb:busy:N1", "lead:L0")  # the line is free again
    await r.zadd("bb:due", {"N1": NOW()})  # listed as due now, by Redis's clock
    real = time.time
    monkeypatch.setattr(
        SW.time, "time", lambda: real() - 0.05
    )  # the pod is 50 ms behind
    alert = await _full_pass(r, monkeypatch)
    assert await r.sismember("bb:busy:N1", "lead:L1")  # the full pass did issue
    alert.assert_not_awaited()


async def test_a_due_time_inside_the_round_trip_is_not_a_missed_write(
    sweep, monkeypatch
):
    r, _ = sweep
    await seed_number(r, "N1", 1, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    soon = NOW() + 40
    assert await scripts.enqueue("T1", "L1", soon) == 0
    assert await _due(r, "N1") == soon  # the index is exact
    spy = SW.scripts.match_many

    async def slow(ids):  # the round trip takes longer than the lead has left
        await asyncio.sleep(0.08)
        return await spy(ids)

    monkeypatch.setattr(SW.scripts, "match_many", slow)
    alert = await _full_pass(r, monkeypatch)
    assert await r.smembers("bb:busy:N1") == {"lead:L1"}
    alert.assert_not_awaited()


async def test_a_window_that_just_opened_is_not_a_missed_write(sweep, monkeypatch):
    # a closed room lists its number at now + WHOLE seconds to the opening, so the entry
    # lands up to 999 ms after the window really opens
    r, _ = sweep
    await seed_number(r, "N1", 1, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    await r.zadd("bb:q:T1", {"L1": NOW() - 1})
    await r.zadd("bb:due", {"N1": NOW() + 700})
    alert = await _full_pass(r, monkeypatch)
    assert await r.smembers("bb:busy:N1") == {"lead:L1"}
    alert.assert_not_awaited()


async def test_an_entry_far_ahead_on_a_dialable_number_is_a_missed_write(
    sweep, monkeypatch
):
    r, _ = sweep
    await seed_number(r, "N1", 1, {"T1": {}})
    await r.sadd("bb:v2:active", "N1")
    await r.zadd("bb:q:T1", {"L1": NOW() - 1})
    await r.zadd("bb:due", {"N1": NOW() + 60_000})  # a stale entry: nothing woke N1
    alert = await _full_pass(r, monkeypatch)
    assert await r.smembers("bb:busy:N1") == {"lead:L1"}
    alert.assert_awaited_once_with(["N1"])
