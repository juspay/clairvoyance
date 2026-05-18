"""
Unit tests for ``app.ai.voice.agents.breeze_buddy.dispatch.worker``.

The full ``Worker._dispatch`` flow involves many DB/HTTP collaborators; we
test the *guards* and the *runtime-flag short-circuits* here, which are the
parts unique to the dispatcher. The end-to-end happy path is best covered
by integration tests against a real Redis (out of scope for unit).
"""

from __future__ import annotations

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import worker as w
from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    READY_LIST,
    processing_list_for,
    reseller_paused_key,
    worker_heartbeat_key,
)


async def test_dispatch_globally_disabled_when_flag_false(fake_redis, monkeypatch):
    """BB_DISPATCH_ENABLED returns False → worker reports disabled."""

    async def _disabled() -> bool:
        return False

    monkeypatch.setattr(w.dyn_cfg, "BB_DISPATCH_ENABLED", _disabled)
    worker = w.Worker()
    assert await worker._dispatch_globally_disabled() is True


async def test_dispatch_globally_enabled_by_default(fake_redis):
    """Default (no override) → BB_DISPATCH_ENABLED falls back to True."""
    worker = w.Worker()
    assert await worker._dispatch_globally_disabled() is False


async def test_dispatch_globally_enabled_when_flag_true(fake_redis, monkeypatch):
    async def _enabled() -> bool:
        return True

    monkeypatch.setattr(w.dyn_cfg, "BB_DISPATCH_ENABLED", _enabled)
    worker = w.Worker()
    assert await worker._dispatch_globally_disabled() is False


async def test_dispatch_fails_open_when_config_raises(fake_redis, monkeypatch):
    """If the dynamic config layer raises, we treat it as enabled (fail-open)
    so a sick Redis / DevCycle can't silently halt dispatch.
    """

    async def _raises() -> bool:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(w.dyn_cfg, "BB_DISPATCH_ENABLED", _raises)
    worker = w.Worker()
    assert await worker._dispatch_globally_disabled() is False


async def test_reseller_paused_when_flag_set(fake_redis):
    fake_redis.client.kv[reseller_paused_key("res-1")] = "1"

    worker = w.Worker()
    assert await worker._reseller_paused("res-1") is True


async def test_reseller_not_paused_when_flag_missing(fake_redis):
    worker = w.Worker()
    assert await worker._reseller_paused("res-other") is False


async def test_heartbeat_loop_writes_to_redis(fake_redis, monkeypatch):
    """
    Drive the actual Worker._heartbeat_loop and assert it writes the
    heartbeat key (with TTL) via the production code path.
    """
    import asyncio

    # Shrink the refresh interval so the loop emits a write almost immediately.
    monkeypatch.setattr(w, "BB_WORKER_HEARTBEAT_REFRESH_S", 0.05)

    worker = w.Worker(worker_uuid="w-test")
    worker._stopping.clear()
    task = asyncio.create_task(worker._heartbeat_loop())

    # Let at least one tick fire.
    await asyncio.sleep(0.1)

    assert fake_redis.client.kv[worker_heartbeat_key("w-test")] == "1"
    assert (
        fake_redis.client.expirations[worker_heartbeat_key("w-test")]
        == w.BB_WORKER_HEARTBEAT_TTL_S
    )

    worker._stopping.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_blpop_ready_returns_none_on_empty(fake_redis):
    worker = w.Worker()
    result = await worker._blpop_ready()
    assert result is None


async def test_blpop_ready_returns_lead_id_when_present(fake_redis):
    fake_redis.client.lists[READY_LIST] = ["lead-7"]

    worker = w.Worker()
    result = await worker._blpop_ready()

    assert result == "lead-7"


async def test_processing_list_rpush_and_lrem_roundtrip(fake_redis):
    worker = w.Worker(worker_uuid="w-roundtrip")
    key = processing_list_for("w-roundtrip")

    await worker._rpush_processing("lead-99")
    assert fake_redis.client.lists[key] == ["lead-99"]

    await worker._lrem_processing("lead-99")
    assert fake_redis.client.lists[key] == []
