"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.reconcilers``.

DB accessors are monkeypatched — these tests verify the reconciler's
control flow against known DB returns, not the SQL itself (that's the
accessor's contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import reconcilers as rc
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    SCHEDULE_ZSET,
    channel_key,
)


@dataclass
class _FakeNumber:
    id: str
    maximum_channels: int
    status: str = "AVAILABLE"  # reconciler filters out DISABLED
    channels: Optional[int] = 0  # Postgres lines-in-use counter


# ---------------------------------------------------------------------------
# reconcile_backlog_to_zset
# ---------------------------------------------------------------------------


async def test_reconcile_backlog_adds_missing_leads(fake_redis, monkeypatch):
    async def _fake_get(*a, **kw):
        return [("lead-A", "res-1", 1000), ("lead-B", "res-1", 2000)]

    monkeypatch.setattr(rc, "get_unscheduled_backlog_leads", _fake_get)

    await rc.reconcile_backlog_to_zset()

    z = fake_redis.client.zsets.get(SCHEDULE_ZSET, {})
    assert z.get("lead-A") == 1000
    assert z.get("lead-B") == 2000


async def test_reconcile_backlog_skips_already_present(fake_redis, monkeypatch):
    fake_redis.client.zsets[SCHEDULE_ZSET] = {"lead-A": 500}

    async def _fake_get(*a, **kw):
        return [("lead-A", "res-1", 1000), ("lead-B", "res-1", 2000)]

    monkeypatch.setattr(rc, "get_unscheduled_backlog_leads", _fake_get)

    await rc.reconcile_backlog_to_zset()

    # lead-A keeps its original score (we only ZADD missing members).
    assert fake_redis.client.zsets[SCHEDULE_ZSET]["lead-A"] == 500
    assert fake_redis.client.zsets[SCHEDULE_ZSET]["lead-B"] == 2000


async def test_reconcile_backlog_handles_empty_input(fake_redis, monkeypatch):
    async def _fake_get(*a, **kw):
        return []

    monkeypatch.setattr(rc, "get_unscheduled_backlog_leads", _fake_get)

    # Must not raise; state unchanged.
    await rc.reconcile_backlog_to_zset()
    assert fake_redis.client.zsets.get(SCHEDULE_ZSET, {}) == {}


# ---------------------------------------------------------------------------
# reconcile_channel_tokens — create-or-top-up
# ---------------------------------------------------------------------------


async def test_reconcile_channels_initialises_missing_list(fake_redis, monkeypatch):
    async def _fake_numbers():
        return [_FakeNumber(id="num-A", maximum_channels=3)]

    async def _fake_in_flight():
        return {}  # no calls in flight

    monkeypatch.setattr(rc, "get_all_telephony_numbers", _fake_numbers)
    monkeypatch.setattr(rc, "count_processing_by_telephony_number", _fake_in_flight)

    await rc.reconcile_channel_tokens()

    assert len(fake_redis.client.lists.get(channel_key("num-A"), [])) == 3


async def test_reconcile_channels_skips_disabled_numbers(fake_redis, monkeypatch):
    """DISABLED telephony numbers are not dispatchable; reconciler must not
    create or maintain channel state for them (would inflate Redis +
    drift alerts).
    """

    async def _fake_numbers():
        return [
            _FakeNumber(id="num-active", maximum_channels=3, status="AVAILABLE"),
            _FakeNumber(id="num-off", maximum_channels=3, status="DISABLED"),
        ]

    async def _fake_in_flight():
        return {}

    monkeypatch.setattr(rc, "get_all_telephony_numbers", _fake_numbers)
    monkeypatch.setattr(rc, "count_processing_by_telephony_number", _fake_in_flight)

    await rc.reconcile_channel_tokens()

    # Active number initialised; disabled number untouched.
    assert len(fake_redis.client.lists.get(channel_key("num-active"), [])) == 3
    assert channel_key("num-off") not in fake_redis.client.lists


async def test_reconcile_channels_preserves_zero_maximum_channels(
    fake_redis, monkeypatch
):
    """``maximum_channels=0`` means zero capacity — reconciler must not
    create a channel LIST with 1 token (regression for the ``or 1`` bug)."""

    async def _fake_numbers():
        return [_FakeNumber(id="num-zero", maximum_channels=0)]

    async def _fake_in_flight():
        return {}

    monkeypatch.setattr(rc, "get_all_telephony_numbers", _fake_numbers)
    monkeypatch.setattr(rc, "count_processing_by_telephony_number", _fake_in_flight)

    await rc.reconcile_channel_tokens()

    # init_channel_semaphore early-returns for maximum_channels <= 0.
    assert channel_key("num-zero") not in fake_redis.client.lists


async def test_reconcile_channels_tops_up_short_list(fake_redis, monkeypatch):
    # Existing list with 1 token, max 5, 0 in flight => expected_free=5
    fake_redis.client.lists[channel_key("num-A")] = ["t1"]

    async def _fake_numbers():
        return [_FakeNumber(id="num-A", maximum_channels=5)]

    async def _fake_in_flight():
        return {}

    monkeypatch.setattr(rc, "get_all_telephony_numbers", _fake_numbers)
    monkeypatch.setattr(rc, "count_processing_by_telephony_number", _fake_in_flight)

    await rc.reconcile_channel_tokens()

    # Topped up to 5.
    assert len(fake_redis.client.lists[channel_key("num-A")]) == 5


async def test_reconcile_channels_trims_excess(fake_redis, monkeypatch):
    # Existing list with 10 tokens, max 5, 0 in flight => expected_free=5
    fake_redis.client.lists[channel_key("num-A")] = [f"t{i}" for i in range(10)]

    async def _fake_numbers():
        return [_FakeNumber(id="num-A", maximum_channels=5)]

    async def _fake_in_flight():
        return {}

    monkeypatch.setattr(rc, "get_all_telephony_numbers", _fake_numbers)
    monkeypatch.setattr(rc, "count_processing_by_telephony_number", _fake_in_flight)

    await rc.reconcile_channel_tokens()

    assert len(fake_redis.client.lists[channel_key("num-A")]) == 5


async def test_reconcile_channels_accounts_for_in_flight(fake_redis, monkeypatch):
    """expected_free = max - in_flight. 2 in flight on a 5-channel number => 3 free."""

    async def _fake_numbers():
        return [_FakeNumber(id="num-A", maximum_channels=5)]

    async def _fake_in_flight():
        return {"num-A": 2}

    monkeypatch.setattr(rc, "get_all_telephony_numbers", _fake_numbers)
    monkeypatch.setattr(rc, "count_processing_by_telephony_number", _fake_in_flight)

    await rc.reconcile_channel_tokens()

    assert len(fake_redis.client.lists[channel_key("num-A")]) == 3


# ---------------------------------------------------------------------------
# reconcile_channel_tokens — the band rule
#
# Two Postgres counts say "line in use": ``channels`` (+1 right before the
# dial) and PROCESSING rows (set once the provider returns a call id). The
# higher one wins in both directions.
# ---------------------------------------------------------------------------


def _wire(monkeypatch, numbers, in_flight):
    async def _fake_numbers():
        return numbers

    async def _fake_in_flight():
        return in_flight

    monkeypatch.setattr(rc, "get_all_telephony_numbers", _fake_numbers)
    monkeypatch.setattr(rc, "count_processing_by_telephony_number", _fake_in_flight)


async def test_reconcile_channels_no_phantom_for_calls_still_dialling(
    fake_redis, monkeypatch
):
    """5 lines, all taken (channels=5), but only 3 rows are PROCESSING yet —
    2 calls are between ``+1`` and the CAS update. max - PROCESSING would
    mint 2 tokens here; the reconciler must mint none."""
    fake_redis.client.lists[channel_key("num-A")] = []
    _wire(
        monkeypatch,
        [_FakeNumber(id="num-A", maximum_channels=5, channels=5)],
        {"num-A": 3},
    )

    await rc.reconcile_channel_tokens()

    assert fake_redis.client.lists[channel_key("num-A")] == []


async def test_reconcile_channels_trims_token_from_duplicate_release(
    fake_redis, monkeypatch
):
    """2-line number, calls A and B live. A's end webhook is released twice:
    ``channels`` 2 -> 0 and two tokens pushed, while only one line is free.
    PROCESSING (B) is still right, so the extra token must be trimmed —
    otherwise the next two leads both pass the ``channels < max`` gate and
    three calls run on two lines."""
    fake_redis.client.lists[channel_key("num-A")] = ["t1", "t2"]
    _wire(
        monkeypatch,
        [_FakeNumber(id="num-A", maximum_channels=2, channels=0)],
        {"num-A": 1},
    )

    await rc.reconcile_channel_tokens()

    assert len(fake_redis.client.lists[channel_key("num-A")]) == 1


async def test_reconcile_channels_trims_to_leaked_channels(fake_redis, monkeypatch):
    """5 tokens, nothing PROCESSING, but Postgres says 2 lines in use (a
    leaked ``+1``). Trim to 3 — the extra tokens would only be refused."""
    fake_redis.client.lists[channel_key("num-A")] = [f"t{i}" for i in range(5)]
    _wire(
        monkeypatch,
        [_FakeNumber(id="num-A", maximum_channels=5, channels=2)],
        {"num-A": 0},
    )

    await rc.reconcile_channel_tokens()

    assert len(fake_redis.client.lists[channel_key("num-A")]) == 3


async def test_reconcile_channels_call_end_window_is_restored_next_tick(
    fake_redis, monkeypatch
):
    """At call end ``channels -1`` and the token push run a moment before the
    row turns FINISHED. A tick landing in that gap trims the token; the next
    tick, with both counts caught up, puts it back."""
    fake_redis.client.lists[channel_key("num-A")] = ["t1"]
    numbers = [_FakeNumber(id="num-A", maximum_channels=5, channels=4)]
    in_flight = {"num-A": 5}
    _wire(monkeypatch, numbers, in_flight)

    await rc.reconcile_channel_tokens()
    assert len(fake_redis.client.lists.get(channel_key("num-A"), [])) == 0

    in_flight["num-A"] = 4
    await rc.reconcile_channel_tokens()
    assert len(fake_redis.client.lists[channel_key("num-A")]) == 1


async def test_reconcile_channels_initialises_from_channels_too(
    fake_redis, monkeypatch
):
    """Cold start while 4 of 5 lines are mid-dial: init with 1 token, not 5."""
    _wire(
        monkeypatch,
        [_FakeNumber(id="num-A", maximum_channels=5, channels=4)],
        {},
    )

    await rc.reconcile_channel_tokens()

    assert len(fake_redis.client.lists[channel_key("num-A")]) == 1


async def test_reconcile_channels_null_channels_behaves_as_before(
    fake_redis, monkeypatch
):
    """Backward compatibility: a row whose ``channels`` is NULL is treated
    as 0, so the result equals the old max - PROCESSING rule."""
    fake_redis.client.lists[channel_key("num-A")] = []
    _wire(
        monkeypatch,
        [_FakeNumber(id="num-A", maximum_channels=5, channels=None)],
        {"num-A": 2},
    )

    await rc.reconcile_channel_tokens()

    assert len(fake_redis.client.lists[channel_key("num-A")]) == 3


# ---------------------------------------------------------------------------
# clean_stale_bb_locks — passes through to accessor
# ---------------------------------------------------------------------------


async def test_clean_stale_bb_locks_invokes_accessor(monkeypatch):
    seen: List[int] = []

    async def _fake_clean(threshold_minutes):
        seen.append(threshold_minutes)
        return ["lead-X", "lead-Y"]

    monkeypatch.setattr(rc, "accessor_clean_stale_bb_locks", _fake_clean)

    await rc.clean_stale_bb_locks()

    assert len(seen) == 1
    # Default threshold matches the config knob (10 minutes).
    assert seen[0] == 10


# ---------------------------------------------------------------------------
# monitor_dispatch_health
# ---------------------------------------------------------------------------


async def test_health_monitor_alerts_no_leader(fake_redis, monkeypatch):
    seen: List[str] = []

    async def _fake_no_leader():
        seen.append("no_leader")

    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr(rc, "raise_no_leader", _fake_no_leader)
    monkeypatch.setattr(rc, "raise_dispatch_halted", _noop)
    monkeypatch.setattr(rc, "raise_schedule_depth_high", _noop)
    monkeypatch.setattr(rc, "clear_throttle", _noop)

    # No leader key set on fake redis -> alert fires.
    await rc.monitor_dispatch_health()
    assert seen == ["no_leader"]


async def test_health_monitor_clears_no_leader_when_leader_present(
    fake_redis, monkeypatch
):
    from app.ai.voice.agents.breeze_buddy.dispatch.keys import PROMOTER_LEADER

    fake_redis.client.kv[PROMOTER_LEADER] = "pod-A"

    no_leader_fired: List[str] = []
    cleared: List[str] = []

    async def _no_leader():
        no_leader_fired.append("x")

    async def _clear(name):
        cleared.append(name)

    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr(rc, "raise_no_leader", _no_leader)
    monkeypatch.setattr(rc, "raise_dispatch_halted", _noop)
    monkeypatch.setattr(rc, "raise_schedule_depth_high", _noop)
    monkeypatch.setattr(rc, "clear_throttle", _clear)

    await rc.monitor_dispatch_health()
    assert no_leader_fired == []
    # Healthy state must clear both halt-style throttles so a future recurrence
    # alerts immediately.
    assert "no_leader" in cleared
    assert "dispatch_halted" in cleared


async def test_health_monitor_alerts_dispatch_halted_when_overdue(
    fake_redis, monkeypatch
):
    import time as _t

    from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
        PROMOTER_LEADER,
        SCHEDULE_ZSET,
    )

    fake_redis.client.kv[PROMOTER_LEADER] = "pod-A"
    # Seed schedule with many overdue leads.
    past_ms = int(_t.time() * 1000) - 5_000
    for i in range(500):
        fake_redis.client.zsets.setdefault(SCHEDULE_ZSET, {})[f"lead-{i}"] = past_ms

    halted: List[int] = []

    async def _halted(overdue_count, schedule_size):
        halted.append(overdue_count)

    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr(rc, "raise_no_leader", _noop)
    monkeypatch.setattr(rc, "raise_dispatch_halted", _halted)
    monkeypatch.setattr(rc, "raise_schedule_depth_high", _noop)
    monkeypatch.setattr(rc, "clear_throttle", _noop)

    await rc.monitor_dispatch_health()
    assert halted == [500]
