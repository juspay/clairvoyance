"""Audit: today's journey on HEAD vs BASE (15b27e97). Situation A = v2 never used;
situation B = v2 seen (on for N1), journeys on a legacy number N2.

Tests named *_differs are EXPECTED TO FAIL on HEAD: each asserts BASE behaviour.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace as NS
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch import (
    queue as queue_mod,
    worker as head_mod,
)
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    latch as latch_mod,
    release as R,
    routes as routes_mod,
)
from app.ai.voice.agents.breeze_buddy.managers import (
    calls as calls_mod,
    inbound_channel as IC,
)
from app.schemas import CallDirection, CallProvider, ExecutionMode
from tests.breeze_buddy.dispatch.v2.test_break_pA_legacy_parity import (  # noqa: F401
    FIXED_NOW,
    SCENARIOS,
    _booby_trap_v2,
    _run,
    base,
    s_success,
)

pytestmark = pytest.mark.asyncio

FIELDS = (
    "calls",
    "released_locks",
    "released_numbers",
    "deferred",
    "completions",
    "locked",
    "lead",
    "redis",
)


def _diff(old, new) -> List[str]:
    return [f for f in FIELDS if getattr(old, f) != getattr(new, f)]


# ---------------------------------------------------------------- situation A


def s_stale_early_copy(h, f):
    # A stale queue copy: the row's next_attempt_at is 60 s in the future.
    h.leads["L1"].next_attempt_at = FIXED_NOW + timedelta(seconds=60)


async def test_A_stale_early_copy_dials_like_base_differs(base, monkeypatch):
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=False))
    touched: list = []
    _booby_trap_v2(monkeypatch, touched)
    old = await _run(base.worker, base.queue, s_stale_early_copy, monkeypatch)
    new = await _run(head_mod, queue_mod, s_stale_early_copy, monkeypatch)
    assert touched == []
    assert _diff(old, new) == [], (
        f"BASE calls={old.calls} deferred={old.deferred}; "
        f"HEAD calls={new.calls} deferred={new.deferred}"
    )


# ---------------------------------------------------------------- situation B


class _Scripts:
    def __init__(self, enqueue_ret):
        self.enqueue_calls: list = []
        self._ret = enqueue_ret

    async def enqueue(self, *a):
        self.enqueue_calls.append(a)
        return self._ret

    def __getattr__(self, name):  # any other v2 script on N2's path is a bug
        raise AssertionError(f"v2 script {name} touched for N2")


def _b_setup(monkeypatch, mode="legacy", epoch=True, enqueue_ret=-3):
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(queue_mod, "_number_mode_or_none", AsyncMock(return_value=mode))
    monkeypatch.setattr(
        queue_mod, "_epoch_present_or_none", AsyncMock(return_value=epoch)
    )
    s = _Scripts(enqueue_ret)
    monkeypatch.setattr(queue_mod, "v2_scripts", s)
    return s


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.__name__ for s in SCENARIOS])
async def test_B_worker_on_legacy_n2_matches_base(scenario, base, monkeypatch):
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=False))
    old = await _run(base.worker, base.queue, scenario, monkeypatch)
    _b_setup(monkeypatch)
    new = await _run(head_mod, queue_mod, scenario, monkeypatch)
    assert _diff(old, new) == []
    assert new.raised == old.raised


@pytest.mark.xfail(
    strict=True,
    reason="intended: an unreadable mode defers 30 s instead of dialling (it may be a v2 number; today's token step would fail in the same Redis outage)",
)
async def test_B_worker_n2_mode_read_blip_differs(base, monkeypatch):
    """Redis HGET of bb:num:N2.mode fails once: BASE dials, HEAD defers 30 s."""
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=False))
    old = await _run(base.worker, base.queue, s_success, monkeypatch)
    _b_setup(monkeypatch, mode=None)
    new = await _run(head_mod, queue_mod, s_success, monkeypatch)
    assert (
        _diff(old, new) == []
    ), f"BASE calls={old.calls}; HEAD calls={new.calls} deferred={new.deferred}"


@pytest.mark.xfail(
    strict=True,
    reason="intended: after a Redis loss today's dialling holds until counters are recounted (design card rule 32, x7b)",
)
async def test_B_worker_n2_epoch_lost_differs(base, monkeypatch):
    """bb:epoch missing after this process saw it: every N2 dispatch defers."""
    monkeypatch.setattr(queue_mod, "v2_seen", AsyncMock(return_value=False))
    old = await _run(base.worker, base.queue, s_success, monkeypatch)
    _b_setup(monkeypatch, epoch=False)
    monkeypatch.setattr(latch_mod, "_epoch_seen", True)
    new = await _run(head_mod, queue_mod, s_success, monkeypatch)
    assert (
        _diff(old, new) == []
    ), f"BASE calls={old.calls}; HEAD calls={new.calls} deferred={new.deferred}"


def _lead(direction=CallDirection.OUTBOUND) -> Any:
    return NS(
        id="L2",
        telephony_number_id="N2",
        call_direction=direction,
        call_id="C2",
        execution_mode=ExecutionMode.TELEPHONY,
    )


def _r_blip(monkeypatch):
    """Situation B, legacy N2, a Redis read blip: mode unreadable, release script fails."""
    monkeypatch.setattr(R, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value=None))
    monkeypatch.setattr(R.scripts, "release", AsyncMock(return_value=None))


async def test_B_n2_legacy_call_end_releases_db_channel_on_redis_blip_differs(
    monkeypatch,
):
    _r_blip(monkeypatch)
    monkeypatch.setattr(
        calls_mod,
        "get_telephony_number_by_id",
        AsyncMock(return_value=NS(id="N2", provider=CallProvider.PLIVO)),
    )
    monkeypatch.setattr(calls_mod, "_releases_capacity", lambda *a: True)
    rel_num, rel_tok = AsyncMock(), AsyncMock()
    monkeypatch.setattr(calls_mod, "_release_number", rel_num)
    monkeypatch.setattr(calls_mod, "release_channel_token", rel_tok)
    await calls_mod._release_call_resources(_lead())
    # BASE: the DB channel (and token) are given back; Redis has no say on the DB step.
    rel_num.assert_awaited_once()


async def test_B_n2_legacy_call_end_mode_ok_runs_todays_release(monkeypatch):
    monkeypatch.setattr(R, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="legacy"))
    monkeypatch.setattr(
        calls_mod,
        "get_telephony_number_by_id",
        AsyncMock(return_value=NS(id="N2", provider=CallProvider.PLIVO)),
    )
    monkeypatch.setattr(calls_mod, "_releases_capacity", lambda *a: True)
    rel_num, rel_tok = AsyncMock(), AsyncMock()
    monkeypatch.setattr(calls_mod, "_release_number", rel_num)
    monkeypatch.setattr(calls_mod, "release_channel_token", rel_tok)
    await calls_mod._release_call_resources(_lead())
    rel_num.assert_awaited_once()
    rel_tok.assert_awaited_once()


@pytest.mark.xfail(
    strict=True,
    reason="intended: an inbound call is refused while the number's owner can't be read (admitting blind could over-dial a v2 number)",
)
async def test_B_n2_legacy_inbound_admit_on_redis_blip_differs(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import latch, routes

    monkeypatch.setattr(latch, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "number_mode_or_none", AsyncMock(return_value=None))
    db = AsyncMock(return_value=object())  # the DB gate has a free channel
    monkeypatch.setattr(IC, "increment_telephony_number_channels", db)
    # BASE: admitted by the DB gate (no Redis involved).
    assert await IC.admit_inbound_call("N2", call_id="C9") is True


async def test_B_n2_legacy_inbound_admit_mode_ok_uses_db_gate(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import latch, routes

    monkeypatch.setattr(latch, "v2_seen", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "number_mode_or_none", AsyncMock(return_value="legacy"))
    db = AsyncMock(return_value=object())
    monkeypatch.setattr(IC, "increment_telephony_number_channels", db)
    assert await IC.admit_inbound_call("N2", call_id="C9") is True
    db.assert_awaited_once_with("N2")


async def test_B_n2_legacy_inbound_release_on_redis_blip_differs(monkeypatch):
    _r_blip(monkeypatch)
    monkeypatch.setattr(
        IC,
        "get_telephony_number_by_id",
        AsyncMock(return_value=NS(id="N2", provider=CallProvider.PLIVO)),
    )
    monkeypatch.setattr(IC, "inbound_holds_channel", lambda *a: True)
    dec = AsyncMock()
    monkeypatch.setattr(IC, "decrement_telephony_number_channels", dec)
    await IC.release_inbound_channel(_lead(CallDirection.INBOUND))
    dec.assert_awaited_once()  # BASE: DB channel given back


@pytest.mark.xfail(
    strict=True,
    reason="intended: no blind ZADD when the v2 script fails; the backlog reconciler re-queues within 60 s",
)
async def test_B_schedule_on_n2_with_unreadable_lua_returns_false_without_zadd(
    monkeypatch,
):
    """enqueue -> None (script error) on N2: BASE would ZADD; HEAD does not."""
    s = _b_setup(monkeypatch, enqueue_ret=None)
    zadds: list = []

    class _C:
        async def zadd(self, *a, **k):
            zadds.append(a)
            return 1

    class _Svc:
        async def get_client(self):
            return _C()

    monkeypatch.setattr(queue_mod, "get_redis_service", AsyncMock(return_value=_Svc()))
    ok = await queue_mod.schedule_lead("L2", FIXED_NOW, template_id="T2")
    assert s.enqueue_calls
    assert ok is True and zadds, "BASE ZADDs; HEAD returned False without a ZADD"


async def test_routes_ensure_route_has_no_today_side_effect(monkeypatch):
    """Sanity: routes.number_mode_or_none defaults to legacy on a missing hash."""

    class _C:
        async def hget(self, *a):
            return None

    monkeypatch.setattr(routes_mod, "_client", AsyncMock(return_value=_C()))
    assert await routes_mod.number_mode_or_none("N2") == "legacy"
