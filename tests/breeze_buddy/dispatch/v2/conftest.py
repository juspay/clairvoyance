"""Real-Redis fixture for the v2 Lua scripts (skips unless BB_TEST_REDIS_URL is set)."""

import json
import os
from typing import Any, Optional, Tuple

import pytest
import redis.asyncio as aioredis

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import redis_client, routes, scripts

OWNER = "own-1"  # the dial coroutine that claimed a test's ticket


class _Svc:
    def __init__(self, client):
        self._c = client

    async def run_script(self, script, keys, args):
        return await self._c.eval(script, len(keys), *keys, *args)

    async def get_client(self):
        return self._c


@pytest.fixture
async def rr(monkeypatch):
    url = os.environ.get("BB_TEST_REDIS_URL")
    if not url:
        pytest.skip(
            "set BB_TEST_REDIS_URL=redis://localhost:56380/15 to run v2 Lua tests"
        )
    client = aioredis.from_url(url, decode_responses=True)
    await client.flushdb()
    monkeypatch.setattr(redis_client, "_client", client)
    yield client
    await client.flushdb()
    await client.aclose()


async def seed_number(c, n, max_lines, templates, *, mode="v2"):
    """Number ``n`` in ``mode`` with ``max_lines``, and a route to it per template."""
    await c.hset(
        f"bb:num:{n}",
        mapping={"max": max_lines, "provider": "plivo", "status": "AVAILABLE"},
    )
    if mode is not None:
        await c.hset(f"bb:num:{n}", "mode", mode)
    for t, extra in templates.items():
        route = {
            "number": n,
            "tier": "normal",
            "enabled": "1",
            "reseller": "R1",
            "start": "0",
            "end": "86399",
        }
        route.update(extra)
        await c.hset(f"bb:route:{t}", mapping=route)
        await c.sadd(f"bb:numtpl:{n}", t)


def use_redis(monkeypatch, client, *modules) -> None:
    """Point ``get_redis_service`` at ``client``, like ``rr`` does for scripts: in
    ``routes`` (whose ``_client`` every v2 job and hook shares) and in each of ``modules``
    that has its own getter (e.g. today's channel semaphore)."""

    async def _get():
        return _Svc(client)

    for m in (routes, *modules):
        if hasattr(m, "get_redis_service"):
            monkeypatch.setattr(m, "get_redis_service", _get)


async def pop_ticket(n: str) -> Optional[Tuple[str, int]]:
    """(lead id, ticket id) of number ``n``'s oldest live ticket, taken off bb:tickets as
    an acceptor pops it (void entries are dropped); None when ``n`` has none."""
    client: Any = await redis_client.v2_redis()
    for raw in await client.lrange("bb:tickets", 0, -1):
        t = scripts.parse_ticket(raw)
        if t is None or t.number_id != n:
            continue
        await client.lrem("bb:tickets", 1, raw)
        lease = await client.hget(f"bb:inflight:{n}", t.lead_id)
        if lease and json.loads(lease).get("tk") == t.tk:
            return t.lead_id, t.tk
    return None


async def claim_next(n: str, owner: str = OWNER) -> Optional[Tuple[str, int]]:
    """``pop_ticket``, then claimed by ``owner`` as a dial coroutine claims it."""
    got = await pop_ticket(n)
    if got is not None:
        assert await scripts.claim(n, got[0], got[1], owner)
    return got


async def tickets_of(c, n: str) -> list:
    """Lead ids of number ``n``'s live tickets waiting in bb:tickets, oldest first.
    Void entries (reaped or re-issued tickets, which claim refuses) are skipped."""
    out = []
    for raw in await c.lrange("bb:tickets", 0, -1):
        t = scripts.parse_ticket(raw)
        if t is None or t.number_id != n:
            continue
        lease = await c.hget(f"bb:inflight:{n}", t.lead_id)
        if lease and json.loads(lease).get("tk") == t.tk:
            out.append(t.lead_id)
    return out
