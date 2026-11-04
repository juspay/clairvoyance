"""
The two public demo endpoints start paid sessions (a Daily voice call for
POST /demo/connect, an LLM turn for the chat demo), so their per-IP limits
must hold:

- each limit allows exactly its count, then answers 429 with Retry-After;
- the IP is read the way widget_common.client_ip reads it, so neither a
  client-written X-Forwarded-For entry nor X-Real-IP buys a fresh bucket;
- with Redis down or not configured, no demo starts (fail closed).

Redis is an in-memory fake; nothing here reaches Redis, Postgres or Daily.
"""

from typing import Dict, List, Optional

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routers.breeze_buddy.chat import demo as chat_demo
from app.api.routers.breeze_buddy.demo import handlers as demo
from app.services.redis import rate_limit

LB = "34.120.0.1"  # the load balancer's own address, last in X-Forwarded-For


class FakeRedis:
    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def delete(self, key: str) -> None:
        self.counts.pop(key, None)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()

    async def get_redis_service() -> FakeRedis:
        return fake

    monkeypatch.setattr(rate_limit, "is_redis_configured", lambda: True)
    monkeypatch.setattr(rate_limit, "get_redis_service", get_redis_service)
    return fake


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_redis_service() -> FakeRedis:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(rate_limit, "is_redis_configured", lambda: True)
    monkeypatch.setattr(rate_limit, "get_redis_service", get_redis_service)


def make_request(
    headers: Optional[Dict[str, str]] = None, peer: str = "10.0.0.9"
) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/demo/connect",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": (peer, 40000),
    }
    return Request(scope)


async def connect(request: Request) -> int:
    """Status the voice demo's rate limit answers: 200 (passes) or its error."""
    try:
        await demo._rate_limit(request)
    except HTTPException as exc:
        return exc.status_code
    return 200


def limits(monkeypatch: pytest.MonkeyPatch, hourly: int, daily: int) -> None:
    monkeypatch.setattr(demo, "DEMO_RATE_LIMIT_HOURLY", hourly)
    monkeypatch.setattr(demo, "DEMO_RATE_LIMIT_DAILY", daily)


# ── POST /demo/connect ───────────────────────────────────────────────────


async def test_the_hourly_limit_allows_exactly_its_count(redis, monkeypatch):
    limits(monkeypatch, hourly=3, daily=100)

    statuses = [await connect(make_request()) for _ in range(4)]

    assert statuses == [200, 200, 200, 429]


async def test_the_daily_limit_holds_under_the_hourly_one(redis, monkeypatch):
    limits(monkeypatch, hourly=100, daily=2)

    statuses = [await connect(make_request()) for _ in range(3)]

    assert statuses == [200, 200, 429]


async def test_a_refusal_says_when_to_retry(redis, monkeypatch):
    limits(monkeypatch, hourly=1, daily=100)
    await connect(make_request())

    with pytest.raises(HTTPException) as refused:
        await demo._rate_limit(make_request())

    assert refused.value.status_code == 429
    assert int((refused.value.headers or {}).get("Retry-After", "0")) >= 1


async def test_another_ip_has_its_own_bucket(redis, monkeypatch):
    limits(monkeypatch, hourly=1, daily=100)

    assert await connect(make_request(peer="10.0.0.1")) == 200
    assert await connect(make_request(peer="10.0.0.2")) == 200
    assert await connect(make_request(peer="10.0.0.1")) == 429


async def test_x_real_ip_does_not_buy_a_new_bucket(redis, monkeypatch):
    """Anyone can send X-Real-IP; it used to be the fallback key."""
    limits(monkeypatch, hourly=2, daily=100)

    statuses = [
        await connect(make_request({"X-Real-IP": f"198.51.100.{n}"})) for n in range(3)
    ]

    assert statuses == [200, 200, 429]


async def test_a_client_written_forwarded_for_entry_does_not_buy_a_new_bucket(
    redis, monkeypatch
):
    """The client controls everything left of what the load balancer appends."""
    limits(monkeypatch, hourly=2, daily=100)

    statuses = [
        await connect(
            make_request({"X-Forwarded-For": f"6.6.6.{n}, 203.0.113.7, {LB}"})
        )
        for n in range(3)
    ]

    assert statuses == [200, 200, 429]


async def test_with_redis_down_no_demo_call_starts(redis_down):
    assert await connect(make_request()) == 429


async def test_with_redis_not_configured_no_demo_call_starts(monkeypatch):
    monkeypatch.setattr(rate_limit, "is_redis_configured", lambda: False)

    assert await connect(make_request()) == 429


async def test_a_refused_connect_reads_no_template_and_starts_no_call(
    redis_down, monkeypatch
):
    touched: List[str] = []

    async def get_template_in_scope(**_kw: object) -> None:
        touched.append("template")

    async def start_daily_session(*_a: object, **_kw: object) -> None:
        touched.append("daily")

    monkeypatch.setattr(demo, "get_template_in_scope", get_template_in_scope)
    monkeypatch.setattr(demo, "start_daily_session", start_daily_session)
    body = demo.DemoConnectRequest.model_construct(agent="order-confirmation")

    with pytest.raises(HTTPException) as refused:
        await demo.breeze_buddy_demo_connect_handler(make_request(), body)

    assert refused.value.status_code == 429
    assert touched == []


# ── chat demo ────────────────────────────────────────────────────────────


async def test_the_chat_demo_allows_its_limit_then_refuses(redis):
    request = make_request()
    await chat_demo._enforce_ip_limit(request=request, bucket="message", limit=1)

    with pytest.raises(HTTPException) as refused:
        await chat_demo._enforce_ip_limit(request=request, bucket="message", limit=1)

    assert refused.value.status_code == 429


async def test_with_redis_down_the_chat_demo_refuses(redis_down):
    with pytest.raises(HTTPException) as refused:
        await chat_demo._enforce_ip_limit(
            request=make_request(), bucket="session_create", limit=5
        )

    assert refused.value.status_code == 429
