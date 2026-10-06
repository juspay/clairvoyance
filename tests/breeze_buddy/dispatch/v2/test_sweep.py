"""The 1 s sweep (design card §5; Fable I3): cheap tick, spawned jobs, leader only."""

import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    latch,
    reconcile as RC,
    routes,
    sweep as SW,
)
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis

pytestmark = pytest.mark.asyncio


def NOW() -> int:
    return int(time.time() * 1000)


@pytest.fixture
async def rv(rr, monkeypatch):
    use_redis(monkeypatch, rr, SW, RC, routes)
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "JOBS", ())
    await rr.set("bb:epoch", "x")
    yield rr


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# -- the tick -----------------------------------------------------------------------------


async def test_tick_does_nothing_while_v2_was_never_used(monkeypatch):
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=False))
    client = MagicMock(side_effect=AssertionError("touched"))
    await SW.Sweeper(redis_client=client).tick()
    assert client.method_calls == []


async def test_tick_matches_a_lead_whose_time_arrived(rv):
    await seed_number(rv, "N1", 1, {"T1": {}})
    await rv.zadd("bb:q:T1", {"L1": NOW() - 1})
    await rv.zadd("bb:due", {"N1": NOW() - 1})
    await SW.Sweeper(redis_client=rv).tick()
    assert await rv.smembers("bb:busy:N1") == {"lead:L1"}


async def test_tick_matches_every_due_number_in_one_call(rv, monkeypatch):
    for n in ("N1", "N2", "N3"):
        await seed_number(rv, n, 1, {f"T-{n}": {}})
        await rv.zadd(f"bb:q:T-{n}", {f"{n}-L": NOW() - 1})
        await rv.zadd("bb:due", {n: NOW() - 1})
    calls = []
    real = SW.scripts.match_many

    async def spy(ids):
        calls.append(sorted(ids))
        return await real(ids)

    monkeypatch.setattr(SW.scripts, "match_many", spy)
    await SW.Sweeper(redis_client=rv).tick()
    assert calls == [["N1", "N2", "N3"]]
    for n in ("N1", "N2", "N3"):
        assert await rv.scard(f"bb:busy:{n}") == 1


async def test_tick_fills_every_free_line_of_a_big_number(rv):
    await seed_number(rv, "N1", 1_000, {"T1": {}})
    await rv.zadd("bb:q:T1", {f"L{i}": NOW() - 1 for i in range(1_000)})
    await rv.zadd("bb:due", {"N1": NOW() - 1})
    await SW.Sweeper(redis_client=rv).tick()
    assert await rv.scard("bb:busy:N1") == 1_000  # not 100: the cap loop ran


async def test_tick_never_scans_the_keyspace(rv, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("SCAN on the 1 s path")

    monkeypatch.setattr(rv, "scan_iter", boom)
    monkeypatch.setattr(rv, "scan", boom)
    noop = AsyncMock()
    monkeypatch.setattr(SW, "JOBS", tuple(SW.Job(f"j{n}", n, noop, 5) for n in (1, 5)))
    await seed_number(rv, "N1", 1, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.zadd("bb:due", {"N1": NOW() - 1})
    sw = SW.Sweeper(redis_client=rv)
    for _ in range(10):
        await sw.tick()
    await _settle()
    assert noop.await_count == 12  # 10 + 2


async def test_epoch_missing_recovers_in_the_same_tick_then_refills_rooms(
    rv, monkeypatch
):
    await rv.delete("bb:epoch")
    recover = AsyncMock()
    monkeypatch.setattr(SW, "recover_after_flush", recover)
    backlog = AsyncMock()
    other = AsyncMock()
    monkeypatch.setattr(
        SW, "JOBS", (SW.Job("backlog", 60, backlog, 5), SW.Job("j", 1, other, 5))
    )
    await SW.Sweeper(redis_client=rv).tick()
    await _settle()
    recover.assert_awaited_once()
    backlog.assert_awaited_once()  # rooms refilled now, not in up to 60 s
    other.assert_not_awaited()


async def test_epoch_missing_tells_recovery_how_long_since_the_loss_was_seen(
    rv, monkeypatch
):
    """Fix 1-B: the legacy recovery waits 30 s from the first tick that saw the epoch
    missing; the sweeper keeps that time in memory and starts it again on the next loss.
    The epoch was seen set before it went missing: a loss, not first use."""
    clock = [100.0]
    monkeypatch.setattr(SW, "time", NS(monotonic=lambda: clock[0], time=time.time))
    recover = AsyncMock(side_effect=[False, False, True, False])
    monkeypatch.setattr(SW, "recover_after_flush", recover)
    backlog = AsyncMock()
    monkeypatch.setattr(SW, "JOBS", (SW.Job("backlog", 60, backlog, 5),))
    sw = SW.Sweeper(redis_client=rv)
    await sw.tick()  # bb:epoch is set (fixture): seen
    await rv.delete("bb:epoch")  # the loss
    for t in (100.0, 115.0, 130.0):
        clock[0] = t
        await sw.tick()
        await _settle()
        if t < 130.0:
            backlog.assert_not_awaited()  # rooms only once the epoch is back
    assert [c.kwargs for c in recover.await_args_list] == [
        {"lost_for_ms": 0, "first_use": False},
        {"lost_for_ms": 15_000, "first_use": False},
        {"lost_for_ms": 30_000, "first_use": False},
    ]
    backlog.assert_awaited_once()
    clock[0] = 500.0  # a later loss starts its own wait
    await sw.tick()
    assert recover.await_args_list[-1].kwargs["lost_for_ms"] == 0


async def test_a_sweeper_that_stops_leading_forgets_when_it_saw_the_loss(
    rv, monkeypatch
):
    """Should it lead again later, a stale time must not cut a new loss's 30 s short."""
    sw = SW.Sweeper(redis_client=rv)
    sw._lost_since = 1.0
    sw._leader._is_leader = False

    async def one_loop():
        sw._stopping.set()

    monkeypatch.setattr(sw._leader, "start", one_loop)
    monkeypatch.setattr(SW, "check_sweep_leader", AsyncMock())
    await asyncio.wait_for(sw._loop(), timeout=2)
    assert sw._lost_since is None


async def test_every_pod_learns_the_epoch_not_only_the_leader(rv, monkeypatch):
    """A pod whose workers only meet v2 numbers never reads bb:epoch on the dial path; its
    sweeper loop reads it until seen once, so a later loss holds that pod too."""
    sw = SW.Sweeper(redis_client=rv)
    sw._leader._is_leader = False

    async def one_loop():
        sw._stopping.set()

    monkeypatch.setattr(sw._leader, "start", one_loop)
    monkeypatch.setattr(SW, "check_sweep_leader", AsyncMock())
    assert not latch.epoch_seen()
    await asyncio.wait_for(sw._loop(), timeout=2)  # bb:epoch is set (fixture)
    assert latch.epoch_seen()


async def test_switch_runs_every_5_ticks_and_every_job_has_a_timeout():
    jobs = {job.name: job for job in SW.JOBS}
    assert jobs["switch"].every == 5
    assert all(0 < job.timeout_s <= job.every * 30 for job in SW.JOBS)


# -- jobs ---------------------------------------------------------------------------------


async def test_jobs_are_spawned_never_awaited_and_never_doubled(rv, monkeypatch):
    gate = asyncio.Event()
    calls = []

    async def slow():
        calls.append(1)
        await gate.wait()

    monkeypatch.setattr(SW, "JOBS", (SW.Job("slow", 1, slow, 5),))
    sw = SW.Sweeper(redis_client=rv)
    await asyncio.wait_for(sw.tick(), timeout=1)  # returns while the job still runs
    await _settle()
    await sw.tick()
    await _settle()
    assert calls == [1]  # still running: no second copy
    gate.set()
    await _settle()
    await sw.tick()
    await _settle()
    assert calls == [1, 1]
    gate.set()


async def test_a_failing_or_slow_job_is_contained(rv, monkeypatch):
    async def bad():
        raise RuntimeError("boom")

    async def hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(
        SW, "JOBS", (SW.Job("bad", 1, bad, 5), SW.Job("hang", 1, hang, 0.01))
    )
    sw = SW.Sweeper(redis_client=rv)
    await sw.tick()
    await asyncio.sleep(0.05)
    assert all(t.done() for t in sw._running.values())  # timed out, not stuck
    await sw.tick()  # and the next tick spawns them again


async def test_after_a_recovery_or_a_failed_tick_the_jobs_that_free_lines_wait(
    rv, monkeypatch
):
    """BB_V2_RECONNECT_GRACE_S (0 = off): what they would read may not be whole yet."""
    ran = {name: AsyncMock() for name in ("ledger", "lease_reaper", "backlog")}
    monkeypatch.setattr(
        SW, "JOBS", tuple(SW.Job(name, 1, fn, 5) for name, fn in ran.items())
    )
    monkeypatch.setattr(SW, "recover_after_flush", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "SWEEP_INTERVAL_S", 0.01)
    assert SW.BB_V2_RECONNECT_GRACE_S == 0  # off by default
    monkeypatch.setattr(SW, "BB_V2_RECONNECT_GRACE_S", 60)

    def counts():
        return [fn.await_count for fn in ran.values()]

    sw = SW.Sweeper(redis_client=rv)
    await sw.tick()
    await _settle()
    assert counts() == [1, 1, 1]  # nothing went wrong: no wait
    await rv.delete("bb:epoch")
    await sw.tick()  # Redis lost its data: recovered (the backlog job is spawned)
    await rv.set("bb:epoch", "x")
    await sw.tick()
    await _settle()
    assert counts() == [1, 1, 3]
    sw._grace_until = 0.0  # the wait is over
    await sw.tick()
    await _settle()
    assert counts() == [2, 2, 4]

    failing = SW.Sweeper(redis_client=rv)
    leader = NS(is_leader=True, start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(failing, "_leader", leader)
    monkeypatch.setattr(failing, "tick", AsyncMock(side_effect=ConnectionError))
    failing.start()
    await asyncio.sleep(0.05)
    await failing.stop()
    assert failing._grace_until > time.monotonic() + 50


async def test_only_the_leader_ticks(monkeypatch):
    monkeypatch.setattr(SW, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(SW, "SWEEP_INTERVAL_S", 0.01)
    sw = SW.Sweeper(redis_client=MagicMock())
    leader = NS(is_leader=False, start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(sw, "_leader", leader)
    tick = AsyncMock()
    monkeypatch.setattr(sw, "tick", tick)
    sw.start()
    await asyncio.sleep(0.05)
    tick.assert_not_awaited()
    leader.is_leader = True
    await asyncio.sleep(0.05)
    await sw.stop()
    assert tick.await_count >= 1
    leader.stop.assert_awaited_once()


async def test_leader_election_waits_until_v2_is_first_used(monkeypatch):
    seen = AsyncMock(return_value=False)
    monkeypatch.setattr(SW, "v2_seen", seen)
    monkeypatch.setattr(SW, "SWEEP_INTERVAL_S", 0.01)
    sw = SW.Sweeper(redis_client=MagicMock())
    leader = NS(is_leader=False, start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(sw, "_leader", leader)
    sw.start()
    await asyncio.sleep(0.05)
    leader.start.assert_not_awaited()  # today's path: no leader key written at all
    seen.return_value = True
    await asyncio.sleep(0.05)
    await sw.stop()
    leader.start.assert_awaited()


# -- refresh jobs -------------------------------------------------------------------------


async def test_enabled_mirror_follows_the_kill_switch_and_never_scans(rv, monkeypatch):
    # Fable M2: match reads today's pause keys itself, so nothing SCANs for them
    monkeypatch.setattr(rv, "scan", AsyncMock(side_effect=AssertionError("SCAN")))
    monkeypatch.setattr(rv, "scan_iter", MagicMock(side_effect=AssertionError("SCAN")))
    monkeypatch.setattr(
        SW.dyn_cfg, "BB_DISPATCH_ENABLED", AsyncMock(return_value=False)
    )
    await SW.refresh_enabled_mirror()
    assert await rv.get("bb:dispatch:enabled") == "0"
    monkeypatch.setattr(SW.dyn_cfg, "BB_DISPATCH_ENABLED", AsyncMock(return_value=True))
    await SW.refresh_enabled_mirror()
    assert await rv.get("bb:dispatch:enabled") == "1"
    assert not await rv.exists("bb:paused_resellers")


async def test_number_facts_refresh_rewrites_max_and_keeps_mode(rv, monkeypatch):
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    row = NS(
        id="N1", status="AVAILABLE", provider="PLIVO", maximum_channels=7, channels=0
    )
    monkeypatch.setattr(
        SW, "get_telephony_numbers_by_ids", AsyncMock(return_value={"N1": row})
    )
    await SW.refresh_number_facts()
    assert await rv.hget("bb:num:N1", "max") == "7"
    assert await rv.hget("bb:num:N1", "mode") == "v2"


async def test_ranked_flag_follows_the_config_for_a_merchants_own_number(
    rv, monkeypatch
):
    rows = {}
    for n, merchant in (("N1", "M1"), ("N2", "M1"), ("N3", None)):
        await seed_number(rv, n, 2, {f"T{n}": {}})
        await rv.sadd("bb:v2:active", n)
        rows[n] = NS(id=n, status="AVAILABLE", provider="PLIVO", maximum_channels=2)
        rows[n].merchant_id = merchant
    monkeypatch.setattr(
        SW, "get_telephony_numbers_by_ids", AsyncMock(return_value=rows)
    )
    listed = AsyncMock(return_value=["N1", "N3"])
    monkeypatch.setattr(SW.dyn_cfg, "BB_V2_RANKED_NUMBERS", listed)

    async def flags():
        return [await rv.hget(f"bb:num:{n}", "ranked") for n in ("N1", "N2", "N3")]

    await SW.refresh_number_facts()
    listed.assert_awaited_once_with(strict=True)
    # N3 is listed but belongs to no merchant: ranks are one merchant's, so never
    assert await flags() == ["1", "0", "0"]
    listed.return_value = []
    await SW.refresh_number_facts()
    assert await flags() == ["0", "0", "0"]


async def test_unreadable_ranked_config_changes_no_flag(rv, monkeypatch):
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.hset("bb:num:N1", "ranked", "1")
    row = NS(id="N1", status="AVAILABLE", provider="PLIVO", maximum_channels=7)
    row.merchant_id = "M1"
    monkeypatch.setattr(
        SW, "get_telephony_numbers_by_ids", AsyncMock(return_value={"N1": row})
    )
    monkeypatch.setattr(
        SW.dyn_cfg, "BB_V2_RANKED_NUMBERS", AsyncMock(side_effect=ConnectionError)
    )
    await SW.refresh_number_facts()
    assert await rv.hmget("bb:num:N1", "ranked", "max") == ["1", "7"]


async def test_intents_flag_follows_its_list_for_a_merchants_own_number(
    rv, monkeypatch
):
    rows = {}
    for n, merchant in (("N1", "M1"), ("N2", "M1"), ("N3", None)):
        await seed_number(rv, n, 2, {f"T{n}": {}})
        await rv.sadd("bb:v2:active", n)
        rows[n] = NS(id=n, status="AVAILABLE", provider="PLIVO", maximum_channels=2)
        rows[n].merchant_id = merchant
    monkeypatch.setattr(
        SW, "get_telephony_numbers_by_ids", AsyncMock(return_value=rows)
    )
    monkeypatch.setattr(
        SW.dyn_cfg, "BB_V2_RANKED_NUMBERS", AsyncMock(side_effect=ConnectionError)
    )
    listed = AsyncMock(return_value=["N1", "N3"])
    monkeypatch.setattr(SW.dyn_cfg, "BB_V2_INTENT_NUMBERS", listed, raising=False)

    async def flags():
        return [await rv.hget(f"bb:num:{n}", "intents") for n in ("N1", "N2", "N3")]

    await SW.refresh_number_facts()
    listed.assert_awaited_once_with(strict=True)
    # N3 is listed but is a shared number (no merchant): never
    assert await flags() == ["1", "0", "0"]
    listed.side_effect = ConnectionError  # unreadable: no flag changes
    await SW.refresh_number_facts()
    assert await flags() == ["1", "0", "0"]
    listed.side_effect, listed.return_value = None, []
    await SW.refresh_number_facts()
    assert await flags() == ["0", "0", "0"]


async def test_a_number_that_takes_calls_with_no_lead_row_is_always_ranked(
    rv, monkeypatch
):
    """Else ENQUEUE would drop, without a word, the rank the CRM passes with the call."""
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
    from tests.breeze_buddy.dispatch.v2.test_rank_scripts import band

    await seed_number(rv, "N1", 0, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    row = NS(id="N1", status="AVAILABLE", provider="PLIVO", maximum_channels=0)
    row.merchant_id = "M1"
    monkeypatch.setattr(
        SW, "get_telephony_numbers_by_ids", AsyncMock(return_value={"N1": row})
    )
    ranked = AsyncMock(side_effect=ConnectionError)
    intents = AsyncMock(return_value=["N1"])
    monkeypatch.setattr(SW.dyn_cfg, "BB_V2_RANKED_NUMBERS", ranked)
    monkeypatch.setattr(SW.dyn_cfg, "BB_V2_INTENT_NUMBERS", intents, raising=False)

    async def flags():
        return await rv.hmget("bb:num:N1", "intents", "ranked", "backfill")

    await SW.refresh_number_facts()
    # even with the ranked list unread; marked for the backfill like any switch-on
    assert await flags() == ["1", "1", "1"]
    rank = scripts.Rank(2, "f", 0)
    assert await scripts.enqueue("T1", "L1", NOW() - 5, rank=rank, run_id="R1") == 0
    assert band(await rv.zscore("bb:q:T1", "L1")) == 2
    # the ranked list is read and does not name it; the intents list is unread
    ranked.side_effect, ranked.return_value = None, []
    intents.side_effect = ConnectionError
    await SW.refresh_number_facts()
    assert (await flags())[:2] == ["1", "1"]
    intents.side_effect, intents.return_value = None, []
    await SW.refresh_number_facts()
    assert (await flags())[:2] == ["0", "0"]


async def test_leads_queued_before_a_number_is_ranked_get_their_rows_rank(
    rv, monkeypatch
):
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
    from app.database.accessor.breeze_buddy.dispatch import LeadDispatchState as St
    from tests.breeze_buddy.dispatch.v2.conftest import tickets_of
    from tests.breeze_buddy.dispatch.v2.test_rank_scripts import band

    await seed_number(rv, "N1", 0, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    now = NOW()
    await rv.zadd(
        "bb:q:T1", {"first": now - 9, "P3": now - 3, "P2": now - 2, "none": now - 1}
    )
    row = NS(id="N1", status="AVAILABLE", provider="PLIVO", maximum_channels=0)
    row.merchant_id = "M1"
    monkeypatch.setattr(
        SW, "get_telephony_numbers_by_ids", AsyncMock(return_value={"N1": row})
    )
    monkeypatch.setattr(
        SW.dyn_cfg, "BB_V2_RANKED_NUMBERS", AsyncMock(return_value=["N1"])
    )
    await SW.refresh_number_facts()
    # ranked, and marked: until the rows' ranks are in, match gives nobody the default
    assert await rv.hmget("bb:num:N1", "ranked", "backfill") == ["1", "1"]
    await rv.hset("bb:num:N1", "max", 1)
    await scripts.match("N1")
    assert await tickets_of(rv, "N1") == ["first"]
    assert await rv.zscore("bb:q:T1", "P3") == now - 3

    def pri(rank):
        return {"rank": rank, "order": "newest_event", "event_ms": now}

    states = {
        "P3": St("BACKLOG", False, "T1", None, pri(3)),
        "P2": St("BACKLOG", False, "T1", None, pri(2)),
        "none": St("BACKLOG", False, "T1", None, None),
    }
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value=states))
    await RC.backfill_ranks()
    assert band(await rv.zscore("bb:q:T1", "P3")) == 3
    assert band(await rv.zscore("bb:q:T1", "P2")) == 2
    assert await rv.zscore("bb:q:T1", "none") == now - 1  # no rank on its row
    assert await rv.hget("bb:num:N1", "backfill") is None
    await SW.refresh_number_facts()
    assert await rv.hget("bb:num:N1", "backfill") is None  # once per switch-on
    await rv.hset("bb:num:N1", "max", 4)
    await scripts.match("N1")
    # "none" takes the default rank, 1
    assert await tickets_of(rv, "N1") == ["first", "none", "P2", "P3"]


async def test_a_queued_lead_with_no_rank_on_its_row_gets_its_runs_rank(
    rv, monkeypatch
):
    from app.database.accessor.breeze_buddy.dispatch import LeadDispatchState as St
    from tests.breeze_buddy.dispatch.v2.test_rank_scripts import band

    await seed_number(rv, "N1", 0, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.hset("bb:num:N1", mapping={"ranked": "1", "backfill": "1"})
    now = NOW()
    await rv.zadd(
        "bb:q:T1", {"row": now - 4, "run": now - 3, "gone": now - 2, "norun": now - 1}
    )
    pri = {"rank": 2, "order": "newest_event", "event_ms": now}
    states = {
        "row": St("BACKLOG", False, "T1", None, pri, "E0"),
        "run": St("BACKLOG", False, "T1", None, None, "E1"),
        "gone": St("BACKLOG", False, "T1", None, None, "E2"),  # its run gives no rank
        "norun": St("BACKLOG", False, "T1", None, None),
    }
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value=states))
    asked = AsyncMock(return_value={"run": {**pri, "rank": 3}})
    monkeypatch.setattr(RC, "ranks_for_leads", asked)
    await RC.backfill_ranks()
    # one ask for the chunk, as (lead id, run id); a lead with a rank on its row not asked
    asked.assert_awaited_once_with([("run", "E1"), ("gone", "E2")])
    assert band(await rv.zscore("bb:q:T1", "row")) == 2
    assert band(await rv.zscore("bb:q:T1", "run")) == 3
    # no run, or a run with no rank: left for the default
    assert await rv.zscore("bb:q:T1", "gone") == now - 2
    assert await rv.zscore("bb:q:T1", "norun") == now - 1


async def test_route_refresh_reresolves_templates_with_waiting_leads(rv, monkeypatch):
    await seed_number(rv, "N1", 2, {"T1": {}, "T2": {}, "T3": {}})
    await seed_number(rv, "N9", 2, {"T9": {}}, mode=None)  # legacy: not refreshed
    await rv.sadd("bb:v2:active", "N1")
    for t in ("T1", "T2", "T9"):
        await rv.zadd(f"bb:q:{t}", {f"L-{t}": NOW() + 60_000})
    inv = AsyncMock()
    monkeypatch.setattr(SW, "invalidate_route", inv)
    await SW.refresh_routes()
    # T3's room is empty: its next lead is routed by the route as it is then
    assert sorted(c.args[0] for c in inv.await_args_list) == ["T1", "T2"]


async def test_the_routes_job_runs_every_routes_refresh_interval():
    job = next(j for j in SW.JOBS if j.name == "routes")
    assert (job.every, job.timeout_s) == (600, 300)  # BB_V2_ROUTES_REFRESH_S default


async def test_channels_mirror_writes_the_db_processing_count_for_v2_numbers_only(
    rv, monkeypatch
):
    """Fable I3: DB ``channels`` is today's gate the moment v2 lets go of a number without
    a hand-back (Redis flush, code rollback), and today's code only moves it by +-1. So the
    mirror writes the DB's own count of calls holding a line (the hand-back's query), never
    the busy list's size, which also counts tickets not dialled yet."""
    await seed_number(rv, "N1", 5, {"T1": {}})
    await seed_number(rv, "N2", 5, {"T2": {}}, mode="draining")
    await rv.sadd("bb:v2:active", "N1", "N2")
    # a ticket (not dialled yet), a live outbound call and a live inbound call
    await rv.sadd("bb:busy:N1", "lead:ticket", "lead:live", "call:in")
    await rv.sadd("bb:busy:N2", "lead:d")
    count = AsyncMock(return_value={"N1": 2, "N2": 1})
    monkeypatch.setattr(SW, "count_processing_by_telephony_number", count)
    put = AsyncMock(return_value=True)
    monkeypatch.setattr(SW, "set_telephony_number_channels", put)
    await SW.write_channels_mirror()
    count.assert_awaited_once_with()  # one query for every number
    put.assert_awaited_once_with("N1", 2)  # draining: the hand-back owns it


async def test_channels_mirror_skips_a_number_handed_back_while_it_ran(rv, monkeypatch):
    # once a hand-back flips the mode it owns DB channels: never overwrite its count
    await seed_number(rv, "N1", 5, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")

    async def count():
        await rv.hset("bb:num:N1", "mode", "legacy")  # the hand-back flips it meanwhile
        return {"N1": 1}

    monkeypatch.setattr(SW, "count_processing_by_telephony_number", count)
    put = AsyncMock()
    monkeypatch.setattr(SW, "set_telephony_number_channels", put)
    await SW.write_channels_mirror()
    put.assert_not_awaited()


async def test_channels_mirror_never_overwrites_a_hand_back_that_finished_mid_loop(
    rv, monkeypatch
):
    """At the 09:00 DB peak one UPDATE per number can take ~170 ms, so with ~30 v2 numbers
    the write loop can outlast a global off's drain + hand-back. A number handed back after
    the mirror read its mode (mode legacy, the hand-back's own count written) must not get
    the mirror's older count on top: today's gate would stay wrong for good (like I3).
    """
    for n in ("N1", "N2"):
        await seed_number(rv, n, 5, {f"T{n}": {}})
        await rv.sadd("bb:v2:active", n)
    monkeypatch.setattr(
        SW,
        "count_processing_by_telephony_number",
        AsyncMock(return_value={"N1": 1, "N2": 4}),
    )
    channels = {}

    async def put(number_id, value):
        channels[number_id] = value
        if number_id == "N1":  # while N1's slow UPDATE runs, N2 is handed back
            await rv.hset("bb:num:N2", "mode", "legacy")
            channels["N2"] = 2  # the hand-back's fresh PROCESSING count
        return True

    monkeypatch.setattr(SW, "set_telephony_number_channels", put)
    await SW.write_channels_mirror()
    assert channels == {"N1": 1, "N2": 2}  # N2 keeps the hand-back's count


async def test_after_a_flush_todays_gate_counts_live_calls_not_v2_tickets(
    rv, monkeypatch
):
    """Fable I3, end to end: N1 on v2 with 2 lines, one live call and one ticket being
    dialled. Redis is flushed (the dynamic config with it, so v2 reads as off) and today's
    path takes the number back with no hand-back. Its DB gate must hold the live call only:
    1 of 2 lines taken, not 2 of 2 (a refused-token storm) and never a stale low."""
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:live", "lead:ticket")
    channels = {}

    async def put(number_id, value):
        channels[number_id] = value
        return True

    monkeypatch.setattr(
        SW, "count_processing_by_telephony_number", AsyncMock(return_value={"N1": 1})
    )
    monkeypatch.setattr(SW, "set_telephony_number_channels", put)
    await SW.write_channels_mirror()
    await rv.flushdb()
    assert await routes.number_mode_or_none("N1") == "legacy"  # today's path owns it
    assert channels == {"N1": 1}


async def test_backlog_job_runs_only_while_a_number_is_v2_accounted(rv, monkeypatch):
    backlog = AsyncMock(return_value=0)
    monkeypatch.setattr(SW, "reconcile_backlog_v2", backlog)
    await SW.backlog_job()
    backlog.assert_not_awaited()
    await rv.sadd("bb:v2:active", "N1")
    await SW.backlog_job()
    backlog.assert_awaited_once()


async def test_the_sweep_leader_task_is_named_for_the_sweep(monkeypatch):
    # Fable M12: the sweep's election ran as a task named "bb-promoter-leader"
    from app.ai.voice.agents.breeze_buddy.dispatch import leader as leader_mod

    monkeypatch.setattr(
        leader_mod, "get_redis_service", AsyncMock(side_effect=RuntimeError("no redis"))
    )
    sweep_leader = SW.Sweeper(redis_client=MagicMock())._leader
    promoter_leader = leader_mod.LeaderElection()
    for election in (sweep_leader, promoter_leader):
        await election.start()
    try:
        names = [e._task.get_name() for e in (sweep_leader, promoter_leader) if e._task]
        assert names == [
            "bb-v2-sweep-leader",
            "bb-promoter-leader",
        ]  # today's unchanged
    finally:
        for election in (sweep_leader, promoter_leader):
            await election.stop()


async def test_a_sweeper_that_loses_the_lead_cancels_its_running_jobs(rv, monkeypatch):
    """Review #1287 finding 3: a deposed leader's switch step (or counter rewrite) must
    not run on next to its successor's; the new leader re-runs every job."""
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await gate.wait()

    monkeypatch.setattr(SW, "JOBS", (SW.Job("slow", 1, slow, 30),))
    sw = SW.Sweeper(redis_client=rv)
    await sw.tick()
    await asyncio.wait_for(started.wait(), timeout=1)
    job = sw._running["slow"]
    sw._leader._is_leader = False  # the lock expired

    async def one_loop():
        sw._stopping.set()

    monkeypatch.setattr(sw._leader, "start", one_loop)
    monkeypatch.setattr(SW, "check_sweep_leader", AsyncMock())
    await asyncio.wait_for(sw._loop(), timeout=2)
    await _settle()
    assert job.done() and not gate.is_set()  # cancelled, not finished
