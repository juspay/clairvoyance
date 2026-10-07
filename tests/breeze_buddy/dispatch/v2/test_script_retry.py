"""v2 scripts after a lost reply, on the dialler's own client (dispatch/v2/redis_client.py).

The dialler's client never re-sends a command: when the socket dies (or times out) after
Redis ran an EVAL but before the reply was read, the caller sees an error and ``_run``
answers None. ``claim`` and ``mark_dialling`` run once more themselves, with the same
owner, and the scripts answer an owner's re-run with "still yours". These tests lose the
reply of an EVAL after Redis has executed it.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict

import pytest
import redis.asyncio as aioredis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import redis_client, scripts
from tests.breeze_buddy.dispatch.v2.conftest import seed_number

pytestmark = pytest.mark.asyncio

# A reply lost on the wire, and one that outlived the socket timeout (an event-loop stall)
LOSSES = [RedisConnectionError, RedisTimeoutError]


def _now() -> int:
    return int(time.time() * 1000)


@pytest.fixture
async def lossy(monkeypatch):
    """The dialler's client (no library retries) whose next ``drop["evals"]`` EVAL replies
    are lost after Redis executed the script, raising ``drop["error"]``."""
    url = os.environ.get("BB_TEST_REDIS_URL")
    if not url:
        pytest.skip("set BB_TEST_REDIS_URL=redis://localhost:56380/15")
    u = aioredis.from_url(url, decode_responses=True)
    kw = u.connection_pool.connection_kwargs
    client = aioredis.Redis(
        host=kw["host"],
        port=kw["port"],
        db=kw.get("db", 0),
        decode_responses=True,
        retry=redis_client._no_retry(),
    )
    await u.aclose()
    await client.flushdb()
    drop: Dict[str, Any] = {"evals": 0, "dropped": 0, "error": RedisConnectionError}
    real_parse = type(client).parse_response

    async def parse(conn, command_name, **options):
        if command_name in ("EVAL", "EVALSHA") and drop["evals"] > 0:
            drop["evals"] -= 1
            drop["dropped"] += 1
            raise drop["error"]("reply lost after Redis ran the script")
        return await real_parse(client, conn, command_name, **options)

    monkeypatch.setattr(client, "parse_response", parse)
    monkeypatch.setattr(redis_client, "_client", client)
    yield client, drop
    await client.flushdb()
    await client.aclose()


async def _issued(client, lead: str = "L1") -> scripts.Ticket:
    await seed_number(client, "N1", 2, {"T1": {}})
    assert await scripts.enqueue("T1", lead, _now() - 1) == 1
    t = scripts.parse_ticket(await client.lpop("bb:tickets"))
    assert t is not None
    return t


@pytest.mark.parametrize("loss", LOSSES)
async def test_the_dialler_client_never_re_sends_an_eval(lossy, loss):
    client, drop = lossy
    drop["evals"], drop["error"] = 1, loss
    with pytest.raises(loss):
        await client.eval("return redis.call('INCR', 'retry:counter')", 0)
    assert await client.get("retry:counter") == "1"  # ran once, not re-sent


@pytest.mark.parametrize("loss", LOSSES)
async def test_a_lost_claim_reply_is_retried_once_with_the_same_owner(lossy, loss):
    client, drop = lossy
    t = await _issued(client)
    drop["evals"], drop["error"] = 1, loss
    assert await scripts.claim("N1", "L1", t.tk, "owner-a") is True
    assert drop["dropped"] == 1
    assert json.loads(await client.hget("bb:inflight:N1", "L1"))["owner"] == "owner-a"


async def test_two_lost_claim_replies_count_as_not_ours(lossy):
    client, drop = lossy
    t = await _issued(client)
    drop["evals"] = 2
    assert await scripts.claim("N1", "L1", t.tk, "owner-a") is False
    # the script did run: the reaper's claimed tier frees this line
    assert json.loads(await client.hget("bb:inflight:N1", "L1"))["owner"] == "owner-a"


@pytest.mark.parametrize("loss", LOSSES)
async def test_a_lost_mark_reply_is_retried_once_and_still_ours(lossy, loss):
    client, drop = lossy
    t = await _issued(client)
    assert await scripts.claim("N1", "L1", t.tk, "owner-a")
    drop["evals"], drop["error"] = 1, loss
    assert await scripts.mark_dialling("N1", "L1", t.tk, "owner-a") is scripts.Mark.DIAL
    assert drop["dropped"] == 1
    assert "dialling_ms" in json.loads(await client.hget("bb:inflight:N1", "L1"))


async def test_two_lost_mark_replies_never_dial(lossy):
    client, drop = lossy
    t = await _issued(client)
    assert await scripts.claim("N1", "L1", t.tk, "owner-a")
    drop["evals"] = 2
    assert (
        await scripts.mark_dialling("N1", "L1", t.tk, "owner-a") is scripts.Mark.REFUSED
    )
    # marked, so the line stays with a call that never happens: the dialling tier frees it
    assert "dialling_ms" in json.loads(await client.hget("bb:inflight:N1", "L1"))
