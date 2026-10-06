"""A template is always listed on the number its route names (bb:numtpl:{N}).

match reads a number's rooms from bb:numtpl:{N}. A route that names N while N does not
list the template gives that template no lines, and nothing says so.
"""

import asyncio
import time

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import routes, scripts
from tests.breeze_buddy.dispatch.v2.conftest import seed_number, use_redis


def NOW() -> int:
    return int(time.time() * 1000)


def _route(number: str) -> routes.Route:
    return routes.Route("T1", number, "normal", None, None, True, "R1")


async def _stranded(r) -> None:
    """The route says N2, a lead waits, and N2 does not list T1 (a route write cut short)."""
    await seed_number(r, "N2", 1, {"T1": {}})
    await r.zadd("bb:q:T1", {"L1": NOW() - 1})
    await r.srem("bb:numtpl:N2", "T1")
    assert await scripts.match("N2") == 0  # a free line and a due lead, and no ticket


async def test_a_route_write_lists_the_template_in_the_same_step_as_the_route(
    rr, monkeypatch
):
    use_redis(monkeypatch, rr)
    await seed_number(rr, "N1", 1, {"T1": {}})
    await seed_number(rr, "N2", 1, {})

    async def cut(*a, **k):  # the task is cancelled right after the route is written
        raise asyncio.CancelledError()

    for step in ("srem", "sadd", "zcard"):
        monkeypatch.setattr(rr, step, cut)
    with pytest.raises(asyncio.CancelledError):
        await routes._write(_route("N2"), None)
    monkeypatch.undo()
    assert await rr.hget("bb:route:T1", "number") == "N2"
    assert await rr.smembers("bb:numtpl:N2") == {"T1"}
    assert await rr.smembers("bb:numtpl:N1") == set()


async def test_the_next_route_write_lists_a_template_its_number_lost(rr, monkeypatch):
    use_redis(monkeypatch, rr)
    await _stranded(rr)
    # the route is unchanged (N2 before, N2 now): the write lists the template all the same
    await routes._write(_route("N2"), None)
    assert await rr.smembers("bb:numtpl:N2") == {"T1"}
    assert await scripts.match("N2") == 1


async def test_the_backlog_job_lists_a_room_its_number_lost(rr):
    await _stranded(rr)
    # the backlog job re-reads a lead that is already in its room: nothing moves, but the
    # room is listed again
    assert await scripts.enqueue("T1", "L1", NOW() - 1, only_if_absent=True) == 0
    assert await rr.smembers("bb:numtpl:N2") == {"T1"}
    assert await scripts.match("N2") == 1


async def test_a_template_whose_route_key_is_gone_stays_listed(rr, monkeypatch):
    """Swaroop's #1313 question: a route key evicted or expired (it has a TTL and prod
    Redis is volatile-lru) must not drop the template from its number, or its waiting
    leads wait for a brand-new lead. The backlog job re-resolves the route, and the
    room is matched again."""
    use_redis(monkeypatch, rr)
    await seed_number(rr, "N2", 1, {"T1": {}})
    await rr.zadd("bb:q:T1", {"L1": NOW() - 1, "L2": NOW() - 1})
    await rr.delete("bb:route:T1")
    assert await scripts.match("N2") == 0  # no route: nothing to give
    assert await rr.smembers("bb:numtpl:N2") == {"T1"}  # still listed
    await rr.hset(
        "bb:route:T1",
        mapping={
            "number": "N2",
            "tier": "normal",
            "enabled": "1",
            "reseller": "R1",
            "start": "0",
            "end": "86399",
        },
    )  # the backlog job's re-resolve
    assert await scripts.match("N2") == 1


async def test_a_template_routed_to_another_number_still_leaves(rr, monkeypatch):
    use_redis(monkeypatch, rr)
    await seed_number(rr, "N2", 1, {"T1": {}})
    await seed_number(rr, "N3", 1, {})
    await rr.zadd("bb:q:T1", {"L1": NOW() - 1})
    await rr.hset("bb:route:T1", "number", "N3")
    await scripts.match("N2")
    assert await rr.smembers("bb:numtpl:N2") == set()
