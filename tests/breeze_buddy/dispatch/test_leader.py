"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.leader``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.keys import PROMOTER_LEADER
from app.ai.voice.agents.breeze_buddy.dispatch.leader import LeaderElection


async def test_single_candidate_becomes_leader(fake_redis):
    L = LeaderElection(instance_id="pod-A")

    acquired = await L._try_acquire()

    assert acquired is True
    assert fake_redis.client.kv[PROMOTER_LEADER] == "pod-A"


async def test_two_candidates_first_wins(fake_redis):
    a = LeaderElection(instance_id="pod-A")
    b = LeaderElection(instance_id="pod-B")

    assert await a._try_acquire() is True
    assert await b._try_acquire() is False

    assert fake_redis.client.kv[PROMOTER_LEADER] == "pod-A"


async def test_renew_succeeds_for_current_holder(fake_redis):
    a = LeaderElection(instance_id="pod-A")
    await a._try_acquire()

    ok = await a._try_renew()

    assert ok is True


async def test_renew_fails_for_non_holder(fake_redis):
    a = LeaderElection(instance_id="pod-A")
    b = LeaderElection(instance_id="pod-B")
    await a._try_acquire()

    ok = await b._try_renew()

    assert ok is False


async def test_release_clears_lock_only_for_holder(fake_redis):
    a = LeaderElection(instance_id="pod-A")
    b = LeaderElection(instance_id="pod-B")
    await a._try_acquire()

    # B tries to release — must not affect A's lock.
    await b._release()
    assert fake_redis.client.kv.get(PROMOTER_LEADER) == "pod-A"

    # A releases — lock is cleared.
    await a._release()
    assert PROMOTER_LEADER not in fake_redis.client.kv


async def test_loop_acquires_then_renews(fake_redis):
    """
    Start the loop, wait a renewal interval, confirm leadership is held.
    """
    L = LeaderElection(instance_id="pod-loop")

    await L.start()
    # Give the loop one tick to acquire.
    await asyncio.sleep(0.05)

    assert L.is_leader is True

    await L.stop()
    assert L.is_leader is False
