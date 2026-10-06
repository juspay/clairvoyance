"""v2 safety nets (design card §5, rules 11, 14, 19; Fable M2, M4, M6): backlog, ledger,
lease reaper, seeding helpers and orphan prune. Real Redis; DB accessors mocked."""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import queue as queue_mod
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    reconcile as RC,
    routes,
    scripts,
)
from app.database.accessor.breeze_buddy.dispatch import LeadDispatchState as S
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, tickets_of, use_redis

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


def NOW() -> int:
    return int(time.time() * 1000)


def _lease(tk: int, issued_ms: int, t: str = "T1", dialling_ms=None) -> str:
    """A lease claimed when it was issued, by a coroutine that is gone."""
    lease = {"t": t, "issued_ms": issued_ms, "tk": tk}
    lease.update(owner="dead-pod", claimed_ms=issued_ms)
    if dialling_ms is not None:
        lease["dialling_ms"] = dialling_ms
    return json.dumps(lease)


@pytest.fixture
async def rv(rr, monkeypatch):
    use_redis(monkeypatch, rr, RC, routes)
    RC._backlog_after = None
    RC._missing_last.clear()
    RC._rowless_last.clear()
    # route re-resolves need the DB; tests that care patch their own
    monkeypatch.setattr(RC, "invalidate_route", AsyncMock())
    yield rr


# -- backlog reconciler -------------------------------------------------------------------


async def test_backlog_resumes_where_it_stopped_and_wraps_at_the_end(rv, monkeypatch):
    # PoC issue 6: restarting from the oldest rows every run never reaches a lost lead
    seen = []
    rows = [("A", "T1", WHEN), ("B", "T1", WHEN), ("C", "T1", WHEN)]

    async def page(after, size):
        seen.append(after)
        return [] if after == (WHEN, "C") else rows

    monkeypatch.setattr(RC, "get_due_backlog_page", page)
    monkeypatch.setattr(RC, "_is_v2_template", AsyncMock(return_value=False))
    for _ in range(3):
        await RC.reconcile_backlog_v2(page_size=3, max_pages=1)
    assert seen == [None, (WHEN, "C"), None]


async def test_backlog_schedules_only_v2_rows_and_skips_unreadable(rv, monkeypatch):
    rows = [
        ("L1", "TV", WHEN),
        ("L2", "TL", WHEN),
        ("L3", "TX", WHEN),
        ("L4", None, WHEN),
    ]
    monkeypatch.setattr(RC, "get_due_backlog_page", AsyncMock(side_effect=[rows, []]))
    verdict = {"TV": True, "TL": False, "TX": None}
    monkeypatch.setattr(RC, "_is_v2_template", AsyncMock(side_effect=verdict.get))
    batch = AsyncMock(return_value=1)
    monkeypatch.setattr(RC, "schedule_backlog_v2", batch)
    assert await RC.reconcile_backlog_v2() == 1
    batch.assert_awaited_once_with([("L1", WHEN, "TV")])  # no Python pre-checks


async def test_backlog_enqueue_failure_is_healed_into_the_room(rv, monkeypatch):
    await seed_number(rv, "N1", 0, {"T1": {}})  # full: the lead waits in its room
    due = datetime.now(timezone.utc) - timedelta(seconds=5)
    monkeypatch.setattr(
        RC, "get_due_backlog_page", AsyncMock(side_effect=[[("L1", "T1", due)], []])
    )
    monkeypatch.setattr(RC, "_is_v2_template", AsyncMock(return_value=True))
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    assert await RC.reconcile_backlog_v2() == 1
    assert await rv.zrange("bb:q:T1", 0, -1) == ["L1"]


def _backlog(monkeypatch, *pages) -> None:
    """The DB's due BACKLOG pages (then the end), every template on a v2 number."""
    monkeypatch.setattr(RC, "get_due_backlog_page", AsyncMock(side_effect=[*pages, []]))
    monkeypatch.setattr(RC, "_is_v2_template", AsyncMock(return_value=True))
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))


@pytest.fixture
def round_trips(rv, monkeypatch) -> List[str]:
    """Each round trip to Redis: a single command by its name, a pipeline as "pipeline"."""
    trips: List[str] = []
    real_command, real_pipeline = rv.execute_command, rv.pipeline

    async def command(*args, **kwargs):
        trips.append(str(args[0]).upper())
        return await real_command(*args, **kwargs)

    def pipeline(*args, **kwargs):
        trips.append("pipeline")
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(rv, "execute_command", command)
    monkeypatch.setattr(rv, "pipeline", pipeline)
    return trips


async def test_backlog_sends_one_round_trip_per_page(rv, monkeypatch, round_trips):
    await seed_number(rv, "N1", 0, {"T1": {}})  # full: every lead waits in its room
    due = datetime.now(timezone.utc) - timedelta(seconds=5)
    _backlog(
        monkeypatch, *[[(f"L{p}{i}", "T1", due) for i in range(3)] for p in (1, 2)]
    )
    await rv.script_load(scripts.ENQUEUE_LUA)  # NOSCRIPT would add a round trip
    round_trips.clear()
    assert await RC.reconcile_backlog_v2(page_size=3) == 6
    assert round_trips == ["pipeline", "pipeline"]
    assert await rv.zcard("bb:q:T1") == 6


async def test_backlog_page_leaves_waiting_and_held_leads_and_queues_a_lost_one(
    rv, monkeypatch
):
    await seed_number(rv, "N1", 1, {"T1": {}})
    later = NOW() + 60_000
    await rv.zadd("bb:q:T1", {"WAITING": later})  # deferred: a stale page can't move it
    await rv.sadd("bb:busy:N1", "lead:HELD")  # holds the only line
    due = datetime.now(timezone.utc) - timedelta(seconds=5)
    _backlog(
        monkeypatch, [("WAITING", "T1", due), ("HELD", "T1", due), ("LOST", "T1", due)]
    )
    assert (
        await RC.reconcile_backlog_v2() == 3
    )  # the count as one lead at a time gave it
    assert await rv.zscore("bb:q:T1", "WAITING") == later
    assert (
        await rv.zscore("bb:q:T1", "HELD") is None
    )  # rule 17: its holder re-queues it
    assert await rv.zscore("bb:q:T1", "LOST") is not None


async def test_backlog_page_lead_with_no_route_or_on_todays_number_goes_alone(
    rv, monkeypatch
):
    use_redis(monkeypatch, rv, queue_mod)
    await seed_number(rv, "N2", 1, {"T2": {}}, mode="legacy")  # left v2 since the check
    due = datetime.now(timezone.utc) - timedelta(seconds=5)
    _backlog(monkeypatch, [("L1", "T9", due), ("L2", "T2", due)])
    alone = AsyncMock(return_value=True)
    monkeypatch.setattr(queue_mod, "schedule_lead", alone)
    assert await RC.reconcile_backlog_v2() == 2
    # no route in Redis: the one-lead path resolves it (rule 18)
    alone.assert_awaited_once_with("L1", due, template_id="T9", only_if_absent=True)
    assert await rv.zscore("bb:schedule:leads", "L2") is not None  # today's schedule


# -- ledger check -------------------------------------------------------------------------


async def test_ledger_frees_finished_and_stale_backlog_and_keeps_live(rv, monkeypatch):
    await seed_number(rv, "N1", 9, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd(
        "bb:busy:N1",
        "lead:F",
        "lead:B",
        "lead:K",
        "lead:P",
        "lead:T",
        "call:C1",
        "call:C2",
    )
    await rv.hset("bb:inflight:N1", "T", _lease(1, NOW()))
    states = {
        "F": S("FINISHED", False, "T1", None),
        "B": S("BACKLOG", False, "T1", WHEN),
        "K": S("BACKLOG", True, "T1", WHEN),  # locked: a dispatch owns it (alive)
        "P": S("PROCESSING", True, "T1", None),
        "T": S("BACKLOG", False, "T1", WHEN),  # has a lease: a ticket owns it
    }
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value=states))
    monkeypatch.setattr(
        RC, "get_finished_inbound_calls", AsyncMock(return_value={"C1"})
    )
    monkeypatch.setattr(
        RC, "get_known_inbound_calls", AsyncMock(return_value={"C1", "C2"})
    )
    monkeypatch.setattr(
        RC,
        "get_live_calls_on_numbers",
        AsyncMock(return_value={"N1": [("P", "OUTBOUND", "x")]}),
    )
    sched = AsyncMock(return_value=True)
    monkeypatch.setattr(RC, "schedule_lead", sched)
    assert await RC.ledger_check() == {"removed": 3}
    assert await rv.smembers("bb:busy:N1") == {"lead:K", "lead:P", "lead:T", "call:C2"}
    sched.assert_awaited_once_with(
        "B", WHEN, template_id="T1"
    )  # M6: freed BACKLOG re-queued


async def test_ledger_keeps_a_live_call_stamped_on_a_finished_lead(rv, monkeypatch):
    # #1280: the merchant finished the lead mid-dial, the placed call was stamped on it and
    # owns its line. The ledger must not free that line while the call can be live (the
    # end webhook frees it); past the stuck-sweep ceiling it is freed like any FINISHED.
    await seed_number(rv, "N1", 9, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:A", "lead:OLD", "lead:F")
    now = datetime.now(timezone.utc)
    states = {
        "A": S("FINISHED", False, "T1", None, now - timedelta(minutes=3)),
        "OLD": S("FINISHED", False, "T1", None, now - timedelta(hours=5)),
        "F": S("FINISHED", False, "T1", None),
    }
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value=states))
    monkeypatch.setattr(RC, "get_finished_inbound_calls", AsyncMock(return_value=set()))
    monkeypatch.setattr(RC, "get_known_inbound_calls", AsyncMock(return_value=set()))
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    assert await RC.ledger_check() == {"removed": 2}
    assert await rv.smembers("bb:busy:N1") == {"lead:A"}


async def test_ledger_reads_leases_before_statuses(rv, monkeypatch):
    # rule 11 (PoC bug): the status is read as BACKLOG just before the dial coroutine writes
    # PROCESSING and clears its lease. Read leases first and the live call keeps its line.
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:L1")
    await rv.hset("bb:inflight:N1", "L1", _lease(1, NOW()))

    async def statuses(ids):
        await rv.hdel(
            "bb:inflight:N1", "L1"
        )  # the coroutine: PROCESSING, then clear_lease
        return {"L1": S("BACKLOG", False, "T1", WHEN)}  # the read before PROCESSING

    monkeypatch.setattr(RC, "get_lead_dispatch_states", statuses)
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    monkeypatch.setattr(RC, "schedule_lead", AsyncMock())
    await RC.ledger_check()
    assert await rv.smembers("bb:busy:N1") == {"lead:L1"}


async def test_ledger_removes_atomically_when_a_ticket_lands_mid_check(rv, monkeypatch):
    # The ticket lands between the check's reads and its removal: the holder is kept.
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:L1")

    async def statuses(ids):
        await rv.hset("bb:inflight:N1", "L1", _lease(1, NOW()))
        return {"L1": S("BACKLOG", False, "T1", WHEN)}

    monkeypatch.setattr(RC, "get_lead_dispatch_states", statuses)
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    monkeypatch.setattr(RC, "schedule_lead", AsyncMock())
    await RC.ledger_check()
    assert await rv.smembers("bb:busy:N1") == {"lead:L1"}


async def test_a_lead_redialled_after_the_batch_read_keeps_its_line(rv, monkeypatch):
    # The batched read can be seconds old when its holder is freed. A lead
    # that was BACKLOG then, and has since been ticketed, dialled and had its lease
    # cleared (its holder is now the live call's line), must keep that line.
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:L1")
    reads = iter(
        [
            {"L1": S("BACKLOG", False, "T1", WHEN)},  # the batch: no lease, stale
            {"L1": S("PROCESSING", True, "T1", None)},  # the re-read: dialled since
        ]
    )
    states = AsyncMock(side_effect=lambda ids: next(reads))
    monkeypatch.setattr(RC, "get_lead_dispatch_states", states)
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    monkeypatch.setattr(RC, "schedule_lead", AsyncMock())
    assert await RC.ledger_check() == {"removed": 0}
    assert await rv.smembers("bb:busy:N1") == {"lead:L1"}
    assert states.await_count == 2  # the re-read covers only the candidates


async def test_a_candidate_ticketed_after_the_batch_read_is_not_re_read(
    rv, monkeypatch
):
    # its lease is re-read first (rule 11 order): a lease now means a ticket owns it
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:L1")

    async def batch(ids):
        await rv.hset("bb:inflight:N1", "L1", _lease(1, NOW()))  # a ticket lands
        return {"L1": S("BACKLOG", False, "T1", WHEN)}

    states = AsyncMock(side_effect=batch)
    monkeypatch.setattr(RC, "get_lead_dispatch_states", states)
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    monkeypatch.setattr(RC, "schedule_lead", AsyncMock())
    assert await RC.ledger_check() == {"removed": 0}
    assert states.await_count == 1  # no candidate left to read again


async def test_one_failing_db_chunk_does_not_stop_the_ledger(rv, monkeypatch):
    # A DB error on one chunk must not abort the whole run
    for n, lead in (("N1", "L1"), ("N2", "L2")):
        await seed_number(rv, n, 2, {f"T{n}": {}})
        await rv.sadd("bb:v2:active", n)
        await rv.sadd(f"bb:busy:{n}", f"lead:{lead}")

    async def states(ids):
        if ids == ["L1"]:
            raise ConnectionError("db blip")
        return {"L2": S("BACKLOG", False, "TN2", WHEN)}

    async def live(numbers):
        if numbers == ["N1"]:
            raise ConnectionError("db blip")
        return {}

    monkeypatch.setattr(RC, "BB_V2_LEDGER_CHUNK", 1)
    monkeypatch.setattr(RC, "get_lead_dispatch_states", states)
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", live)
    monkeypatch.setattr(RC, "schedule_lead", AsyncMock())
    assert await RC.ledger_check() == {"removed": 1}
    assert await rv.smembers("bb:busy:N1") == {"lead:L1"}  # unread: kept
    assert await rv.scard("bb:busy:N2") == 0


async def test_an_unread_inbound_chunk_is_never_taken_for_rowless(rv, monkeypatch):
    # the rowless rule frees a call: holder seen with no row twice; an unread chunk is
    # not "no row"
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "call:C1")
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value={}))
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    monkeypatch.setattr(RC, "get_finished_inbound_calls", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        RC, "get_known_inbound_calls", AsyncMock(side_effect=ConnectionError("blip"))
    )
    for _ in range(3):
        assert await RC.ledger_check() == {"removed": 0}
    assert await rv.smembers("bb:busy:N1") == {"call:C1"}


async def test_ledger_alerts_on_two_misses_in_a_row_and_never_adds(rv, monkeypatch):
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value={}))
    monkeypatch.setattr(
        RC,
        "get_live_calls_on_numbers",
        AsyncMock(
            return_value={"N1": [("L2", "OUTBOUND", "c2"), ("I1", "INBOUND", "c9")]}
        ),
    )
    alert = AsyncMock()
    monkeypatch.setattr(RC, "raise_v2_ledger_missing", alert)
    await RC.ledger_check()
    alert.assert_not_awaited()  # one sighting is the normal release-before-FINISHED gap
    await RC.ledger_check()
    alert.assert_awaited_once_with("N1", ["L2"])  # inbound rows are not lead holders
    assert await rv.scard("bb:busy:N1") == 0


async def test_the_ledger_reads_the_db_a_fixed_number_of_times(rv, monkeypatch):
    # spec 2026-10-05 §4.10: one query per BB_V2_LEDGER_CHUNK ids, not per number
    for i in range(50):
        n = f"N{i}"
        await seed_number(rv, n, 2, {f"T{i}": {}})
        await rv.sadd("bb:v2:active", n)
        await rv.sadd(f"bb:busy:{n}", f"lead:L{i}", f"call:C{i}")
    states = AsyncMock(return_value={})
    live = AsyncMock(return_value={})
    ended = AsyncMock(return_value=set())
    known = AsyncMock(return_value=set())
    monkeypatch.setattr(RC, "get_lead_dispatch_states", states)
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", live)
    monkeypatch.setattr(RC, "get_finished_inbound_calls", ended)
    monkeypatch.setattr(RC, "get_known_inbound_calls", known)
    await RC.ledger_check()
    assert [m.await_count for m in (states, live, ended, known)] == [1, 1, 1, 1]
    assert len(states.await_args_list[0].args[0]) == 50
    monkeypatch.setattr(RC, "BB_V2_LEDGER_CHUNK", 20)
    await RC.ledger_check()
    assert [m.await_count for m in (states, live, ended, known)] == [4, 4, 4, 4]


async def test_ledger_does_not_alert_while_a_number_is_switching_on(rv, monkeypatch):
    await seed_number(rv, "N1", 2, {"T1": {}}, mode="v2_pending")
    await rv.sadd("bb:v2:active", "N1")
    monkeypatch.setattr(
        RC,
        "get_live_calls_on_numbers",
        AsyncMock(return_value={"N1": [("L2", "OUTBOUND", "c")]}),
    )
    alert = AsyncMock()
    monkeypatch.setattr(RC, "raise_v2_ledger_missing", alert)
    await RC.ledger_check()
    await RC.ledger_check()
    alert.assert_not_awaited()  # legacy calls are seeded only at the end of v2_pending


async def test_prune_reads_a_big_room_in_chunks(rv, monkeypatch):
    await rv.zadd("bb:q:T1", {f"L{i}": i for i in range(2_500)})
    finished = {f"L{i}" for i in range(0, 2_500, 2)}
    asked = []

    async def states(ids):
        asked.append(len(ids))
        return {
            i: S("FINISHED" if i in finished else "BACKLOG", False, "T1", None)
            for i in ids
        }

    monkeypatch.setattr(RC, "get_lead_dispatch_states", states)

    async def whole_room(*a, **k):
        raise AssertionError("the whole room read in one reply")

    real_zrange = rv.zrange
    monkeypatch.setattr(rv, "zrange", whole_room)
    monkeypatch.setattr(RC, "BB_V2_PRUNE_CHUNK", 1000)
    assert await RC._drop_finished(rv, "bb:q:T1") == 1_250
    assert set(await real_zrange("bb:q:T1", 0, -1)) == {
        f"L{i}" for i in range(1, 2_500, 2)
    }
    assert max(asked) <= 1000 and sum(asked) >= 2_500  # one DB question per chunk


# -- lease reaper -------------------------------------------------------------------------


async def test_the_reaper_reads_every_number_in_one_round_trip(rv, monkeypatch):
    for i in range(20):
        await seed_number(rv, f"N{i}", 1, {f"T{i}": {}})
        await rv.sadd("bb:v2:active", f"N{i}")
    await rv.hset("bb:inflight:N7", "L7", _lease(1, NOW() - 10 * 60_000))
    monkeypatch.setattr(RC, "get_lead_dispatch_states", AsyncMock(return_value={}))

    async def one_by_one(*a, **k):
        raise AssertionError("a lease hash read on its own")

    monkeypatch.setattr(rv, "hgetall", one_by_one)
    assert await RC.reap_leases() == 1  # the old lease was still found


async def test_reaper_frees_an_old_ticket_and_requeues_a_backlog_lead(rv, monkeypatch):
    await seed_number(rv, "N1", 1, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:L1")
    await rv.hset("bb:num:N1", "seq", 7)
    await rv.hset("bb:inflight:N1", "L1", _lease(7, NOW() - 200_000))
    monkeypatch.setattr(
        RC,
        "get_lead_dispatch_states",
        AsyncMock(return_value={"L1": S("BACKLOG", False, "T1", None)}),
    )
    assert await RC.reap_leases() == 1
    # freed and re-queued, then matched at once onto the freed line with a new ticket
    lease = json.loads(await rv.hget("bb:inflight:N1", "L1"))
    assert lease["tk"] == 8
    assert await tickets_of(rv, "N1") == ["L1"]


async def test_reaper_leaves_young_and_dialling_leases_alone(rv, monkeypatch):
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:L1", "lead:L2")
    await rv.hset("bb:inflight:N1", "L1", _lease(1, NOW() - 170_000))  # < 180 s
    await rv.hset(
        "bb:inflight:N1", "L2", _lease(2, NOW() - 300_000, dialling_ms=NOW() - 290_000)
    )
    states = AsyncMock()
    monkeypatch.setattr(RC, "get_lead_dispatch_states", states)
    assert await RC.reap_leases() == 0
    assert await rv.hlen("bb:inflight:N1") == 2
    states.assert_not_awaited()


async def test_reaper_stuck_dial_keeps_a_live_call_line_and_requeues_backlog(
    rv, monkeypatch
):
    # rule 14: a dial coroutine died mid-dial and nothing ever cleared its lease
    await seed_number(rv, "N1", 2, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "lead:LP", "lead:LB")
    old = NOW() - 700_000
    await rv.hset("bb:inflight:N1", "LP", _lease(1, old, dialling_ms=old))
    await rv.hset("bb:inflight:N1", "LB", _lease(2, old, dialling_ms=old))
    monkeypatch.setattr(
        RC,
        "get_lead_dispatch_states",
        AsyncMock(
            return_value={
                "LP": S("PROCESSING", True, "T1", None),
                "LB": S("BACKLOG", False, "T1", None),
            }
        ),
    )
    assert await RC.reap_leases() == 2
    assert await rv.smembers("bb:busy:N1") == {
        "lead:LP",
        "lead:LB",
    }  # LP keeps its line
    assert await rv.hkeys("bb:inflight:N1") == ["LB"]  # LB re-ticketed at once
    assert await tickets_of(rv, "N1") == ["LB"]


# -- seeding helpers ----------------------------------------------------------------------


async def test_locked_leads_are_grouped_by_their_templates_number(rv, monkeypatch):
    # Fable I4: only locked leads count; today's ready/processing lists are not read
    await seed_number(rv, "N1", 5, {"T1": {}})
    await seed_number(rv, "N2", 5, {"T2": {}})
    await rv.rpush("bb:ready:leads", "R1", "R2")
    rows = AsyncMock(
        return_value=[
            ("K1", "T1", True),
            ("K2", "T2", True),  # another number's lead
            ("K3", None, True),  # no template: no route
            ("K4", "T1", True),
        ]
    )
    monkeypatch.setattr(RC, "get_legacy_inflight_leads", rows)
    assert await RC.locked_leads_by_number() == {"N1": {"K1", "K4"}, "N2": {"K2"}}
    rows.assert_awaited_once_with([])  # no picked ids
    sig = RC.locked_signature({"K1", "K4"})
    assert sig == RC.locked_signature({"K4", "K1"})
    assert sig != RC.locked_signature({"K1"})


async def test_attribution_reresolves_each_template_before_reading_its_route(
    rv, monkeypatch
):
    # a stale route (T1 -> N2) would credit N1's legacy dial to N2 and miss it in N1's seed
    await seed_number(rv, "N1", 5, {})
    await seed_number(rv, "N2", 5, {"T1": {}})

    async def reresolve(template_id):
        await rv.hset(f"bb:route:{template_id}", "number", "N1")

    inv = AsyncMock(side_effect=reresolve)
    monkeypatch.setattr(RC, "invalidate_route", inv)
    monkeypatch.setattr(
        RC,
        "get_legacy_inflight_leads",
        AsyncMock(return_value=[("K1", "T1", True), ("K2", "T1", True)]),
    )
    assert await RC.locked_leads_by_number() == {"N1": {"K1", "K2"}}
    inv.assert_awaited_once_with("T1")  # once per template


async def test_locked_leads_read_raises_when_the_db_fails(rv, monkeypatch):
    monkeypatch.setattr(
        RC, "get_legacy_inflight_leads", AsyncMock(side_effect=RuntimeError("db down"))
    )
    with pytest.raises(RuntimeError):
        await RC.locked_leads_by_number()


async def test_seed_holders_cover_outbound_inbound_and_locked_backlog(rv, monkeypatch):
    await seed_number(rv, "N1", 5, {"T1": {}})
    monkeypatch.setattr(
        RC,
        "get_live_calls_on_number",
        AsyncMock(
            return_value=[
                ("L7", "OUTBOUND", "c7"),
                ("I1", "INBOUND", "C9"),
                ("I2", "INBOUND", None),  # no call id: nothing to name it by
            ]
        ),
    )
    monkeypatch.setattr(
        RC, "get_legacy_inflight_leads", AsyncMock(return_value=[("L9", "T1", True)])
    )
    assert await RC.seed_holders("N1") == {"lead:L7", "call:C9", "lead:L9"}


async def test_seed_reads_the_locked_set_before_live_calls(rv, monkeypatch):
    # a legacy dial goes locked BACKLOG -> PROCESSING: read in this order, one read sees it
    await seed_number(rv, "N1", 5, {"T1": {}})
    order = []

    async def locked(ids):
        order.append("locked")
        return []

    async def live(n):
        order.append("live")
        return []

    monkeypatch.setattr(RC, "get_legacy_inflight_leads", locked)
    monkeypatch.setattr(RC, "get_live_calls_on_number", live)
    await RC.seed_holders("N1")
    assert order == ["locked", "live"]


async def test_ledger_frees_a_call_holder_with_no_lead_row_on_the_second_check(
    rv, monkeypatch
):
    # the answer path admits a moment before it inserts the row: one sighting is normal
    await seed_number(rv, "N1", 3, {"T1": {}})
    await rv.sadd("bb:v2:active", "N1")
    await rv.sadd("bb:busy:N1", "call:GONE", "call:LIVE")
    monkeypatch.setattr(RC, "get_finished_inbound_calls", AsyncMock(return_value=set()))
    monkeypatch.setattr(RC, "get_known_inbound_calls", AsyncMock(return_value={"LIVE"}))
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value={}))
    assert await RC.ledger_check() == {"removed": 0}
    assert await rv.scard("bb:busy:N1") == 2
    assert await RC.ledger_check() == {"removed": 1}
    assert await rv.smembers("bb:busy:N1") == {"call:LIVE"}


# -- orphan prune -------------------------------------------------------------------------


async def test_prune_moves_rooms_without_a_v2_number_and_drops_finished_leads(
    rv, monkeypatch
):
    await rv.zadd("bb:q:TV", {"A": 1000, "B": 2000})  # v2 number: keep BACKLOG only
    await rv.zadd("bb:q:TO", {"X": 100, "Y": 200})  # no route / legacy number: today's
    await rv.zadd("bb:q:TU", {"U": 300})  # unreadable: left alone this run
    verdict = {"TV": True, "TO": False, "TU": None}
    monkeypatch.setattr(RC, "_is_v2_template", AsyncMock(side_effect=verdict.get))
    monkeypatch.setattr(
        RC,
        "get_lead_dispatch_states",
        AsyncMock(return_value={"A": S("BACKLOG", False, "TV", None)}),  # B gone
    )
    assert await RC.prune_orphans() == 3  # B dropped, X and Y moved
    assert await rv.zrange("bb:q:TV", 0, -1) == ["A"]
    assert not await rv.exists("bb:q:TO")
    assert await rv.zrange("bb:schedule:leads", 0, -1, withscores=True) == [
        ("X", 100.0),
        ("Y", 200.0),
    ]
    assert await rv.zrange("bb:q:TU", 0, -1) == ["U"]


async def test_move_room_keeps_scores_and_unlinks_the_room(rv):
    await rv.zadd("bb:q:T1", {"L1": 111, "L2": 222})
    await rv.zadd("bb:schedule:leads", {"Z": 5})
    assert await scripts.move_room_to_schedule("T1") == 2
    assert not await rv.exists("bb:q:T1")
    assert await rv.zrange("bb:schedule:leads", 0, -1, withscores=True) == [
        ("Z", 5.0),
        ("L1", 111.0),
        ("L2", 222.0),
    ]


async def test_ledger_reads_every_numbers_mode_in_one_round_trip(rv, monkeypatch):
    """Fable M5: the missing-call alert needs each number's mode; the ledger reads them
    all in one pipeline, not one HGET per number."""
    for n in ("N1", "N2"):
        await seed_number(rv, n, 3, {f"T{n}": {}})
        await rv.sadd("bb:v2:active", n)
    real_hget = rv.hget

    async def hget(key, field, *a):
        if field == "mode":
            raise AssertionError("per-number mode read")
        return await real_hget(key, field, *a)

    monkeypatch.setattr(rv, "hget", hget)
    live = {"N1": [("P1", "OUTBOUND", "c1")]}
    monkeypatch.setattr(RC, "get_live_calls_on_numbers", AsyncMock(return_value=live))
    alert = AsyncMock()
    monkeypatch.setattr(RC, "raise_v2_ledger_missing", alert)
    await RC.ledger_check()
    await RC.ledger_check()  # missing on two checks in a row
    alert.assert_awaited_once_with("N1", ["P1"])
