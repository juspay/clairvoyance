"""schedule_lead routes v2 numbers' leads into rooms; everything else is today's ZADD."""

import datetime as dt
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import queue, reconcilers as rc
from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET

pytestmark = pytest.mark.asyncio

NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def v2(monkeypatch, fake_redis):
    """v2 seen; route resolves; enqueue answers 1 unless a test says otherwise."""
    m = NS(
        seen=AsyncMock(return_value=True),
        route=AsyncMock(return_value=NS(number_id="N1")),
        invalidate=AsyncMock(),
        enqueue=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(queue, "v2_seen", m.seen)
    monkeypatch.setattr(queue, "_ensure_route", m.route)
    monkeypatch.setattr(queue, "_invalidate_route", m.invalidate)
    monkeypatch.setattr(queue.v2_scripts, "enqueue", m.enqueue)
    m.redis = fake_redis
    return m


def _zset(v2):
    return v2.redis.client.zsets.get(SCHEDULE_ZSET, {})


async def test_not_seen_is_todays_zadd_and_touches_nothing_v2(v2):
    v2.seen.return_value = False
    assert await queue.schedule_lead("L1", NOW, jitter_ms=0, template_id="T1") is True
    assert "L1" in _zset(v2)
    v2.route.assert_not_awaited()
    v2.enqueue.assert_not_awaited()


async def test_no_template_is_todays_zadd_without_a_latch_read(v2):
    assert await queue.schedule_lead("L1", NOW, jitter_ms=0) is True
    assert "L1" in _zset(v2)
    v2.seen.assert_not_awaited()
    v2.enqueue.assert_not_awaited()


async def test_v2_number_enqueues_at_the_exact_due_time(v2):
    # rule 16: no jitter in v2 (it pushed half of the "call now" leads 1 ms out)
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    v2.enqueue.assert_awaited_once_with("T1", "L1", queue._to_unix_ms(NOW), False)
    assert _zset(v2) == {}


async def test_number_not_v2_accounted_falls_back_to_zadd(v2):
    v2.enqueue.return_value = -3
    assert await queue.schedule_lead("L1", NOW, jitter_ms=0, template_id="T1") is True
    assert _zset(v2) == {"L1": queue._to_unix_ms(NOW)}


async def test_missing_route_is_reresolved_once_then_retried(v2):
    v2.enqueue.side_effect = [-1, 1]
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    v2.route.assert_awaited_once_with("T1")
    v2.invalidate.assert_not_awaited()
    assert v2.enqueue.await_count == 2


async def test_route_present_costs_no_route_read(v2):
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    v2.route.assert_not_awaited()


async def test_route_still_missing_is_reresolved_once_more(v2):
    v2.enqueue.side_effect = [-1, -1, 1]
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    v2.invalidate.assert_awaited_once_with("T1")


async def test_unresolvable_route_uses_todays_schedule(v2):
    v2.enqueue.side_effect = [-1, -1, -1]
    assert await queue.schedule_lead("L1", NOW, jitter_ms=0, template_id="T1") is True
    assert "L1" in _zset(v2)


async def test_lead_holding_a_line_is_left_to_its_holder(v2):
    v2.enqueue.return_value = -2
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    assert _zset(v2) == {}


async def test_redis_error_returns_false_and_no_zadd(v2):
    # a ZADD is not safe for a v2 number; the backlog reconciler retries
    v2.enqueue.return_value = None
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is False
    assert _zset(v2) == {}


async def test_ensure_route_error_does_not_escape(v2):
    v2.route.side_effect = RuntimeError("db down")
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    v2.enqueue.assert_awaited_once()


async def test_legacy_reconciler_skips_leads_of_v2_numbers(v2, monkeypatch):
    async def _get(*a, **kw):
        return [
            ("A", "r", 1000, "T-v2"),
            ("B", "r", 2000, "T-old"),
            ("C", "r", 3000, "T-v2"),
        ]

    async def _is_v2(t):
        return t == "T-v2"

    monkeypatch.setattr(rc, "v2_seen", v2.seen)
    monkeypatch.setattr(rc, "get_unscheduled_backlog_leads", _get)
    monkeypatch.setattr(rc, "_is_v2_template", _is_v2)
    await rc.reconcile_backlog_to_zset()
    assert _zset(v2) == {"B": 2000}


async def test_legacy_reconciler_does_not_check_v2_before_it_is_seen(v2, monkeypatch):
    v2.seen.return_value = False
    check = AsyncMock(return_value=True)

    async def _get(*a, **kw):
        return [("A", "r", 1000, "T-v2")]

    monkeypatch.setattr(rc, "v2_seen", v2.seen)
    monkeypatch.setattr(rc, "get_unscheduled_backlog_leads", _get)
    monkeypatch.setattr(rc, "_is_v2_template", check)
    await rc.reconcile_backlog_to_zset()
    assert _zset(v2) == {"A": 1000}
    check.assert_not_awaited()
