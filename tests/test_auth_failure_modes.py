"""What the auth-hardening layers do when their infrastructure misbehaves.

Two failure modes are deliberately asymmetric and are easy to get backwards, so
they are pinned here:

* ``is_user_active`` — a Redis failure fails OPEN (the cache is only an
  optimisation and the authoritative read still runs), a database failure fails
  CLOSED (returning True there would accept tokens for disabled and deleted
  users for the whole outage).
* ``check_rate_limit`` — INCR and EXPIRE are two round trips and only the first
  request in a window sets the TTL, so a counter that survives a failed EXPIRE
  can never be repaired and would 429 a fail-closed caller forever.
"""

from __future__ import annotations

import pytest

from app.database.accessor.breeze_buddy import users as users_accessor
from app.services.redis import rate_limit as svc


class _RedisMiss:
    """Cache that answers "not cached" and accepts writes."""

    def __init__(self):
        self.writes: list = []

    async def get(self, key):
        return None

    async def setex(self, key, value, ttl_seconds=None):
        self.writes.append((key, value))
        return True


def _patch_redis(monkeypatch, redis):
    async def _get():
        return redis

    monkeypatch.setattr(users_accessor, "get_redis_service", _get)


# ── is_user_active ────────────────────────────────────────────────────────
async def test_db_failure_fails_closed(monkeypatch):
    _patch_redis(monkeypatch, _RedisMiss())

    async def _boom(user_id):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(users_accessor, "get_user_in_db_by_id", _boom)
    # The security-relevant direction: an unreachable database must not be a
    # window in which a revoked or deactivated account keeps authenticating.
    assert await users_accessor.is_user_active("u1") is False


async def test_redis_read_failure_still_consults_the_database(monkeypatch):
    async def _boom_redis():
        raise RuntimeError("redis down")

    monkeypatch.setattr(users_accessor, "get_redis_service", _boom_redis)

    class _Active:
        is_active = True

    async def _db(user_id):
        return _Active()

    monkeypatch.setattr(users_accessor, "get_user_in_db_by_id", _db)
    # Redis is only a cache: losing it degrades latency, never correctness.
    assert await users_accessor.is_user_active("u1") is True


async def test_inactive_user_is_reported_inactive(monkeypatch):
    _patch_redis(monkeypatch, _RedisMiss())

    class _Disabled:
        is_active = False

    async def _db(user_id):
        return _Disabled()

    monkeypatch.setattr(users_accessor, "get_user_in_db_by_id", _db)
    assert await users_accessor.is_user_active("u1") is False


async def test_missing_user_is_reported_inactive(monkeypatch):
    _patch_redis(monkeypatch, _RedisMiss())

    async def _db(user_id):
        return None

    monkeypatch.setattr(users_accessor, "get_user_in_db_by_id", _db)
    assert await users_accessor.is_user_active("deleted") is False


# ── check_rate_limit TTL repair ───────────────────────────────────────────
class _FakeLimiterRedis:
    def __init__(self, *, expire_ok: bool = True):
        self.counts: dict = {}
        self.expire_ok = expire_ok
        self.deleted: list = []

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        return self.expire_ok

    async def delete(self, key):
        self.deleted.append(key)
        self.counts.pop(key, None)
        return True


@pytest.fixture(autouse=True)
def _redis_configured(monkeypatch):
    monkeypatch.setattr(svc, "is_redis_configured", lambda: True)


async def test_counter_is_dropped_when_its_ttl_cannot_be_set(monkeypatch):
    fake = _FakeLimiterRedis(expire_ok=False)

    async def _get():
        return fake

    monkeypatch.setattr(svc, "get_redis_service", _get)
    decision = await svc.check_rate_limit(
        bucket="b", identifier="i", limit=5, window_seconds=60, fail_closed=True
    )
    # The request itself is still allowed — the point is that the key does not
    # survive without a TTL, because nothing downstream could ever repair it.
    assert decision.allowed is True
    assert fake.deleted, "counter without a TTL must be deleted, not left immortal"
    assert fake.counts == {}


async def test_normal_path_keeps_the_counter(monkeypatch):
    fake = _FakeLimiterRedis(expire_ok=True)

    async def _get():
        return fake

    monkeypatch.setattr(svc, "get_redis_service", _get)
    for _ in range(3):
        decision = await svc.check_rate_limit(
            bucket="b", identifier="i", limit=5, window_seconds=60
        )
    assert decision.allowed is True
    assert fake.deleted == []
    assert list(fake.counts.values()) == [3]


async def test_over_limit_still_denies(monkeypatch):
    fake = _FakeLimiterRedis(expire_ok=True)

    async def _get():
        return fake

    monkeypatch.setattr(svc, "get_redis_service", _get)
    decisions = [
        await svc.check_rate_limit(
            bucket="b", identifier="i", limit=2, window_seconds=60
        )
        for _ in range(3)
    ]
    assert [d.allowed for d in decisions] == [True, True, False]
