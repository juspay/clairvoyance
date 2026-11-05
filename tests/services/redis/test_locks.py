"""RedisLock release semantics -- no real Redis.

Regression for the prod ``Task exception was never retrieved ...
TypeError("'NoneType' object is not subscriptable")`` seen when the chat
handler released the per-session lock twice on one instance (a shielded
background release overlapping the ``finally`` release). Both calls
passed the ``_token is None`` guard before either awaited Redis; the
first cleared the token, the second's "lock lost" warning then formatted
``None[:8]``.
"""

import asyncio
from typing import Any, List, cast

import pytest

from app.services.redis.client import RedisService
from app.services.redis.locks import RedisLock


class _FakeRedisService:
    """Minimal stand-in: SET NX always succeeds; run_script blocks on an
    event so two ``release()`` calls can be made to overlap deterministically."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.script_calls: List[List[Any]] = []
        self.script_result: int = 1

    async def set(self, key: str, value: str, nx: bool = False, ex=None) -> bool:
        return True

    async def run_script(self, script: str, keys: List[str], args: List[Any]) -> Any:
        self.script_calls.append(list(args))
        await self.gate.wait()
        return self.script_result


def _lock(fake: _FakeRedisService) -> RedisLock:
    return RedisLock("test:lock", ttl_seconds=5, redis_service=cast(RedisService, fake))


async def test_single_release_returns_true_and_clears_token():
    fake = _FakeRedisService()
    fake.gate.set()
    lock = _lock(fake)
    token = await lock.acquire()

    assert await lock.release() is True
    assert lock.token is None
    assert fake.script_calls == [[token]]


async def test_release_without_token_returns_false_without_redis():
    fake = _FakeRedisService()
    fake.gate.set()
    lock = _lock(fake)

    assert await lock.release() is False
    assert fake.script_calls == []


async def test_concurrent_release_on_same_instance_is_safe():
    """Two overlapping release() calls: no exception, exactly one True,
    the other False, and the Lua script runs once."""
    fake = _FakeRedisService()
    lock = _lock(fake)
    await lock.acquire()

    first = asyncio.create_task(lock.release())
    await asyncio.sleep(0)  # first release is now parked inside run_script
    second = asyncio.create_task(lock.release())
    await asyncio.sleep(0)
    fake.gate.set()

    results = await asyncio.gather(first, second)

    assert sorted(results) == [False, True]
    assert len(fake.script_calls) == 1
    assert lock.token is None


async def test_lost_lock_warning_formats_without_error():
    """When Redis reports the key is no longer ours (TTL elapsed / stolen),
    release() returns False and still logs the warning (kept on purpose)."""
    fake = _FakeRedisService()
    fake.gate.set()
    fake.script_result = 0
    lock = _lock(fake)
    await lock.acquire()

    assert await lock.release() is False
    assert lock.token is None
