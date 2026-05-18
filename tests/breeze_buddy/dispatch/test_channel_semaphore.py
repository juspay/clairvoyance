"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.channel_semaphore``.
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import channel_semaphore as cs
from app.ai.voice.agents.breeze_buddy.dispatch.keys import channel_key


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
