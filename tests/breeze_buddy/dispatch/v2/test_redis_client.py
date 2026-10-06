"""The dialler's Redis client never retries or sleeps inside the library: a v2 script is
idempotent by (ticket id, owner) and every caller treats None as "not done"."""

import time

import pytest
import redis.asyncio as aioredis

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import redis_client as RC, scripts

pytestmark = pytest.mark.asyncio


async def test_the_client_is_built_without_library_retries(monkeypatch):
    monkeypatch.setattr(RC, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(RC, "REDIS_PORT", "6390")
    monkeypatch.setattr(RC, "_client", None)
    c = await RC.v2_redis()
    try:
        assert c is await RC.v2_redis()  # one client per process
        retry = c.get_retry()
        assert retry is not None and retry.get_retries() == 0
        kw = c.connection_pool.connection_kwargs
        assert kw["socket_timeout"] == RC.BB_V2_REDIS_SOCKET_TIMEOUT_S >= 2
        assert kw["socket_connect_timeout"] == 2.0
    finally:
        await RC.close_v2_redis()
    assert RC._client is None


async def test_a_dead_redis_answers_none_without_sleeping(monkeypatch):
    dead = aioredis.Redis(
        host="127.0.0.1", port=1, decode_responses=True, retry=RC._no_retry()
    )
    monkeypatch.setattr(RC, "_client", dead)
    t0 = time.monotonic()
    assert await scripts.match("N1") is None
    assert await scripts.claim("N1", "L1", 1, "o") is False  # two tries, no sleep
    assert time.monotonic() - t0 < 0.5  # redis-py's default backs off 1-10 s
    await dead.aclose()
