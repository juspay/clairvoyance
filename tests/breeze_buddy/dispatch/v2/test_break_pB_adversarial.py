"""Adversarial tests for package B (release hooks T8, inbound admit/release T10), real Redis."""

import subprocess
import sys
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import (
    latch,
    release as R,
    routes,
    scripts,
)
from app.ai.voice.agents.breeze_buddy.managers import (
    calls as calls_mod,
    inbound_channel as IC,
)
from app.schemas import CallDirection, CallProvider, LeadCallStatus

from .conftest import _Svc, seed_number, tickets_of


def _out(lid="L1", n="N1", cid="C1") -> Any:
    return NS(
        id=lid,
        telephony_number_id=n,
        call_direction=CallDirection.OUTBOUND,
        call_id=cid,
        template_id="T1",
        status=LeadCallStatus.PROCESSING,
        execution_mode="BACKLOG",
    )


def _inb(cid="C1", n="N1", lid="I1") -> Any:
    return NS(
        id=lid,
        telephony_number_id=n,
        call_direction=CallDirection.INBOUND,
        call_id=cid,
        status=LeadCallStatus.PROCESSING,
    )


@pytest.fixture
async def wired(rr, monkeypatch):
    """Latch on, routes + scripts on the real test Redis."""
    monkeypatch.setattr(latch, "_seen", True)

    async def _get():
        return _Svc(rr)

    monkeypatch.setattr(routes, "get_redis_service", _get)
    yield rr
    latch._reset_for_tests()


def _break_mode_reads(monkeypatch, rr):
    class P:
        async def hget(self, key, field, *a):
            if field == "mode":
                raise ConnectionError("blip")
            return await rr.hget(key, field, *a)

    async def _c():
        return P()

    monkeypatch.setattr(routes, "_client", _c)


def _legacy_spies(monkeypatch):
    tok, dec, inc = AsyncMock(), AsyncMock(), AsyncMock(return_value=object())
    monkeypatch.setattr(calls_mod, "release_channel_token", tok)
    monkeypatch.setattr(calls_mod, "_release_number", dec)
    monkeypatch.setattr(
        calls_mod,
        "get_telephony_number_by_id",
        AsyncMock(return_value=NS(id="N1", provider=CallProvider.PLIVO)),
    )
    monkeypatch.setattr(calls_mod, "is_dispatchable", lambda m: True)
    monkeypatch.setattr(IC, "increment_telephony_number_channels", inc)
    return tok, dec, inc


# 1a. mode read blip on release
@pytest.mark.asyncio
async def test_mode_read_error_on_release_frees_both_sides(wired, monkeypatch):
    # The owner can't be read, so the call end releases on BOTH sides: the v2 line is
    # freed (no leak in the busy list) and today's release runs (a legacy number's DB
    # channel must never leak - audit #1). On a v2 number today's side is harmless: its
    # DB channels are a mirror rewritten every 30 s, its token list is unused.
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    await wired.sadd("bb:busy:N1", "lead:L1")
    tok, dec, _ = _legacy_spies(monkeypatch)
    _break_mode_reads(monkeypatch, wired)
    await calls_mod._release_call_resources(_out())
    assert not await wired.sismember(
        "bb:busy:N1", "lead:L1"
    ), "line leaked in busy list"
    dec.assert_awaited_once()


# 1b. mode read blip on inbound admit
@pytest.mark.asyncio
async def test_mode_read_error_on_inbound_admit_must_not_bypass_busy_list(
    wired, monkeypatch
):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    _, _, inc = _legacy_spies(monkeypatch)
    _break_mode_reads(monkeypatch, wired)
    ok = await IC.admit_inbound_call("N1", call_id="C1")
    in_busy = await wired.sismember("bb:busy:N1", "call:C1")
    # Admitted through DB gate with no busy entry -> v2 can fill the line: over the limit.
    assert not (ok and not in_busy), "admitted without occupying a v2 line"
    inc.assert_not_awaited()


# 2. duplicate / out-of-order end signals
@pytest.mark.asyncio
async def test_duplicate_end_signals_free_exactly_one_line(wired, monkeypatch):
    await seed_number(wired, "N1", 2, {"T1": {}}, mode="v2")
    await wired.sadd("bb:busy:N1", "lead:L1", "lead:L2")
    tok, dec, _ = _legacy_spies(monkeypatch)
    lead = _out()
    assert await R.release_lead_line(lead) is True  # on_client_disconnected
    await calls_mod._release_call_resources(lead)  # completion
    await calls_mod._release_call_resources(lead)  # duplicate Plivo callback
    assert await wired.smembers("bb:busy:N1") == {"lead:L2"}
    tok.assert_not_awaited()
    dec.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_release_after_handoff_does_not_free_next_lead(
    wired, monkeypatch
):
    """Line handed to L2 (same number) after L1's first release; L1's duplicate must not free L2."""
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    await wired.sadd("bb:busy:N1", "lead:L1")
    _legacy_spies(monkeypatch)
    await R.release_lead_line(_out("L1"))
    await wired.sadd("bb:busy:N1", "lead:L2")
    await calls_mod._release_call_resources(_out("L1"))
    assert await wired.sismember("bb:busy:N1", "lead:L2")


# 3. stamped number, not route
@pytest.mark.asyncio
async def test_release_uses_stamped_number_when_route_moved(wired, monkeypatch):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    await seed_number(wired, "N2", 1, {}, mode="v2")
    await wired.sadd("bb:busy:N1", "lead:L1")
    await wired.sadd("bb:busy:N2", "lead:L1")  # same-id decoy must stay
    await wired.hset("bb:route:T1", "number", "N2")  # template moved mid-call
    assert await R.release_lead_line(_out("L1", n="N1")) is True
    assert not await wired.sismember("bb:busy:N1", "lead:L1")
    assert await wired.sismember("bb:busy:N2", "lead:L1")


@pytest.mark.asyncio
async def test_release_stamped_number_in_legacy_while_route_number_is_v2(
    wired, monkeypatch
):
    await seed_number(wired, "N1", 1, {}, mode="legacy")
    await seed_number(wired, "N2", 1, {"T1": {}}, mode="v2")
    assert await R.release_lead_line(_out("L1", n="N1")) is None


# 4. modes
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expect",
    [
        # final review M3: until the busy list is seeded at the end of v2_pending, today's
        # DB gate counts the number's calls, so they release through today's path
        ("v2_pending", None),
        ("v2", True),
        ("draining", True),
        ("legacy", None),
        (None, None),
    ],
)
async def test_release_by_mode(wired, mode, expect):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode=mode)
    await wired.sadd("bb:busy:N1", "lead:L1")
    assert await R.release_lead_line(_out()) is expect
    assert (not await wired.sismember("bb:busy:N1", "lead:L1")) is bool(expect)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,v2",
    [
        # ruling C-concern 2 (Package C fix round): until the busy list is seeded at the
        # end of v2_pending, today's DB gate admits inbound
        ("v2_pending", False),
        ("v2", True),
        ("draining", True),
        ("legacy", False),
        (None, False),
    ],
)
async def test_admit_by_mode(wired, monkeypatch, mode, v2):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode=mode)
    _, _, inc = _legacy_spies(monkeypatch)
    assert await IC.admit_inbound_call("N1", call_id="C1") is True
    assert bool(await wired.sismember("bb:busy:N1", "call:C1")) is v2
    assert (inc.await_count == 0) is v2


@pytest.mark.asyncio
async def test_latch_off_never_calls_v2(monkeypatch):
    latch._reset_for_tests()
    monkeypatch.setattr(latch, "_checked_at", 1e18)  # no refresh
    boom = AsyncMock(side_effect=AssertionError("v2 touched"))
    monkeypatch.setattr(R, "number_mode_or_none", boom)
    monkeypatch.setattr(routes, "number_mode_or_none", boom)
    monkeypatch.setattr(R.scripts, "release", boom)
    monkeypatch.setattr(scripts, "admit_inbound", boom)
    _, _, inc = _legacy_spies(monkeypatch)
    assert await IC.admit_inbound_call("N1", call_id="C1") is True
    inc.assert_awaited_once_with("N1")
    await calls_mod._release_call_resources(_out())
    latch._reset_for_tests()


# 5. inbound
@pytest.mark.asyncio
async def test_inbound_at_max_rejected_and_idempotent(wired, monkeypatch):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    _, _, inc = _legacy_spies(monkeypatch)
    assert await IC.admit_inbound_call("N1", call_id="C1") is True
    assert await IC.admit_inbound_call("N1", call_id="C1") is True  # same call twice
    assert await wired.scard("bb:busy:N1") == 1
    assert await IC.admit_inbound_call("N1", call_id="C2") is False
    inc.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_release_frees_and_hands_to_waiting_outbound(wired):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    assert await IC.admit_inbound_call("N1", call_id="C1") is True
    assert await scripts.enqueue("T1", "L9", 0) is not None
    assert len(await tickets_of(wired, "N1")) == 0  # line full: nothing issued
    assert await IC.release_inbound_channel(_inb("C1")) is True
    assert await wired.smembers("bb:busy:N1") == {"lead:L9"}
    assert len(await tickets_of(wired, "N1")) == 1


@pytest.mark.asyncio
async def test_inbound_release_does_not_free_other_holder(wired):
    await seed_number(wired, "N1", 2, {"T1": {}}, mode="v2")
    await wired.sadd("bb:busy:N1", "call:C1", "lead:L1")
    assert await IC.release_inbound_channel(_inb("C1")) is True
    assert await IC.release_inbound_channel(_inb("C1")) is False
    assert await wired.smembers("bb:busy:N1") == {"lead:L1"}


@pytest.mark.asyncio
async def test_inbound_call_end_via_calls_module_skips_db(wired, monkeypatch):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    await wired.sadd("bb:busy:N1", "call:C1")
    dec = AsyncMock()
    monkeypatch.setattr(IC, "decrement_telephony_number_channels", dec)
    monkeypatch.setattr(IC, "get_telephony_number_by_id", AsyncMock())
    await calls_mod._release_call_resources(_inb("C1"))
    dec.assert_not_awaited()
    assert await wired.scard("bb:busy:N1") == 0


@pytest.mark.asyncio
async def test_inbound_legacy_untouched(wired, monkeypatch):
    await seed_number(wired, "N1", 1, {}, mode="legacy")
    dec = AsyncMock()
    monkeypatch.setattr(IC, "decrement_telephony_number_channels", dec)
    monkeypatch.setattr(
        IC,
        "get_telephony_number_by_id",
        AsyncMock(return_value=NS(id="N1", provider=CallProvider.PLIVO)),
    )
    assert await IC.release_inbound_channel(_inb("C1")) is True
    dec.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", [None, ""])
async def test_inbound_admit_without_call_id_on_v2_number_must_not_use_db_gate(
    wired, monkeypatch, cid
):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    _, _, inc = _legacy_spies(monkeypatch)
    await IC.admit_inbound_call("N1", call_id=cid)
    inc.assert_not_awaited()


# 6. import safety
@pytest.mark.parametrize(
    "first",
    [
        "app.ai.voice.agents.breeze_buddy.managers.inbound_channel",
        "app.ai.voice.agents.breeze_buddy.dispatch.v2.release",
        "app.ai.voice.agents.breeze_buddy.ivr.selection",
    ],
)
def test_import_order_fresh_interpreter(first):
    code = (
        f"import {first}\n"
        "import app.ai.voice.agents.breeze_buddy.managers.inbound_channel as m\n"
        "assert callable(m.admit_inbound_call)\n"
        "m._v2_admit.__call__\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stderr[-1500:]


# 7. robustness
@pytest.mark.asyncio
async def test_none_lead_and_missing_number_never_raise(wired):
    assert await R.release_lead_line(None) is None
    assert (
        await R.release_lead_line(NS(id="x", call_direction=CallDirection.OUTBOUND))
        is None
    )
    assert await R.release_lead_line(_out(n=None)) is None
    assert await R.release_lead_line(_out(n="")) is None


@pytest.mark.asyncio
async def test_release_exception_on_v2_number_returns_false_not_todays_path(
    wired, monkeypatch
):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    monkeypatch.setattr(R.scripts, "release", AsyncMock(side_effect=RuntimeError("x")))
    assert await R.release_lead_line(_out()) is False  # known v2: never today's path


@pytest.mark.asyncio
async def test_admit_exception_in_v2_does_not_escape(wired, monkeypatch):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    monkeypatch.setattr(
        scripts, "admit_inbound", AsyncMock(side_effect=RuntimeError("x"))
    )
    _legacy_spies(monkeypatch)
    try:
        r = await IC.admit_inbound_call("N1", call_id="C1")
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"exception escaped into the answer path: {e!r}")
    assert r in (True, False)


@pytest.mark.asyncio
async def test_release_redis_error_on_v2_number_does_not_run_todays_path(
    wired, monkeypatch
):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="v2")
    tok, dec, _ = _legacy_spies(monkeypatch)
    monkeypatch.setattr(R.scripts, "release", AsyncMock(return_value=None))
    await calls_mod._release_call_resources(_out())
    tok.assert_not_awaited()
    dec.assert_not_awaited()


# Swaroop's #1313 review: the mode is decided inside the release script.
@pytest.mark.asyncio
async def test_a_call_ending_while_the_number_is_handed_back_uses_todays_release(
    wired, monkeypatch
):
    """Python read 'draining', then the hand-back flipped the number to legacy and
    deleted its busy list before the script ran: today's release must run (None),
    or the DB channel the hand-back counted for this call is never given back."""
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="draining")
    await wired.sadd("bb:busy:N1", "lead:L1")
    real = R.scripts.release

    async def flip_then_release(number_id, holder):
        await wired.hset("bb:num:N1", "mode", "legacy")  # the hand-back lands here
        await wired.delete("bb:busy:N1")
        return await real(number_id, holder)

    monkeypatch.setattr(R.scripts, "release", flip_then_release)
    assert await R.release_lead_line(_out()) is None


@pytest.mark.asyncio
async def test_the_release_script_answers_not_v2_for_a_legacy_number(wired):
    await seed_number(wired, "N1", 1, {"T1": {}}, mode="legacy")
    await wired.sadd("bb:busy:N1", "lead:L1")
    assert await R.scripts.release("N1", "lead:L1") == [-1, 0]
    assert await wired.sismember("bb:busy:N1", "lead:L1")  # untouched
