"""Two concurrent release() calls (a shielded release racing its cancel-path
sibling) both run the compare-and-DEL (so a Redis blip on one still lets the
other free the lock); the loser reports False and never trips over a token the
winner already cleared."""

import asyncio

import pytest

from app.services.redis.locks import RedisLock


class _SlowRedis:
    def __init__(self):
        self.calls = 0

    async def run_script(self, script, keys, args):
        self.calls += 1
        mine = self.calls  # compare-and-DEL: the first script to run wins
        await asyncio.sleep(0.01)  # the await the sibling release sneaks into
        return 1 if mine == 1 else 0


@pytest.mark.asyncio
async def test_concurrent_releases_one_wins_no_crash():
    redis = _SlowRedis()
    lock = RedisLock("k", redis_service=redis)  # type: ignore[arg-type]
    lock._token = "tok-1234567890"

    first, second = await asyncio.gather(
        asyncio.shield(lock.release()), asyncio.shield(lock.release())
    )

    assert sorted([first, second]) == [False, True]
    assert redis.calls == 2  # both ran the script; Redis decided the winner
    assert lock.token is None


class _FailingRedis:
    def __init__(self):
        self.calls = 0

    async def run_script(self, script, keys, args):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("redis blip")
        return 1


@pytest.mark.asyncio
async def test_failed_release_keeps_token_for_the_retry():
    redis = _FailingRedis()
    lock = RedisLock("k", redis_service=redis)  # type: ignore[arg-type]
    lock._token = "tok-1234567890"

    with pytest.raises(ConnectionError):
        await lock.release()
    assert lock.token == "tok-1234567890"  # handed back, not lost until TTL

    assert await lock.release() is True  # the finally-path retry still works
    assert redis.calls == 2
    assert lock.token is None
