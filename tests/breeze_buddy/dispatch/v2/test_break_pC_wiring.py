"""Break Package C wiring (T12) and the controller's rulings on real Redis: the legacy
redirect through today's worker, the acceptor on pods that never used v2 (ruling 1), inbound
admits while a number is ``v2_pending`` (ruling 2), save hooks, and the v2 SQL shapes.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import (
    queue as queue_mod,
    worker as worker_mod,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET, channel_key
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import hooks as H, latch, routes
from app.ai.voice.agents.breeze_buddy.managers import inbound_channel as IC
from app.database.queries.breeze_buddy import dispatch as Q
from app.schemas import CallProvider, TelephonyNumber, TelephonyNumberStatus
from tests.breeze_buddy.dispatch.conftest import make_lead
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, tickets_of, use_redis

pytestmark = pytest.mark.asyncio


def now_ms() -> int:
    return int(time.time() * 1000)


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


# ===========================================================================================
# Ruling C-concern 2: inbound admit while v2_pending uses today's DB gate
# ===========================================================================================


@pytest.mark.parametrize(
    "mode, db_gate_used, busy_after",
    [
        (
            "v2_pending",
            True,
            set(),
        ),  # legacy accounting is still the truth until seeding
        ("v2", False, {"call:C1"}),
        ("draining", False, {"call:C1"}),
    ],
)
async def test_inbound_admit_gate_by_mode(
    rr, monkeypatch, mode, db_gate_used, busy_after
):
    """Ruling C-concern 2: in ``v2_pending`` the busy list is still empty (legacy calls are
    seeded only at the end of the pending phase), so gating inbound on it admits ``max``
    inbound calls on top of the live legacy calls. Today's DB gate must decide until the
    number is seeded; from ``v2`` on, the busy list."""
    monkeypatch.setattr(latch, "_seen", True)
    use_redis(monkeypatch, rr, routes)
    await seed_number(rr, "N1", 2, {"T1": {}}, mode=mode)
    gate = AsyncMock(return_value=NS(id="N1"))
    monkeypatch.setattr(IC, "increment_telephony_number_channels", gate)
    try:
        assert await IC.admit_inbound_call("N1", call_id="C1") is True
    finally:
        latch._reset_for_tests()
    assert gate.await_count == (1 if db_gate_used else 0)
    assert await rr.smembers("bb:busy:N1") == busy_after


# ===========================================================================================
# Legacy redirect through today's worker, with the real enqueue Lua
# ===========================================================================================


@pytest.fixture
def legacy_pick(monkeypatch, harness, fake_redis, rr):
    """Today's worker (harness) picks lead L1 whose number is num-1; v2's Redis is real."""
    harness.add_lead(make_lead("L1"))
    fake_redis.client.lists[channel_key("num-1")] = ["tok-1"]
    monkeypatch.setattr(
        worker_mod, "spawn_background_task", lambda coro, name=None: coro.close()
    )
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    use_redis(monkeypatch, rr, routes)
    # the route is in Redis already; never resolve through the DB here
    monkeypatch.setattr(queue_mod, "_ensure_route", AsyncMock())
    monkeypatch.setattr(queue_mod, "_invalidate_route", AsyncMock())
    return harness


async def _dispatch() -> bool:
    return await worker_mod.Worker(worker_uuid="w-test")._dispatch("L1", None)


async def test_redirect_bounces_to_the_room_with_the_due_time_kept_and_no_ticket_in_pending(
    legacy_pick, rr, fake_redis
):
    h = legacy_pick
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode="v2_pending")
    assert await _dispatch() is False
    assert h.call_recorder.calls == [] and h.deferred == []
    assert h.released_locks == ["L1"] and "L1" not in h.locked_lead_ids
    assert fake_redis.client.lists[channel_key("num-1")] == ["tok-1"]  # no token taken
    assert await rr.zscore("bb:q:tmpl-1", "L1") == ms(h.leads["L1"].next_attempt_at)
    assert len(await tickets_of(rr, "num-1")) == 0  # pending: nothing issued
    assert (
        fake_redis.client.zsets.get(SCHEDULE_ZSET, {}) == {}
    )  # not on today's schedule


async def test_redirect_on_a_v2_number_issues_exactly_one_ticket_even_if_picked_twice(
    legacy_pick, rr
):
    """The bounced lead is due: match issues its ticket at once. A second legacy worker that
    also picked it (a duplicate ready-list entry) bounces it again and gets -2 from the Lua:
    one ticket, one copy, no second dial."""
    h = legacy_pick
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode="v2")
    assert await _dispatch() is False
    assert h.call_recorder.calls == []
    lease = await rr.hget("bb:inflight:num-1", "L1")
    assert lease is not None and await tickets_of(rr, "num-1") == ["L1"]
    assert await rr.zcard("bb:q:tmpl-1") == 0

    assert await _dispatch() is False  # picked again by another legacy worker
    assert h.call_recorder.calls == [] and h.deferred == []
    assert await rr.hget("bb:inflight:num-1", "L1") == lease  # same ticket
    assert await tickets_of(rr, "num-1") == ["L1"]
    assert await rr.zcard("bb:q:tmpl-1") == 0 and await rr.scard("bb:busy:num-1") == 1


async def test_redirect_with_an_unreadable_mode_defers_and_dials_nothing(
    legacy_pick, rr, monkeypatch
):
    h = legacy_pick
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode="v2")

    class Broken:
        async def hget(self, key, field, *a):
            if field == "mode":
                raise ConnectionError("blip")
            return await rr.hget(key, field, *a)

    async def _broken():
        return Broken()

    monkeypatch.setattr(routes, "_client", _broken)
    assert await _dispatch() is False
    assert h.call_recorder.calls == []
    assert h.deferred == [("L1", worker_mod.V2_REDIRECT_RETRY_S)]
    assert (
        len(await tickets_of(rr, "num-1")) == 0 and await rr.scard("bb:busy:num-1") == 0
    )


async def test_redirect_of_a_lead_without_a_template_defers_instead_of_bouncing(
    legacy_pick, rr, fake_redis
):
    """A lead with no template_id on a v2 number cannot go to a room: deferred 30 s, never
    bounced in a loop. (Note: today's path would dial it 'without a template'; on a v2
    number it is deferred every 30 s for as long as the number stays on v2.)"""
    h = legacy_pick
    h.leads["L1"].template_id = None
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode="v2")
    assert await _dispatch() is False
    assert h.call_recorder.calls == []
    assert h.deferred == [("L1", worker_mod.V2_REDIRECT_RETRY_S)]
    assert "L1" not in h.locked_lead_ids
    assert fake_redis.client.lists[channel_key("num-1")] == ["tok-1"]  # no token taken
    assert await rr.zcard("bb:q:tmpl-1") == 0 and await rr.scard("bb:busy:num-1") == 0


# ===========================================================================================
# Fix 1-B: Redis lost v2's state (bb:epoch missing) -> today's worker holds (x7b)
# ===========================================================================================


async def test_redis_loss_holds_todays_dialling_on_a_number_that_reads_as_legacy(
    legacy_pick, rr, fake_redis
):
    """x7b: a Redis restart / flush wiped v2's keys and the v2 flags with them, so every
    number reads as legacy while today's counters may be stale. With v2 in use on this pod
    and bb:epoch missing after this process saw it set, today's worker defers (30 s)
    instead of dialling, and takes no token, until the sweep leader's recovery sets the
    epoch again."""
    h = legacy_pick
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode=None)  # facts, no mode
    await rr.set("bb:epoch", "x")
    assert await queue_mod.v2_owns_number("num-1") is False  # epoch seen: as today
    await rr.delete("bb:epoch")  # the loss
    assert await _dispatch() is False
    assert h.call_recorder.calls == []
    assert h.deferred == [("L1", worker_mod.V2_REDIRECT_RETRY_S)]
    assert fake_redis.client.lists[channel_key("num-1")] == ["tok-1"]  # no token taken
    assert await rr.zcard("bb:q:tmpl-1") == 0  # not bounced to a room either


async def test_first_use_holds_nothing_while_the_epoch_was_never_set(legacy_pick, rr):
    """v2 just turned on: bb:epoch was never set, and this process never saw it, so its
    absence is first use, not a loss. v2 never ran, today's counters are its own: today's
    worker dials as before while the sweep leader sets the epoch."""
    h = legacy_pick
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode=None)
    assert await _dispatch() is True
    assert len(h.call_recorder.calls) == 1 and h.deferred == []


async def test_with_the_epoch_present_a_legacy_number_is_dialled_as_today(
    legacy_pick, rr
):
    h = legacy_pick
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode="legacy")
    await rr.set("bb:epoch", "x")
    assert await _dispatch() is True
    assert len(h.call_recorder.calls) == 1 and h.deferred == []


async def test_an_unreadable_epoch_defers_and_dials_nothing(
    legacy_pick, rr, monkeypatch
):
    h = legacy_pick
    await seed_number(rr, "num-1", 2, {"tmpl-1": {}}, mode="legacy")
    await rr.set("bb:epoch", "x")

    class Broken:
        async def hget(self, *a):
            return await rr.hget(*a)

        async def exists(self, *a):
            raise ConnectionError("blip")

    async def _broken():
        return Broken()

    monkeypatch.setattr(routes, "_client", _broken)
    assert await _dispatch() is False
    assert h.call_recorder.calls == []
    assert h.deferred == [("L1", worker_mod.V2_REDIRECT_RETRY_S)]


async def test_a_pod_that_never_used_v2_dials_as_today_without_reading_the_epoch(
    legacy_pick, rr, monkeypatch
):
    h = legacy_pick
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=False))

    async def _untouched():
        raise AssertionError("v2 Redis read on a pod that never used v2")

    monkeypatch.setattr(routes, "_client", _untouched)
    assert await _dispatch() is True  # no epoch in Redis, and today's path all the same
    assert len(h.call_recorder.calls) == 1


# ===========================================================================================
# Save hooks
# ===========================================================================================


async def test_number_hook_for_a_disabled_number_writes_facts_only(rr, monkeypatch):
    """A number disabled through the API: the hook rewrites status (so the 5 s switch check
    drains it) and never touches mode, busy or the routes synchronously."""
    use_redis(monkeypatch, rr, H, routes)
    monkeypatch.setattr(H, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(
        H, "spawn_background_task", lambda coro, name=None: coro.close()
    )
    await seed_number(rr, "N1", 2, {"T1": {}}, mode="v2")
    await rr.sadd("bb:busy:N1", "lead:A")
    await H.on_number_saved(
        TelephonyNumber(
            id="N1",
            number="+91N1",
            provider=CallProvider.PLIVO,
            status=TelephonyNumberStatus.DISABLED,
            channels=0,
            maximum_channels=2,
        )
    )
    num = await rr.hgetall("bb:num:N1")
    assert num["status"] == "DISABLED" and num["mode"] == "v2" and num["max"] == "2"
    assert await rr.smembers("bb:busy:N1") == {"lead:A"}
    assert await rr.hget("bb:route:T1", "number") == "N1"


# ===========================================================================================
# SQL shapes (no DB here: positional params only, text arrays for VARCHAR ids, filters)
# ===========================================================================================


async def test_v2_queries_use_positional_params_and_the_expected_filters():
    when = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
    marker = "ZZ-never-in-sql-ZZ"
    cases = [
        Q.get_due_backlog_page_query(None, 10, 120),
        Q.get_due_backlog_page_query((when, marker), 10, 120),
        Q.get_lead_dispatch_states_query([marker, "b"]),
        Q.get_live_calls_on_number_query(marker),
        Q.get_live_calls_on_numbers_query([marker]),
        Q.get_finished_inbound_calls_query([marker]),
        Q.get_legacy_inflight_leads_query([marker]),
        Q.get_telephony_numbers_by_ids_query([marker]),
        Q.set_telephony_number_channels_query(marker, 3),
    ]
    for sql, values in cases:
        used = sorted({int(m) for m in re.findall(r"\$(\d+)", sql)})
        assert used == list(range(1, len(values) + 1)), (sql, values)
        assert marker not in sql  # never interpolated
    for builder in (
        Q.get_lead_dispatch_states_query,
        Q.get_finished_inbound_calls_query,
        Q.get_legacy_inflight_leads_query,
        Q.get_telephony_numbers_by_ids_query,
    ):
        assert "::text[]" in builder([marker])[0], builder.__name__
    assert "::text[]" in Q.get_live_calls_on_numbers_query([marker])[0]
    live = Q.get_live_calls_on_number_query(marker)[0]
    assert "'PROCESSING'" in live and "'OUTBOUND'" in live and "'INBOUND'" in live
    assert "'TELEPHONY', 'TELEPHONY_TEST'" in live
    # the ledger's batched read holds a line for exactly the same calls
    many = Q.get_live_calls_on_numbers_query([marker])[0]
    assert (
        many.split("WHERE")[1].replace("ANY($1::text[])", "$1")
        == live.split("WHERE")[1]
    )
    page = Q.get_due_backlog_page_query(None, 10, 120)[0]
    assert "'BACKLOG'" in page and '"is_locked" = FALSE' in page
    assert '("next_attempt_at", "id") > ($1::timestamptz, $2::text)' in page
