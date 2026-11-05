"""The per-process memo of the dynamic-config blob (live_config/store.py).

Every get_config() used to GET and json.loads the whole devcycle:flags blob
to return one key. The memo keeps the parsed blob for
LIVE_CONFIG_MEMO_TTL_SECONDS; these tests pin what it may and may not do:
one GET per TTL, one GET for a crowd, failures never remembered, local
writes seen at once, TTL 0 = the old path, and no caller able to change
what the next caller reads.

Time is driven through store._monotonic (never time.monotonic: the event
loop runs on it), Redis through a fake client in the style of
tests/breeze_buddy/dispatch/conftest.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from app.services.live_config import store

BLOB: Dict[str, Any] = {
    "BB_DISPATCH_ENABLED": True,
    "OUTBOUND_RATE_LIMIT_MAX_CALLS": 7,
    "OUTBOUND_RATE_LIMIT_WINDOW_SECONDS": 3600,
    "OUTBOUND_RATE_LIMIT_BLOCK_ENABLED": False,
    "SOME_LIST": ["a", "b"],
    "SOME_DICT": {"voice_id": "x", "speed": 1.0, "tags": ["t"]},
}


class FakeFlagsClient:
    """Just enough of the redis-py async client: GET/SET on a dict, a GET
    counter, an optional gate that holds every GET open, and an optional
    error to raise from GET."""

    def __init__(self, blob: Optional[Dict[str, Any]] = None) -> None:
        self.kv: Dict[str, str] = {}
        if blob is not None:
            self.kv[store.FEATURE_FLAGS_KEY] = json.dumps(blob)
        self.gets = 0
        self.sets: List[str] = []
        self.gate: Optional[asyncio.Event] = None
        self.fail_with: Optional[BaseException] = None

    async def get(self, key: str) -> Optional[str]:
        self.gets += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        return self.kv.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.sets.append(key)
        self.kv[key] = value
        return True


class FakeService:
    def __init__(self, client: FakeFlagsClient) -> None:
        self.client = client

    async def get_client(self) -> FakeFlagsClient:
        return self.client


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _cold_memo():
    """Each case starts with no memo and leaves none behind (module globals)."""
    store.invalidate_flags_memo()
    yield
    store.invalidate_flags_memo()


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(store, "_monotonic", c)
    return c


@pytest.fixture
def ttl(monkeypatch):
    def _set(seconds: float) -> None:
        monkeypatch.setattr(store, "LIVE_CONFIG_MEMO_TTL_SECONDS", seconds)

    _set(5.0)
    return _set


@pytest.fixture
def redis(monkeypatch) -> FakeFlagsClient:
    client = FakeFlagsClient(BLOB)
    service = FakeService(client)

    async def _get_service() -> FakeService:
        return service

    monkeypatch.setattr(store, "get_redis_service", _get_service)
    monkeypatch.setattr(store, "ENABLE_REDIS_DYNAMIC_CONFIG", True)
    return client


async def test_reads_within_ttl_share_one_get(redis, clock, ttl) -> None:
    for _ in range(10):
        assert await store.get_config("OUTBOUND_RATE_LIMIT_MAX_CALLS", 1, int) == 7
        clock.now += 0.4  # 10 reads over 3.6s, all inside the 5s TTL
    assert redis.gets == 1


async def test_a_read_after_ttl_goes_back_to_redis(redis, clock, ttl) -> None:
    assert await store.get_config("BB_DISPATCH_ENABLED", False, bool) is True
    redis.kv[store.FEATURE_FLAGS_KEY] = json.dumps(
        {**BLOB, "BB_DISPATCH_ENABLED": False}
    )

    clock.now += 4.9
    assert await store.get_config("BB_DISPATCH_ENABLED", False, bool) is True
    assert redis.gets == 1

    clock.now += 0.2  # 5.1s after the read: expired
    assert await store.get_config("BB_DISPATCH_ENABLED", True, bool) is False
    assert redis.gets == 2


async def test_concurrent_cold_callers_share_one_get(redis, clock, ttl) -> None:
    redis.gate = asyncio.Event()
    calls = [
        asyncio.create_task(store.get_config("OUTBOUND_RATE_LIMIT_MAX_CALLS", 1, int))
        for _ in range(20)
    ]
    await asyncio.sleep(0)  # let all 20 reach the in-flight refresh
    await asyncio.sleep(0)
    redis.gate.set()
    assert await asyncio.gather(*calls) == [7] * 20
    assert redis.gets == 1


async def test_cancelling_one_waiter_does_not_break_the_others(
    redis, clock, ttl
) -> None:
    redis.gate = asyncio.Event()
    first = asyncio.create_task(store.get_config("BB_DISPATCH_ENABLED", False, bool))
    others = [
        asyncio.create_task(store.get_config("BB_DISPATCH_ENABLED", False, bool))
        for _ in range(5)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()  # the caller that started the refresh goes away
    await asyncio.sleep(0)
    redis.gate.set()
    results = await asyncio.gather(*others, return_exceptions=True)
    assert results == [True] * 5
    assert first.cancelled()
    assert redis.gets == 1


async def test_a_failed_get_is_not_remembered(redis, clock, ttl) -> None:
    redis.fail_with = ConnectionError("redis down")
    # Same as before the memo: logged, then env -> default.
    assert await store.get_config("OUTBOUND_RATE_LIMIT_MAX_CALLS", 1, int) == 1
    assert await store._get_flag_from_redis("OUTBOUND_RATE_LIMIT_MAX_CALLS") is None
    assert redis.gets == 2

    redis.fail_with = None  # Redis is back; the very next read must see it
    assert await store.get_config("OUTBOUND_RATE_LIMIT_MAX_CALLS", 1, int) == 7
    assert redis.gets == 3


async def test_a_missing_key_is_not_remembered(redis, clock, ttl) -> None:
    del redis.kv[store.FEATURE_FLAGS_KEY]
    assert await store._get_flag_from_redis("BB_DISPATCH_ENABLED") is None
    assert await store._get_all_flags_from_redis() == {}
    assert redis.gets == 2

    redis.kv[store.FEATURE_FLAGS_KEY] = json.dumps(BLOB)
    assert await store._get_flag_from_redis("BB_DISPATCH_ENABLED") is True
    assert redis.gets == 3


async def test_a_local_write_is_seen_at_once(redis, clock, ttl) -> None:
    assert await store.get_config("BB_DISPATCH_ENABLED", True, bool) is True
    assert redis.gets == 1

    await store.set_all_flags({**BLOB, "BB_DISPATCH_ENABLED": False})

    assert await store.get_config("BB_DISPATCH_ENABLED", True, bool) is False
    assert redis.gets == 2


async def test_a_refresh_racing_a_local_write_is_not_memoised(
    redis, clock, ttl
) -> None:
    redis.gate = asyncio.Event()
    racing = asyncio.create_task(store._get_flag_from_redis("BB_DISPATCH_ENABLED"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The write lands while the old GET is still open; that GET's answer
    # (pre-write in a real race) must not become the memo.
    await store.set_all_flags({**BLOB, "BB_DISPATCH_ENABLED": False})
    redis.kv[store.FEATURE_FLAGS_KEY] = json.dumps(BLOB)  # what the GET "saw"
    redis.gate.set()
    await racing
    redis.kv[store.FEATURE_FLAGS_KEY] = json.dumps(
        {**BLOB, "BB_DISPATCH_ENABLED": False}
    )
    assert await store._get_flag_from_redis("BB_DISPATCH_ENABLED") is False


async def test_ttl_zero_reads_redis_every_time(redis, clock, ttl) -> None:
    """TTL 0 is the rollback: the old path, one GET per read, concurrent
    readers included (no memo, no shared in-flight GET)."""
    ttl(0)
    for _ in range(5):
        assert await store.get_config("OUTBOUND_RATE_LIMIT_MAX_CALLS", 1, int) == 7
        await store._get_all_flags_from_redis()
    assert redis.gets == 10

    redis.gate = asyncio.Event()
    calls = [
        asyncio.create_task(store.get_config("OUTBOUND_RATE_LIMIT_MAX_CALLS", 1, int))
        for _ in range(5)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    redis.gate.set()
    assert await asyncio.gather(*calls) == [7] * 5
    assert redis.gets == 15


async def test_callers_cannot_corrupt_the_memo(redis, clock, ttl) -> None:
    got_list = await store.get_config("SOME_LIST", [], list)
    got_list.append("injected")
    got_dict = await store._get_flag_from_redis("SOME_DICT")
    assert isinstance(got_dict, dict)
    got_dict["voice_id"] = "injected"
    got_dict["tags"].append("injected")
    every = await store.get_all_flags()
    del every["BB_DISPATCH_ENABLED"]
    every["SOME_LIST"].append("injected")

    assert redis.gets == 1  # all of the above came from the memo
    assert await store.get_config("SOME_LIST", [], list) == ["a", "b"]
    assert await store._get_flag_from_redis("SOME_DICT") == BLOB["SOME_DICT"]
    assert await store.get_all_flags() == BLOB
    assert redis.gets == 1


async def test_writers_read_fresh(redis, clock, ttl) -> None:
    """Read-modify-write must not be built on a memoised copy."""
    await store.get_all_flags()
    other_pod = {**BLOB, "WRITTEN_ELSEWHERE": 1}
    redis.kv[store.FEATURE_FLAGS_KEY] = json.dumps(other_pod)

    assert await store.get_all_flags(fresh=True) == other_pod
    assert redis.gets == 2


class _FakeResponse:
    status = 200

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    async def json(self) -> Dict[str, Any]:
        return self._payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeSession:
    payload: Dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get(self, url: str) -> _FakeResponse:
        return _FakeResponse(self.payload)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def test_devcycle_sync_write_is_seen_at_once(
    redis, clock, ttl, monkeypatch
) -> None:
    """The DevCycle webhook / startup sync is the other writer in this
    process: after it stores a new blob, this process reads that blob."""
    assert await store.get_config("BB_DISPATCH_ENABLED", True, bool) is True

    _FakeSession.payload = {
        "variables": [{"_id": "v1", "key": "bb-dispatch-enabled", "type": "Boolean"}],
        "features": [
            {
                "configuration": {
                    "targets": [
                        {"distribution": [{"_variation": "x", "percentage": 1}]}
                    ]
                },
                "variations": [
                    {"_id": "x", "variables": [{"_var": "v1", "value": False}]}
                ],
            }
        ],
    }
    monkeypatch.setattr(store, "DEVCYCLE_SERVER_KEY", "test-key")
    monkeypatch.setattr(store.aiohttp, "ClientSession", _FakeSession)

    assert await store.fetch_and_update_feature_flags() is True
    assert redis.sets == [store.FEATURE_FLAGS_KEY]
    assert await store.get_config("BB_DISPATCH_ENABLED", True, bool) is False


async def test_admin_update_merges_onto_redis_not_the_memo(redis, clock, ttl) -> None:
    """POST /feature-flags is read-modify-write: built on a memoised copy it
    would silently drop a flag another pod wrote in the last TTL seconds."""
    from app.api.routers.feature_flags import handlers
    from app.schemas import UserInfo
    from app.schemas.feature_flags import FeatureFlagUpdate

    await store.get_all_flags()  # warm the memo with BLOB
    redis.kv[store.FEATURE_FLAGS_KEY] = json.dumps({**BLOB, "WRITTEN_ELSEWHERE": 1})

    await handlers.update_feature_flags_handler(
        FeatureFlagUpdate(flags={"BB_DISPATCH_ENABLED": False}),
        UserInfo.model_construct(username="ops"),
    )

    stored = json.loads(redis.kv[store.FEATURE_FLAGS_KEY])
    assert stored.get("WRITTEN_ELSEWHERE") == 1
    assert await store.get_config("BB_DISPATCH_ENABLED", True, bool) is False


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 5.0), ("2.5", 2.5), ("0", 0.0), ("0s", 0.0), ("off", 0.0), ("-1", 0.0)],
)
def test_ttl_setting_fails_toward_off(monkeypatch, raw, expected) -> None:
    """Unset -> 5s; a mistyped value must roll back (0 = memo off), never
    silently keep the memo on."""
    from app.core.config import static

    if raw is None:
        monkeypatch.delenv("LIVE_CONFIG_MEMO_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("LIVE_CONFIG_MEMO_TTL_SECONDS", raw)
    assert static._memo_ttl_seconds("LIVE_CONFIG_MEMO_TTL_SECONDS", 5.0) == expected
