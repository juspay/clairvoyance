"""Break Package A, items 9–10 and R-ERR: ``schedule_lead`` result codes and the legacy
backlog reconciler's v2 skip, on REAL Redis (routes + ENQUEUE Lua) with today's schedule
ZSET on the fake Redis."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

import pytest

import app.ai.voice.agents.breeze_buddy.dispatch  # noqa: F401  (import order)
from app.ai.voice.agents.breeze_buddy.dispatch import (
    queue as queue_mod,
    reconcilers as recon_mod,
)
from app.ai.voice.agents.breeze_buddy.dispatch.keys import SCHEDULE_ZSET
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    redis_client,
    routes as routes_mod,
    scripts,
)
from tests.breeze_buddy.dispatch.conftest import make_config, make_number
from tests.breeze_buddy.dispatch.v2.conftest import _Svc, seed_number

pytestmark = pytest.mark.asyncio

T, N1 = "tmpl-1", "num-1"


def now_ms() -> int:
    return int(time.time() * 1000)


def _async(value=None, exc: Optional[BaseException] = None):
    async def f(*a, **k):
        if exc is not None:
            raise exc
        return value

    return f


@pytest.fixture
async def env(rr, fake_redis, monkeypatch):
    svc = _Svc(rr)

    async def _get():
        return svc

    monkeypatch.setattr(routes_mod, "get_redis_service", _get)
    monkeypatch.setattr(queue_mod, "v2_seen", _async(True))
    monkeypatch.setattr(recon_mod, "v2_seen", _async(True))
    db: Dict[str, Any] = {"template": 0, "config": 0}
    state = NS(template_ok=True, template_raises=0, number=make_number(N1))

    async def tpl(tid):
        db["template"] += 1
        if state.template_raises:
            state.template_raises -= 1
            raise RuntimeError("db blip")
        if not state.template_ok:
            return None  # deleted (or the accessor swallowed a DB error)
        return NS(id=tid, merchant_id="merchant-1", reseller_id="res-1")

    async def cfg(tid):
        db["config"] += 1
        return make_config()

    monkeypatch.setattr(routes_mod, "get_template_by_id", tpl)
    monkeypatch.setattr(routes_mod, "get_call_execution_config_by_template_id", cfg)
    monkeypatch.setattr(routes_mod, "_available_number", _async(state.number))
    monkeypatch.setattr(routes_mod, "_tiers", _async((set(), set())))
    return NS(rr=rr, fake=fake_redis, db=db, state=state)


def zset(env) -> Dict[str, float]:
    return dict(env.fake.client.zsets.get(SCHEDULE_ZSET, {}))


WHEN = datetime(2026, 10, 4, 6, 0, 0, 250000, tzinfo=timezone.utc)


# -- result codes -------------------------------------------------------------------------


async def test_holder_minus2_returns_true_and_writes_nothing(env):
    await seed_number(env.rr, N1, 1, {T: {}})
    assert await scripts.enqueue(T, "L1", now_ms() - 10) == 1  # L1 holds the line
    before = await env.rr.hgetall(f"bb:inflight:{N1}")
    assert await queue_mod.schedule_lead("L1", WHEN, template_id=T) is True
    assert await env.rr.zscore(f"bb:q:{T}", "L1") is None
    assert zset(env) == {}
    assert await env.rr.hgetall(f"bb:inflight:{N1}") == before


async def test_legacy_number_minus3_is_todays_zadd_with_todays_jitter(env):
    await seed_number(env.rr, N1, 1, {T: {}}, mode=None)  # today's path owns N1
    random.seed(5)
    expected = queue_mod._apply_jitter(queue_mod._to_unix_ms(WHEN), None)
    random.seed(5)
    assert await queue_mod.schedule_lead("L1", WHEN, template_id=T) is True
    assert zset(env) == {"L1": expected}
    assert await env.rr.exists(f"bb:q:{T}") == 0


async def test_route_with_no_number_is_todays_zadd(env):
    await env.rr.hset(f"bb:route:{T}", mapping={"number": "", "enabled": "1"})
    assert await queue_mod.schedule_lead("L1", WHEN, jitter_ms=0, template_id=T)
    assert zset(env) == {"L1": queue_mod._to_unix_ms(WHEN)}


@pytest.mark.parametrize(
    "mode,tickets", [("v2_pending", 0), ("v2", 1), ("draining", 0)]
)
async def test_v2_accounted_modes_go_to_the_room_only_v2_issues(env, mode, tickets):
    await seed_number(env.rr, N1, 1, {T: {}}, mode=mode)
    due = datetime.now(timezone.utc)
    assert await queue_mod.schedule_lead("L1", due, template_id=T) is True
    assert zset(env) == {}
    in_room = await env.rr.zscore(f"bb:q:{T}", "L1")
    assert (in_room is None) == (tickets == 1)
    assert await env.rr.scard(f"bb:busy:{N1}") == tickets


async def test_v2_never_jitters_even_with_default_jitter(env, monkeypatch):
    monkeypatch.setattr(queue_mod, "BB_DISPATCH_QPS_JITTER_MS", 200)
    await seed_number(env.rr, N1, 0, {T: {}})  # no free line: everything waits
    for i in range(40):
        when = datetime.fromtimestamp(1_790_000_000 + i + 0.123, tz=timezone.utc)
        assert await queue_mod.schedule_lead(f"L{i}", when, template_id=T) is True
        assert await env.rr.zscore(f"bb:q:{T}", f"L{i}") == queue_mod._to_unix_ms(when)


async def test_missing_route_is_reresolved_once_then_enqueued(env):
    await seed_number(env.rr, N1, 0, {})
    env.state.number.maximum_channels = 0  # keep max 0 when the facts are refreshed
    env.state.template_raises = 1  # ensure_route's resolve fails; the re-resolve works
    assert await queue_mod.schedule_lead("L1", WHEN, template_id=T) is True
    assert await env.rr.hget(f"bb:route:{T}", "number") == N1
    assert await env.rr.zscore(f"bb:q:{T}", "L1") is not None
    assert zset(env) == {}
    assert env.db["template"] == 2


async def test_unresolvable_route_falls_to_todays_schedule_with_bounded_db_cost(env):
    """Implementer's choice for a second -1 (template gone): today's ZADD (keeps
    today's 'dial without a template', M4). Two resolves per call, no more."""
    await seed_number(env.rr, N1, 1, {})
    env.state.template_ok = False
    assert await queue_mod.schedule_lead("L1", WHEN, jitter_ms=0, template_id=T) is True
    assert zset(env) == {"L1": queue_mod._to_unix_ms(WHEN)}
    assert env.db["template"] == 2


async def test_redis_failure_returns_false_and_writes_nothing(env, monkeypatch):
    await seed_number(env.rr, N1, 1, {T: {}})

    class _Down:
        async def execute_command(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "_client", _Down())
    assert await queue_mod.schedule_lead("L1", WHEN, template_id=T) is False
    assert zset(env) == {}
    assert await env.rr.exists(f"bb:q:{T}") == 0


async def test_redis_failure_on_the_retry_after_minus1_returns_false(env, monkeypatch):
    await seed_number(env.rr, N1, 1, {})
    env.state.template_raises = 1
    real = scripts.enqueue
    calls: List[int] = []

    async def flaky(*a):
        calls.append(1)
        if len(calls) == 2:
            return None
        return await real(*a)

    monkeypatch.setattr(scripts, "enqueue", flaky)
    assert await queue_mod.schedule_lead("L1", WHEN, template_id=T) is False
    assert zset(env) == {} and len(calls) == 2


async def test_db_errors_while_resolving_never_escape(env, monkeypatch):
    await seed_number(env.rr, N1, 1, {})
    monkeypatch.setattr(
        routes_mod,
        "get_call_execution_config_by_template_id",
        _async(exc=RuntimeError("db down")),
    )
    # no exception escapes into the caller (push API, retry, worker defer)
    assert await queue_mod.schedule_lead("L1", WHEN, jitter_ms=0, template_id=T) in (
        True,
        False,
    )


# -- legacy backlog reconciler (T5 / P14) and R-ERR ------------------------------------------


def _rows(*rows):
    async def get(*a, **k):
        return list(rows)

    return get


async def test_reconciler_skips_only_v2_accounted_templates(env, monkeypatch):
    await seed_number(env.rr, N1, 2, {T: {}})
    await seed_number(env.rr, "num-old", 2, {"tmpl-old": {}}, mode=None)
    env.state.template_ok = False  # "tmpl-gone" cannot be resolved
    monkeypatch.setattr(
        recon_mod,
        "get_unscheduled_backlog_leads",
        _rows(
            ("A", "r", 1000, T),
            ("B", "r", 2000, "tmpl-old"),
            ("C", "r", 3000, None),
            ("D", "r", 4000, "tmpl-gone"),
            ("E", "r", 5000, T),
        ),
    )
    await recon_mod.reconcile_backlog_to_zset()
    assert zset(env) == {"B": 2000, "C": 3000, "D": 4000}


async def test_reconciler_resolves_each_template_once_per_run(env, monkeypatch):
    await seed_number(env.rr, N1, 2, {})  # no route yet: the reconciler resolves it
    monkeypatch.setattr(
        recon_mod,
        "get_unscheduled_backlog_leads",
        _rows(*[(f"L{i}", "r", 1000 + i, T) for i in range(50)]),
    )
    await recon_mod.reconcile_backlog_to_zset()
    assert zset(env) == {}
    assert env.db["template"] == 1 and env.db["config"] == 1


class _FailingClient:
    """The real client, except the chosen read raises (a Redis blip mid-run)."""

    def __init__(self, real, fail: str):
        self._real = real
        self._fail = fail

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name == "hget" and self._fail == "mode":

            async def hget(key, field):
                if field == "mode":
                    raise ConnectionError("redis blip")
                return await attr(key, field)

            return hget
        if name == "hgetall" and self._fail == "route":

            async def hgetall(key):
                raise ConnectionError("redis blip")

            return hgetall
        return attr


@pytest.mark.parametrize("fail", ["mode", "route"])
async def test_reconciler_mode_read_error_must_not_zadd_a_v2_lead(
    env, monkeypatch, fail
):
    """R-ERR (rulings, Cross-cutting): once v2 is seen, a Redis error on the mode read
    must NOT be treated as 'legacy' — a legacy ZADD is not safe for a v2-accounted
    number. Here the template's number IS in mode v2; the reconciler must skip the
    row this run (the next run retries), not copy the lead into today's schedule."""
    await seed_number(env.rr, N1, 2, {T: {}})
    real = env.rr

    async def client():
        return _FailingClient(real, fail)

    monkeypatch.setattr(routes_mod, "_client", client)
    monkeypatch.setattr(
        recon_mod, "get_unscheduled_backlog_leads", _rows(("A", "r", 1000, T))
    )
    await recon_mod.reconcile_backlog_to_zset()
    assert zset(env) == {}, "v2 lead copied into today's schedule on a Redis error"
