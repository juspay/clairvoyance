"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.promoter``.
"""

from __future__ import annotations

import time

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import promoter as p
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    PROMOTER_PAUSED,
    READY_LIST,
    SCHEDULE_ZSET,
)


def _seed_due_leads(fake_redis, ids):
    """Put leads on the schedule with score = now - 1s (already due)."""
    score = int(time.time() * 1000) - 1000
    z = fake_redis.client.zsets.setdefault(SCHEDULE_ZSET, {})
    for lead_id in ids:
        z[lead_id] = score


def _seed_future_leads(fake_redis, ids):
    """Put leads on the schedule with score in the far future."""
    score = int(time.time() * 1000) + 60_000
    z = fake_redis.client.zsets.setdefault(SCHEDULE_ZSET, {})
    for lead_id in ids:
        z[lead_id] = score


async def test_promoter_does_nothing_when_not_leader(fake_redis):
    prom = p.Promoter()
    # Don't start leader election — is_leader stays False.
    _seed_due_leads(fake_redis, ["a", "b"])

    moved = await prom._tick_once()

    assert moved == 0
    assert "a" in fake_redis.client.zsets[SCHEDULE_ZSET]


async def test_promoter_moves_due_leads_when_leader(fake_redis):
    prom = p.Promoter()
    await prom._leader.start()
    # Force leadership.
    prom._leader._is_leader = True
    _seed_due_leads(fake_redis, ["a", "b", "c"])

    moved = await prom._tick_once()

    assert moved == 3
    assert fake_redis.client.zsets.get(SCHEDULE_ZSET, {}) == {}
    # Order from LPUSH is reversed.
    assert set(fake_redis.client.lists.get(READY_LIST, [])) == {"a", "b", "c"}

    await prom._leader.stop()


async def test_promoter_skips_future_leads(fake_redis):
    prom = p.Promoter()
    await prom._leader.start()
    prom._leader._is_leader = True
    _seed_future_leads(fake_redis, ["x"])

    moved = await prom._tick_once()

    assert moved == 0
    assert "x" in fake_redis.client.zsets[SCHEDULE_ZSET]
    assert fake_redis.client.lists.get(READY_LIST, []) == []

    await prom._leader.stop()


async def test_promoter_respects_pause_flag(fake_redis):
    """If bb:promoter:paused exists, the tick must be a no-op."""
    prom = p.Promoter()
    await prom._leader.start()
    prom._leader._is_leader = True
    fake_redis.client.kv[PROMOTER_PAUSED] = "1"
    _seed_due_leads(fake_redis, ["a"])

    moved = await prom._tick_once()

    assert moved == 0
    assert "a" in fake_redis.client.zsets[SCHEDULE_ZSET]

    await prom._leader.stop()


async def test_promoter_promotion_is_atomic_when_zrem_loses(fake_redis):
    """
    Simulate the split-brain absorption clause: pre-remove a lead from the
    ZSET between our hand-check and the Lua move. The lua promotes whatever
    is there now — verifying it doesn't double-LPUSH something that was
    already removed.
    """
    prom = p.Promoter()
    await prom._leader.start()
    prom._leader._is_leader = True
    _seed_due_leads(fake_redis, ["a", "b"])

    # Simulate another promoter winning the race for "a".
    await fake_redis.client.zrem(SCHEDULE_ZSET, "a")

    moved = await prom._tick_once()

    assert moved == 1
    assert fake_redis.client.lists.get(READY_LIST, []) == ["b"]

    await prom._leader.stop()
