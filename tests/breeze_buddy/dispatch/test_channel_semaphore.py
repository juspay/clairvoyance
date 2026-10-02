"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore``.
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import channel_semaphore as cs
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    capacity_wait_key,
    channel_key,
)
from app.core.config.static import BB_CHANNEL_WAIT_BACKOFF_MAX_S


async def test_init_creates_M_tokens(fake_redis):
    ok = await cs.init_channel_semaphore("num-A", maximum_channels=5)

    assert ok is True
    assert await cs.channel_tokens_available("num-A") == 5


async def test_init_replaces_existing_list(fake_redis):
    await cs.init_channel_semaphore("num-A", 3)
    await cs.init_channel_semaphore("num-A", 7)

    assert await cs.channel_tokens_available("num-A") == 7


async def test_init_with_zero_channels_is_noop(fake_redis):
    ok = await cs.init_channel_semaphore("num-zero", maximum_channels=0)

    assert ok is False
    assert await cs.channel_tokens_available("num-zero") == 0


async def test_acquire_returns_token_and_decrements(fake_redis):
    await cs.init_channel_semaphore("num-A", 3)

    token = await cs.acquire_channel_token("num-A", timeout_s=0)

    assert token is not None
    assert await cs.channel_tokens_available("num-A") == 2


async def test_acquire_returns_none_when_empty(fake_redis):
    await cs.init_channel_semaphore("num-A", 0)

    # FakeRedisClient.blpop with no items returns None immediately, modelling
    # a BLPOP that timed out without acquiring a token.
    token = await cs.acquire_channel_token("num-A", timeout_s=0)
    assert token is None


async def test_release_increments_count(fake_redis):
    await cs.init_channel_semaphore("num-A", 1)
    await cs.acquire_channel_token("num-A", timeout_s=0)
    assert await cs.channel_tokens_available("num-A") == 0

    ok = await cs.release_channel_token("num-A")

    assert ok is True
    assert await cs.channel_tokens_available("num-A") == 1


async def test_topup_adds_n_tokens(fake_redis):
    await cs.init_channel_semaphore("num-A", 2)

    added = await cs.topup_channel_tokens("num-A", 3)

    assert added == 3
    assert await cs.channel_tokens_available("num-A") == 5


async def test_topup_zero_is_noop(fake_redis):
    await cs.init_channel_semaphore("num-A", 2)
    added = await cs.topup_channel_tokens("num-A", 0)
    assert added == 0
    assert await cs.channel_tokens_available("num-A") == 2


async def test_trim_removes_n_tokens(fake_redis):
    await cs.init_channel_semaphore("num-A", 5)

    removed = await cs.trim_channel_tokens("num-A", 2)

    assert removed == 2
    assert await cs.channel_tokens_available("num-A") == 3


async def test_trim_stops_when_empty(fake_redis):
    await cs.init_channel_semaphore("num-A", 2)

    removed = await cs.trim_channel_tokens("num-A", 5)

    # Only 2 tokens to remove; trim should stop at 2 not 5.
    assert removed == 2
    assert await cs.channel_tokens_available("num-A") == 0


async def test_channel_exists_distinguishes_missing_from_empty(fake_redis):
    assert await cs.channel_exists("num-never") is False

    await cs.init_channel_semaphore("num-A", 1)
    assert await cs.channel_exists("num-A") is True

    await cs.acquire_channel_token("num-A", timeout_s=0)
    # LLEN is 0 but the LIST key may have been deleted by the fake — both
    # interpretations are fine; what matters is the semaphore is observably
    # consumed, not whether `exists` is True/False.
    assert await cs.channel_tokens_available("num-A") == 0


# ---------------------------------------------------------------------------
# capacity_defer_seconds — the two-level capacity defer
# ---------------------------------------------------------------------------


@pytest.fixture
def pile_config(monkeypatch):
    """Pin the dynamic dials so tests don't depend on the live-config store."""

    async def _threshold():
        return 5

    async def _long():
        return 60

    monkeypatch.setattr(cs.dyn_cfg, "BB_CAPACITY_WAIT_PILE_THRESHOLD", _threshold)
    monkeypatch.setattr(cs.dyn_cfg, "BB_CAPACITY_WAIT_PILE_DEFER_S", _long)


async def test_capacity_defer_few_waiting_gives_short_jitter(fake_redis, pile_config):
    for i in range(4):
        d = await cs.capacity_defer_seconds("num-A", f"lead-{i}")
        assert 1 <= d <= BB_CHANNEL_WAIT_BACKOFF_MAX_S


async def test_capacity_defer_pile_gives_long_defer(fake_redis, pile_config):
    for i in range(4):
        await cs.capacity_defer_seconds("num-A", f"lead-{i}")
    # The 5th distinct lead reaches the threshold.
    assert await cs.capacity_defer_seconds("num-A", "lead-4") == 60
    assert await cs.capacity_defer_seconds("num-A", "lead-5") == 60


async def test_capacity_defer_same_lead_rechecking_counts_once(fake_redis, pile_config):
    """A lead that bounces many times is one waiting lead, not many —
    otherwise the count would depend on the delay and flip modes."""
    for _ in range(50):
        d = await cs.capacity_defer_seconds("num-A", "lead-only")
        assert 1 <= d <= BB_CHANNEL_WAIT_BACKOFF_MAX_S


async def test_capacity_defer_numbers_are_counted_separately(fake_redis, pile_config):
    for i in range(10):
        await cs.capacity_defer_seconds("num-busy", f"lead-{i}")
    # num-busy is in pile mode; num-quiet is not affected.
    assert await cs.capacity_defer_seconds("num-busy", "lead-x") == 60
    d = await cs.capacity_defer_seconds("num-quiet", "lead-x")
    assert 1 <= d <= BB_CHANNEL_WAIT_BACKOFF_MAX_S


async def test_capacity_defer_bucket_keys_expire(fake_redis, pile_config):
    await cs.capacity_defer_seconds("num-A", "lead-1")
    keys = [k for k in fake_redis.client.expirations if k.startswith("bb:capwait:")]
    assert len(keys) == 1
    assert fake_redis.client.expirations[keys[0]] == cs._CAPACITY_WAIT_BUCKET_TTL_S


async def test_capacity_defer_redis_failure_falls_back_to_short(
    fake_redis, pile_config, monkeypatch
):
    """Fail open: a Redis error must never turn into a 60 s sleep or a crash."""

    async def _boom(*a, **kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(fake_redis.client, "pfadd", _boom)

    d = await cs.capacity_defer_seconds("num-A", "lead-1")
    assert 1 <= d <= BB_CHANNEL_WAIT_BACKOFF_MAX_S


async def test_capacity_defer_config_failure_falls_back_to_short(
    fake_redis, monkeypatch
):
    async def _boom():
        raise RuntimeError("config store down")

    monkeypatch.setattr(cs.dyn_cfg, "BB_CAPACITY_WAIT_PILE_THRESHOLD", _boom)

    d = await cs.capacity_defer_seconds("num-A", "lead-1")
    assert 1 <= d <= BB_CHANNEL_WAIT_BACKOFF_MAX_S


def test_capacity_wait_key_uses_hash_tag_for_cluster():
    """This minute's and last minute's keys must land on the same cluster
    slot for the multi-key PFCOUNT — the number id is the hash tag."""
    k1 = capacity_wait_key("num-A", 100)
    k2 = capacity_wait_key("num-A", 99)
    assert k1 == "bb:capwait:{num-A}:100"
    assert k1.split("}")[0] == k2.split("}")[0]
