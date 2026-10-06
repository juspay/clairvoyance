"""schedule_lead routes v2 numbers' leads into rooms; everything else is today's ZADD."""

import datetime as dt
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import queue, reconcilers as rc
from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import (
    Rank,
    rank_from_priority,
)
from tests.breeze_buddy.dispatch.v2.conftest import seed_number

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


# -- ranks (a number whose bb:num:{N}.ranked is 1) --------------------------------------------

NEED_RANK = -4


def _row(monkeypatch, meta):
    """The lead row ``_lead_rank`` reads: its meta_data, or None for no (readable) row."""
    import app.database.accessor as accessor

    read = AsyncMock(return_value=None if meta is None else NS(metaData=meta))
    monkeypatch.setattr(accessor, "get_lead_by_id", read)
    return read


async def test_a_rank_is_passed_to_the_enqueue_and_is_keyword_only(v2):
    rank = Rank(2, "n", 1_700_000_000_000)
    assert await queue.schedule_lead("L1", NOW, template_id="T1", rank=rank) is True
    v2.enqueue.assert_awaited_once_with(
        "T1", "L1", queue._to_unix_ms(NOW), False, rank=rank
    )
    with pytest.raises(TypeError):
        # pyrefly: ignore[bad-argument-count]
        await queue.schedule_lead("L1", NOW, None, "T1", False, rank)


async def test_unranked_number_makes_no_db_read(v2, monkeypatch):
    read = _row(monkeypatch, {"priority": {"rank": 2}})
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    read.assert_not_awaited()


async def test_need_rank_reads_the_lead_row_once(v2, monkeypatch):
    priority = {"rank": 3, "order": "newest_event", "event_ms": 1_791_522_600_123}
    read = _row(monkeypatch, {"workflow_id": "W", "priority": priority})
    v2.enqueue.side_effect = [-1, NEED_RANK, 1]  # route missing, then the rank question
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    read.assert_awaited_once_with("L1")
    assert v2.enqueue.await_count == 3
    assert v2.enqueue.await_args.kwargs == {"rank": Rank(3, "n", 1_791_522_600_123)}


async def test_lead_without_priority_meta_gets_default(v2, monkeypatch):
    _row(monkeypatch, {"workflow_id": "W"})  # the row exists, it carries no rank
    v2.enqueue.side_effect = [NEED_RANK, 1]
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    assert v2.enqueue.await_args.kwargs == {"rank": Rank(0, "f", 0)}
    assert (
        rank_from_priority({"rank": "x"})
        == rank_from_priority("junk")
        == Rank(0, "f", 0)
    )
    assert rank_from_priority({"rank": 2, "order": "n", "event_ms": 5}) == Rank(
        2, "n", 5
    )


async def test_unreadable_lead_row_is_not_queued_at_the_default_rank(v2, monkeypatch):
    # no row, or the DB could not be read: a pile lead must not become rank 1 by accident
    _row(monkeypatch, None)
    v2.enqueue.return_value = NEED_RANK
    assert await queue.schedule_lead("L1", NOW, jitter_ms=0, template_id="T1") is False
    v2.enqueue.assert_awaited_once()
    assert _zset(v2) == {}  # never today's schedule: v2 owns the number


async def test_cancel_clears_the_remembered_score(fake_redis, monkeypatch):
    monkeypatch.setattr(queue, "v2_seen", AsyncMock(return_value=True))
    fake_redis.client.hdel = AsyncMock(return_value=1)
    fake_redis.client.zsets["bb:q:T1"] = {"L1": 1.0}
    assert await queue.cancel_scheduled_lead("L1", template_id="T1") is True
    assert fake_redis.client.zsets["bb:q:T1"] == {}
    fake_redis.client.hdel.assert_awaited_once_with("bb:qp:T1", "L1")


async def test_backlog_rows_carry_ranks_and_need_rank_goes_alone(v2, monkeypatch):
    many = AsyncMock(return_value=[0, NEED_RANK])
    monkeypatch.setattr(queue.v2_scripts, "enqueue_many", many)
    rows = [("L1", NOW, "T1", Rank(2, "n", 4)), ("L2", NOW, "T1")]
    assert await queue.schedule_backlog_v2(rows) == 2
    ms = queue._to_unix_ms(NOW)
    many.assert_awaited_once_with(
        [("T1", "L1", ms, Rank(2, "n", 4)), ("T1", "L2", ms)], only_if_absent=True
    )
    v2.enqueue.assert_awaited_once_with("T1", "L2", ms, True)  # schedule_lead, alone


async def test_ranked_number_end_to_end_queues_the_lead_in_its_rows_band(
    rr, monkeypatch
):
    await seed_number(rr, "N1", 0, {"T1": {}})
    await rr.hset("bb:num:N1", mapping={"ranked": "1", "live_day": "0"})
    monkeypatch.setattr(queue, "v2_seen", AsyncMock(return_value=True))
    read = _row(monkeypatch, {"priority": {"rank": 3, "order": "n", "event_ms": 9}})
    assert await queue.schedule_lead("L1", NOW, template_id="T1") is True
    read.assert_awaited_once_with("L1")
    assert await rr.zscore("bb:q:T1", "L1") == (3 - 100) * 10**13 + (10**13 - 1) - 9
