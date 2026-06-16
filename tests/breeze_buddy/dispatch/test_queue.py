"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.queue``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import queue
from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET


async def test_schedule_lead_writes_to_zset_with_correct_score(fake_redis):
    when = datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc)
    expected_ms = int(when.timestamp() * 1000)

    ok = await queue.schedule_lead("lead-1", when, jitter_ms=0)

    assert ok is True
    score = fake_redis.client.zsets[SCHEDULE_ZSET]["lead-1"]
    assert int(score) == expected_ms


async def test_schedule_lead_applies_jitter_within_bounds(fake_redis, monkeypatch):
    """
    Monkeypatch the RNG so we can assert the EXACT offset the jitter logic
    produced. A range-only check would let a no-jitter implementation pass.
    """
    when = datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc)
    base_ms = int(when.timestamp() * 1000)
    jitter = 250
    expected_offset = 137  # arbitrary; must be within [-jitter, +jitter]

    def fake_randint(low: int, high: int) -> int:
        # Sanity: the queue module should call randint(-jitter, +jitter).
        assert low == -jitter and high == jitter
        return expected_offset

    monkeypatch.setattr(queue.random, "randint", fake_randint)

    await queue.schedule_lead("lead-j", when, jitter_ms=jitter)

    score = fake_redis.client.zsets[SCHEDULE_ZSET]["lead-j"]
    assert int(score) == base_ms + expected_offset


async def test_schedule_lead_zero_jitter_pins_exactly(fake_redis):
    """Operator dispatch-now path: jitter=0 must mean exact 'now'."""
    when = datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc)
    expected_ms = int(when.timestamp() * 1000)

    await queue.schedule_lead("lead-op", when, jitter_ms=0)

    assert int(fake_redis.client.zsets[SCHEDULE_ZSET]["lead-op"]) == expected_ms


async def test_schedule_lead_zadd_overwrite_is_idempotent(fake_redis):
    """Repeat ZADDs of the same lead overwrite the score, don't duplicate."""
    t1 = datetime(2026, 5, 14, 9, 30, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 14, 9, 35, tzinfo=timezone.utc)

    await queue.schedule_lead("lead-1", t1, jitter_ms=0)
    await queue.schedule_lead("lead-1", t2, jitter_ms=0)

    assert await queue.get_schedule_size() == 1
    assert int(fake_redis.client.zsets[SCHEDULE_ZSET]["lead-1"]) == int(
        t2.timestamp() * 1000
    )


async def test_cancel_scheduled_lead_removes_member(fake_redis):
    when = datetime(2026, 5, 14, 9, 30, tzinfo=timezone.utc)
    await queue.schedule_lead("lead-c", when, jitter_ms=0)

    ok = await queue.cancel_scheduled_lead("lead-c")

    assert ok is True
    assert "lead-c" not in fake_redis.client.zsets.get(SCHEDULE_ZSET, {})


async def test_cancel_scheduled_lead_missing_is_safe(fake_redis):
    """ZREM on a non-member is a no-op — must not raise."""
    ok = await queue.cancel_scheduled_lead("never-scheduled")
    assert ok is True


async def test_get_scheduled_score_returns_none_when_missing(fake_redis):
    assert await queue.get_scheduled_score("ghost") is None


async def test_get_schedule_size_reflects_zcard(fake_redis):
    base = datetime(2026, 5, 14, 9, 30, tzinfo=timezone.utc)
    for i in range(5):
        await queue.schedule_lead(f"lead-{i}", base, jitter_ms=0)

    assert await queue.get_schedule_size() == 5


async def test_schedule_lead_does_not_raise_on_redis_error(monkeypatch):
    """Best-effort contract: ZADD failure logs and returns False, no raise."""

    class BrokenRedis:
        async def get_client(self):
            raise RuntimeError("simulated outage")

    async def _get():
        return BrokenRedis()

    monkeypatch.setattr(queue, "get_redis_service", _get)

    when = datetime(2026, 5, 14, 9, 30, tzinfo=timezone.utc)
    ok = await queue.schedule_lead("lead-x", when)

    assert ok is False


# ---------------------------------------------------------------------------
# Execution-mode gating
# ---------------------------------------------------------------------------


def test_is_dispatchable_outbound_pstn_modes_only():
    """Only standalone outbound PSTN modes should pass the gate.

    DAILY / DAILY_TEST / DAILY_STREAM are web-mode (customer joins a Daily
    room) — they must NOT enter the dispatcher's PSTN-dial path. HOLD_TRANSFER
    is a mid-call leg, not a standalone outbound to schedule. This filter
    mirrors the SQL WHERE clause in get_unscheduled_backlog_leads_query.
    """
    from app.schemas import ExecutionMode

    assert queue.is_dispatchable(ExecutionMode.TELEPHONY) is True
    assert queue.is_dispatchable(ExecutionMode.TELEPHONY_TEST) is True
    assert queue.is_dispatchable(ExecutionMode.TELEPHONY_ALERT) is True
    assert queue.is_dispatchable(ExecutionMode.DAILY) is False
    assert queue.is_dispatchable(ExecutionMode.DAILY_TEST) is False
    assert queue.is_dispatchable(ExecutionMode.DAILY_STREAM) is False
    assert queue.is_dispatchable(ExecutionMode.HOLD_TRANSFER) is False
