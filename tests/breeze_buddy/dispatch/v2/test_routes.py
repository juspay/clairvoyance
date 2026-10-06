import datetime as dt
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as aioredis

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import routes
from app.schemas import CallProvider

REDIS_URL = os.getenv("BB_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="BB_TEST_REDIS_URL not set")


def _num(id_, provider=CallProvider.PLIVO, status="AVAILABLE", max_=10):
    return NS(id=id_, provider=provider, status=NS(value=status), maximum_channels=max_)


def _cfg(start="09:00", end="21:00", enabled=True):
    return NS(
        call_start_time=dt.time.fromisoformat(start),
        call_end_time=dt.time.fromisoformat(end),
        enable_calling=enabled,
        merchant_id="M1",
        reseller_id="R1",
    )


def _tpl(id_="T1"):
    return NS(id=id_, merchant_id="M1", reseller_id="R1")


@pytest.fixture
async def rds(monkeypatch):
    if not REDIS_URL:
        pytest.skip("BB_TEST_REDIS_URL not set")
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    service = NS(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(routes, "get_redis_service", AsyncMock(return_value=service))
    monkeypatch.setattr(routes, "_tiers", AsyncMock(return_value=(set(), set())))
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def broken_redis(monkeypatch):
    client = NS(
        hgetall=AsyncMock(side_effect=RuntimeError("down")),
        hget=AsyncMock(side_effect=RuntimeError("down")),
    )
    service = NS(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(routes, "get_redis_service", AsyncMock(return_value=service))


@pytest.mark.asyncio
async def test_pinned_number_route(monkeypatch):
    monkeypatch.setattr(routes, "_available_number", AsyncMock(return_value=_num("N1")))
    monkeypatch.setattr(routes, "_tiers", AsyncMock(return_value=({"M1"}, set())))
    r, n = await routes.resolve_route(_tpl(), _cfg())
    assert (r.number_id, r.tier, r.start_sec, r.end_sec, r.enabled, r.reseller_id) == (
        "N1",
        "high",
        32400,
        75600,
        True,
        "R1",
    )
    assert getattr(n, "id", None) == "N1"
    assert not hasattr(r, "mode")


@pytest.mark.asyncio
async def test_missing_number_route_has_no_number(monkeypatch):
    monkeypatch.setattr(routes, "_available_number", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_tiers", AsyncMock(return_value=(set(), set())))
    r, n = await routes.resolve_route(_tpl(), _cfg())
    assert (r.number_id, r.tier, n) == (None, "normal", None)


@pytest.mark.asyncio
async def test_twilio_number_is_still_a_route(monkeypatch):
    # provider handling lives in the mode owner (switch.py), not the route
    monkeypatch.setattr(
        routes,
        "_available_number",
        AsyncMock(return_value=_num("N1", provider=CallProvider.TWILIO)),
    )
    monkeypatch.setattr(routes, "_tiers", AsyncMock(return_value=(set(), set())))
    r, _ = await routes.resolve_route(_tpl(), _cfg())
    assert r.number_id == "N1"


@needs_redis
@pytest.mark.asyncio
async def test_ensure_route_writes_and_reads_back(rds, monkeypatch):
    avail = AsyncMock(return_value=_num("N1"))
    monkeypatch.setattr(routes, "_available_number", avail)
    r = await routes.ensure_route("T1", template=_tpl(), config=_cfg())
    assert r is not None and r.number_id == "N1"
    h = await rds.hgetall("bb:route:T1")
    assert h == {
        "number": "N1",
        "tier": "normal",
        "start": "32400",
        "end": "75600",
        "enabled": "1",
        "reseller": "R1",
    }
    assert await rds.smembers("bb:numtpl:N1") == {"T1"}
    assert (await rds.hgetall("bb:num:N1"))["max"] == "10"
    # second call: served from Redis, no number resolution
    r2 = await routes.ensure_route("T1")
    assert r2 == r
    assert avail.await_count == 1


@needs_redis
@pytest.mark.asyncio
async def test_route_move_updates_numtpl_and_makes_the_new_number_due(rds, monkeypatch):
    avail = AsyncMock(return_value=_num("N1"))
    monkeypatch.setattr(routes, "_available_number", avail)
    await routes.ensure_route("T1", template=_tpl(), config=_cfg())
    await rds.zadd("bb:q:T1", {"lead-1": 1})
    avail.return_value = _num("N2")
    monkeypatch.setattr(routes, "get_template_by_id", AsyncMock(return_value=_tpl()))
    monkeypatch.setattr(
        routes,
        "get_call_execution_config_by_template_id",
        AsyncMock(return_value=_cfg()),
    )
    await routes.invalidate_route("T1")
    assert (await rds.hgetall("bb:route:T1"))["number"] == "N2"
    assert await rds.smembers("bb:numtpl:N1") == set()
    assert await rds.smembers("bb:numtpl:N2") == {"T1"}
    assert (
        await rds.zscore("bb:due", "N2") is not None
    )  # the waiting lead isn't stranded
    assert await rds.zscore("bb:due", "N1") is None


@needs_redis
@pytest.mark.asyncio
async def test_route_move_with_empty_room_does_not_wake(rds, monkeypatch):
    avail = AsyncMock(return_value=_num("N1"))
    monkeypatch.setattr(routes, "_available_number", avail)
    await routes.ensure_route("T1", template=_tpl(), config=_cfg())
    avail.return_value = _num("N2")
    monkeypatch.setattr(routes, "get_template_by_id", AsyncMock(return_value=_tpl()))
    monkeypatch.setattr(
        routes,
        "get_call_execution_config_by_template_id",
        AsyncMock(return_value=_cfg()),
    )
    await routes.invalidate_route("T1")
    assert await rds.zcard("bb:due") == 0


@needs_redis
@pytest.mark.asyncio
async def test_refresh_number_null_max_is_one_and_never_touches_mode(rds):
    await routes.refresh_number(_num("N1", max_=None))
    h = await rds.hgetall("bb:num:N1")
    assert h == {"max": "1", "provider": "PLIVO", "status": "AVAILABLE"}
    assert "mode" not in h
    await rds.hset("bb:num:N1", "mode", "v2")
    await routes.refresh_number(_num("N1", max_=4, status="DISABLED"))
    h = await rds.hgetall("bb:num:N1")
    assert (h["mode"], h["max"], h["status"]) == ("v2", "4", "DISABLED")


@needs_redis
@pytest.mark.asyncio
async def test_number_mode_or_none_reads_the_mode_and_defaults_to_legacy(rds):
    assert await routes.number_mode_or_none("N1") == "legacy"
    for mode in ("legacy", "v2_pending", "v2", "draining"):
        await rds.hset("bb:num:N1", "mode", mode)
        assert await routes.number_mode_or_none("N1") == mode
    assert await routes.number_modes(["N1", "N9"]) == {"N1": "draining", "N9": None}


@pytest.mark.asyncio
async def test_redis_errors_never_raise(broken_redis, monkeypatch):
    monkeypatch.setattr(routes, "get_template_by_id", AsyncMock(return_value=_tpl()))
    monkeypatch.setattr(
        routes,
        "get_call_execution_config_by_template_id",
        AsyncMock(return_value=_cfg()),
    )
    monkeypatch.setattr(routes, "_available_number", AsyncMock(return_value=_num("N1")))
    monkeypatch.setattr(routes, "_tiers", AsyncMock(return_value=(set(), set())))
    assert await routes.number_mode_or_none("N1") is None  # unreadable, not "legacy"
    assert await routes.ensure_route("T1") is None
    await routes.refresh_number(_num("N1"))
    await routes.invalidate_route("T1")


@needs_redis
@pytest.mark.asyncio
async def test_route_hash_expires_unless_rewritten_and_number_facts_never_expire(
    rds, monkeypatch
):
    """Fable M10 (card §1 lifetimes): bb:route:{T} gets a TTL refreshed on every write, so
    a deleted template's route goes away; safe, ensure_route re-resolves a missing route.
    bb:num:{N} gets none: prod Redis evicts keys with a TTL when memory is tight
    (volatile-lru), and losing a number's mode / seq would switch it off v2 without a
    hand-back. A TTL left on it by older code is cleared by the next facts write."""
    monkeypatch.setattr(routes, "_available_number", AsyncMock(return_value=_num("N1")))
    monkeypatch.setattr(routes, "get_template_by_id", AsyncMock(return_value=_tpl()))
    monkeypatch.setattr(
        routes,
        "get_call_execution_config_by_template_id",
        AsyncMock(return_value=_cfg()),
    )
    await routes.ensure_route("T1")
    day = 24 * 3600
    assert day < await rds.ttl("bb:route:T1") <= 2 * day
    assert await rds.ttl("bb:num:N1") == -1  # no TTL
    await rds.expire("bb:route:T1", 60)  # nearly gone ...
    await rds.expire("bb:num:N1", 60)  # a TTL from older code
    await routes.invalidate_route("T1")  # ... until the next write
    assert day < await rds.ttl("bb:route:T1") <= 2 * day
    assert await rds.ttl("bb:num:N1") == -1
    await rds.expire("bb:num:N1", 60)
    await routes.refresh_number(_num("N1"))  # the 5 s facts refresh
    await rds.hset("bb:num:N1", "mode", "v2")  # the switch's writes
    assert await rds.ttl("bb:num:N1") == -1
