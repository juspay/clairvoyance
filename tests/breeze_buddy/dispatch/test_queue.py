"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.queue``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import queue
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    SCHEDULE_ZSET,
    lead_tier_key,
)


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


def test_is_dispatchable_telephony_modes_only():
    """Only TELEPHONY and TELEPHONY_TEST should pass the gate.

    DAILY / DAILY_TEST / DAILY_STREAM are web-mode (customer joins a Daily
    room) — they must NOT enter the dispatcher's PSTN-dial path. HOLD_TRANSFER
    is a mid-call leg, not a standalone outbound to schedule. This filter
    mirrors the SQL WHERE clause in get_unscheduled_backlog_leads_query.
    """
    from app.schemas import ExecutionMode

    assert queue.is_dispatchable(ExecutionMode.TELEPHONY) is True
    assert queue.is_dispatchable(ExecutionMode.TELEPHONY_TEST) is True
    assert queue.is_dispatchable(ExecutionMode.DAILY) is False
    assert queue.is_dispatchable(ExecutionMode.DAILY_TEST) is False
    assert queue.is_dispatchable(ExecutionMode.DAILY_STREAM) is False
    assert queue.is_dispatchable(ExecutionMode.HOLD_TRANSFER) is False


# ---------------------------------------------------------------------------
# Merchant tiers — the hint schedule_lead leaves for the promoter
# ---------------------------------------------------------------------------


def _pin_tiers(monkeypatch, high=(), medium=()):
    async def _high():
        return list(high)

    async def _medium():
        return list(medium)

    monkeypatch.setattr(queue.dyn_cfg, "BB_PRIORITY_HIGH_MERCHANT_IDS", _high)
    monkeypatch.setattr(queue.dyn_cfg, "BB_PRIORITY_MEDIUM_MERCHANT_IDS", _medium)


async def test_schedule_lead_writes_tier_hint_for_priority_merchant(
    fake_redis, monkeypatch
):
    _pin_tiers(monkeypatch, high=["m-high"], medium=["m-med"])
    when = datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc)

    await queue.schedule_lead("lead-h", when, jitter_ms=0, merchant_id="m-high")
    await queue.schedule_lead("lead-m", when, jitter_ms=0, merchant_id="m-med")
    await queue.schedule_lead("lead-n", when, jitter_ms=0, merchant_id="m-other")

    kv = fake_redis.client.kv
    assert kv[lead_tier_key("lead-h")] == "high"
    assert kv[lead_tier_key("lead-m")] == "medium"
    assert lead_tier_key("lead-n") not in kv
    assert (
        fake_redis.client.expirations[lead_tier_key("lead-h")] == queue._LEAD_TIER_TTL_S
    )


async def test_schedule_lead_without_merchant_keeps_existing_hint(
    fake_redis, monkeypatch
):
    """Defer paths pass no merchant; the hint from the first schedule stays."""
    _pin_tiers(monkeypatch, high=["m-high"])
    when = datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc)

    await queue.schedule_lead("lead-h", when, jitter_ms=0, merchant_id="m-high")
    await queue.schedule_lead("lead-h", when, jitter_ms=0)  # a defer

    assert fake_redis.client.kv[lead_tier_key("lead-h")] == "high"


async def test_schedule_lead_clears_hint_when_merchant_leaves_a_tier(
    fake_redis, monkeypatch
):
    """Config changed: the next schedule with the merchant drops the hint."""
    _pin_tiers(monkeypatch, high=["m-x"])
    when = datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc)
    await queue.schedule_lead("lead-x", when, jitter_ms=0, merchant_id="m-x")
    assert lead_tier_key("lead-x") in fake_redis.client.kv

    _pin_tiers(monkeypatch)  # m-x removed from every tier
    await queue.schedule_lead("lead-x", when, jitter_ms=0, merchant_id="m-x")

    assert lead_tier_key("lead-x") not in fake_redis.client.kv


async def test_merchant_in_both_lists_is_high(fake_redis, monkeypatch):
    _pin_tiers(monkeypatch, high=["m-both"], medium=["m-both"])
    assert await queue.merchant_tier("m-both") == "high"


async def test_merchant_tier_none_and_config_error_are_normal(fake_redis, monkeypatch):
    assert await queue.merchant_tier(None) is None

    async def _boom():
        raise RuntimeError("config down")

    monkeypatch.setattr(queue.dyn_cfg, "BB_PRIORITY_HIGH_MERCHANT_IDS", _boom)
    assert await queue.merchant_tier("m-high") is None


async def test_tier_hint_failure_does_not_fail_scheduling(fake_redis, monkeypatch):
    """The ZADD is what dispatch needs; a hint write error must not undo it."""
    _pin_tiers(monkeypatch, high=["m-high"])
    when = datetime(2026, 5, 14, 9, 30, 0, tzinfo=timezone.utc)

    async def _boom(*a, **kw):
        raise RuntimeError("redis hiccup")

    monkeypatch.setattr(fake_redis.client, "set", _boom)

    ok = await queue.schedule_lead("lead-h", when, jitter_ms=0, merchant_id="m-high")
    assert ok is True
    assert "lead-h" in fake_redis.client.zsets[SCHEDULE_ZSET]
