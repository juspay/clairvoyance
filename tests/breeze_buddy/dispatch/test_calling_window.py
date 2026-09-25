"""Out-of-hours leads sleep until their calling window opens, and a changed
calling window reaches the leads already asleep."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import calling_window as cw, worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod

from .conftest import make_lead

IST = timezone(timedelta(hours=5, minutes=30))


def _cfg(start: time, end: time, template_id: str = "tpl-1", enabled=True) -> Any:
    return SimpleNamespace(
        call_start_time=start,
        call_end_time=end,
        template_id=template_id,
        enable_calling=enabled,
    )


def _ist(h: int, m: int = 0, day: int = 24, s: int = 0, us: int = 0) -> datetime:
    return datetime(2026, 9, day, h, m, s, us, tzinfo=IST)


# -- park time: the next window start, exactly ------------------------------


@pytest.mark.parametrize(
    "start, end, now, expected",
    [
        # Day window 09:00–21:00: late night waits for tomorrow's 09:00.
        (time(9), time(21), _ist(23), _ist(9, day=25)),
        # Early morning waits for today's 09:00.
        (time(9), time(21), _ist(3, 30), _ist(9)),
        # Just after close waits for tomorrow.
        (time(9), time(21), _ist(21, s=1), _ist(9, day=25)),
        # Overnight window 22:00–06:00: mid-day waits for tonight's 22:00.
        (time(22), time(6), _ist(12), _ist(22)),
    ],
)
def test_park_time(start, end, now, expected):
    assert w._calling_window_park_time(_cfg(start, end), now) == expected


def test_parks_at_an_exact_instant():
    """The park time is the exact window start, with no microseconds."""
    park = w._calling_window_park_time(
        _cfg(time(9, 30), time(20)), _ist(2, 17, s=43, us=512345)
    )
    assert park == _ist(9, 30)


def test_jitter_early_wake_reparks_seconds_not_a_day():
    """A lead woken just before the start is parked for today, not tomorrow."""
    cfg = _cfg(time(9), time(21))
    assert w._calling_window_park_time(cfg, _ist(8, 59, s=59, us=800000)) == _ist(9)


# -- the worker parks out-of-hours leads once, at an exact instant ----------


async def test_worker_parks_out_of_hours_lead_at_window_open(
    harness, fake_redis, monkeypatch
):
    parked: list = []

    async def park(lead_id, park_until):
        parked.append((lead_id, park_until))
        lead = harness.leads[lead_id]
        lead.next_attempt_at = park_until
        harness.locked_lead_ids.discard(lead_id)
        return lead

    monkeypatch.setattr(w, "park_lead_until_and_release_lock", park)
    monkeypatch.setattr(w, "_is_within_calling_hours", lambda config, now: False)
    harness.add_lead(make_lead("lead-night"))

    await w.Worker("w-night")._dispatch("lead-night", None)

    ((lead_id, park_until),) = parked
    assert lead_id == "lead-night"
    assert park_until.astimezone(IST).time() == time(0, 0)  # the 00:00 start
    assert park_until.microsecond == 0  # an exact instant, not now + seconds
    assert harness.deferred == []  # not the generic now+seconds defer
    assert harness.call_recorder.calls == []
    assert "lead-night" not in harness.locked_lead_ids
    score = await fake_redis.client.zscore(SCHEDULE_ZSET, "lead-night")
    assert abs(score / 1000 - park_until.timestamp()) <= 0.25  # ± jitter


# -- pure time math ---------------------------------------------------------


def test_wake_is_next_park_instant_when_closed():
    # 10:30 PM, new window 07:00–21:00 → tomorrow 07:00.
    wake = cw.window_wake_time(_cfg(time(7), time(21)), _ist(22, 30))
    assert wake == _ist(7, day=25)


def test_wake_is_now_when_window_now_open():
    now = _ist(22, 30)
    assert cw.window_wake_time(_cfg(time(9), time(23)), now) == now


def test_parked_instants_cover_both_days():
    got = cw.parked_instants(_cfg(time(9), time(21)), _ist(23, 45))
    assert got == [_ist(9), _ist(9, day=25)]


def test_parked_instants_cover_early_morning_change():
    # Parked at 10 PM for tomorrow 09:00, config changed at 02:00 next day.
    got = cw.parked_instants(_cfg(time(9), time(21)), _ist(2, day=25))
    assert _ist(9, day=25) in got


# -- orchestration ----------------------------------------------------------


@pytest.fixture
def calls(monkeypatch):
    rec: dict = {"db": [], "zadd": [], "moved": []}

    async def fake_wake(**kwargs):
        rec["db"].append(kwargs)
        return [(lid, kwargs["wake_at"]) for lid in rec["moved"]]

    async def fake_schedule_leads(items, jitter_ms=None):
        rec["zadd"].extend(items)
        return len(items)

    monkeypatch.setattr(cw, "wake_window_parked_leads", fake_wake)
    monkeypatch.setattr(cw, "schedule_leads", fake_schedule_leads)
    return rec


def _park_times(db_call: dict) -> List[time]:
    return [p.astimezone(IST).time() for p in db_call["parked_at"]]


async def test_start_moved_earlier_moves_and_reschedules(calls):
    calls["moved"].append("lead-a")
    n = await cw.wake_leads_for_new_window(
        _cfg(time(9), time(21)), _cfg(time(7), time(21)), now=_ist(22, 30)
    )
    target = _ist(7, day=25)
    assert n == 1
    (db,) = calls["db"]
    assert db["template_id"] == "tpl-1"
    assert db["wake_at"] == target
    assert _park_times(db) == [time(9)] * 2  # searched at the OLD start
    assert calls["zadd"] == [("lead-a", target)]


async def test_start_moved_later_moves_parked_leads_later(calls):
    """Parked leads also move to a later start."""
    await cw.wake_leads_for_new_window(
        _cfg(time(9), time(21)), _cfg(time(10), time(21)), now=_ist(22, 30)
    )
    assert calls["db"][0]["wake_at"] == _ist(10, day=25)


async def test_unchanged_window_touches_nothing(calls):
    n = await cw.wake_leads_for_new_window(
        _cfg(time(9), time(21)), _cfg(time(9), time(21)), now=_ist(22, 30)
    )
    assert n == 0 and calls["db"] == []


async def test_window_change_with_calling_disabled_still_moves(calls):
    """Parked leads move even while calling is disabled."""
    await cw.wake_leads_for_new_window(
        _cfg(time(9), time(21)),
        _cfg(time(7), time(21), enabled=False),
        now=_ist(22, 30),
    )
    assert len(calls["db"]) == 1


async def test_worker_uses_one_clock_read_at_window_open(
    harness, fake_redis, monkeypatch
):
    """The hours check and the park time use the same clock reading."""
    harness.config.call_start_time = time(9)
    harness.config.call_end_time = time(21)
    reads = iter([_ist(8, 59, s=59), _ist(9, s=0)])  # 2nd read would be "open"
    seen: list = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(reads)

    real_within = calls_mod._is_within_calling_hours  # harness stubs w.'s copy

    def within(config, now):
        seen.append(now)
        return real_within(config, now)

    parked: list = []

    async def park(lead_id, park_until):
        parked.append(park_until)
        harness.locked_lead_ids.discard(lead_id)
        return harness.leads[lead_id]

    monkeypatch.setattr(w, "datetime", Clock)
    monkeypatch.setattr(w, "_is_within_calling_hours", within)
    monkeypatch.setattr(w, "park_lead_until_and_release_lock", park)
    harness.add_lead(make_lead("lead-edge"))

    await w.Worker("w-edge")._dispatch("lead-edge", None)

    assert seen == [_ist(8, 59, s=59)]
    assert parked == [_ist(9)]  # today, not tomorrow


async def test_db_failure_does_not_raise(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(cw, "wake_window_parked_leads", boom)
    n = await cw.wake_leads_for_new_window(
        _cfg(time(9), time(21)), _cfg(time(7), time(21)), now=_ist(22, 30)
    )
    assert n == 0


# -- query shape ------------------------------------------------------------


def test_park_query_writes_the_exact_instant():
    from app.database.queries.breeze_buddy.dispatch import (
        park_lead_until_and_release_lock_query,
    )

    text, values = park_lead_until_and_release_lock_query("lead-1", _ist(9))
    assert 'GREATEST(COALESCE("next_attempt_at", NOW()), $2)' in text
    assert '"is_locked" = FALSE' in text
    assert values == ["lead-1", _ist(9)]


def test_wake_query_matches_parked_instants_exactly():
    from app.database.queries.breeze_buddy.dispatch import (
        wake_window_parked_leads_query,
    )

    text, values = wake_window_parked_leads_query("tpl-1", [_ist(9)], _ist(7))
    assert '"next_attempt_at" = ANY($3::timestamptz[])' in text
    assert '"next_attempt_at" > NOW()' in text  # due leads are already queued
    assert '"next_attempt_at" <> $2' in text  # rows already there untouched
    assert '"is_locked" = FALSE' in text
    assert values == ["tpl-1", _ist(7), [_ist(9)]]
