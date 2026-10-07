import asyncio
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import release as R
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.schemas import CallDirection, CallProvider, ExecutionMode


def _lead(direction=CallDirection.OUTBOUND, number="N1") -> Any:
    return NS(
        id="L1",
        telephony_number_id=number,
        call_direction=direction,
        call_id="C1",
        execution_mode=ExecutionMode.TELEPHONY,
    )


@pytest.fixture
def seen(monkeypatch):
    m = AsyncMock(return_value=True)
    monkeypatch.setattr(R, "v2_seen", m)
    return m


@pytest.mark.asyncio
async def test_latch_off_is_none_and_reads_nothing(monkeypatch):
    monkeypatch.setattr(R, "v2_seen", AsyncMock(return_value=False))
    mode = AsyncMock()
    monkeypatch.setattr(R, "number_mode_or_none", mode)
    assert await R.release_lead_line(_lead()) is None
    mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbound_releases_by_stamped_number(monkeypatch, seen):
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="v2"))
    rel = AsyncMock(return_value=[1, 0])
    monkeypatch.setattr(R.scripts, "release", rel)
    assert await R.release_lead_line(_lead()) is True
    rel.assert_awaited_once_with("N1", "lead:L1")


@pytest.mark.asyncio
async def test_inbound_releases_call_holder(monkeypatch, seen):
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="v2"))
    rel = AsyncMock(return_value=[1, 0])
    monkeypatch.setattr(R.scripts, "release", rel)
    assert await R.release_lead_line(_lead(CallDirection.INBOUND)) is True
    rel.assert_awaited_once_with("N1", "call:C1")


@pytest.mark.asyncio
async def test_early_release_then_completion_is_noop(monkeypatch, seen):
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="v2"))
    monkeypatch.setattr(R.scripts, "release", AsyncMock(side_effect=[[1, 0], [0, 0]]))
    assert await R.release_lead_line(_lead()) is True
    assert await R.release_lead_line(_lead()) is False


@pytest.mark.asyncio
async def test_legacy_number_returns_none(monkeypatch, seen):
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="legacy"))
    assert await R.release_lead_line(_lead()) is None


@pytest.mark.asyncio
async def test_redis_error_retries_once_then_reports_false(monkeypatch, seen):
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="v2"))
    rel = AsyncMock(side_effect=[None, [1, 0]])
    monkeypatch.setattr(R.scripts, "release", rel)
    assert await R.release_lead_line(_lead()) is True
    assert rel.await_count == 2
    rel = AsyncMock(return_value=None)
    monkeypatch.setattr(R.scripts, "release", rel)
    assert await R.release_lead_line(_lead()) is False
    assert rel.await_count == 2


@pytest.mark.asyncio
async def test_call_end_skips_todays_release_on_v2_number(monkeypatch):
    monkeypatch.setattr(calls_mod, "_v2_release", AsyncMock(return_value=True))
    db = AsyncMock()
    tok = AsyncMock()
    monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", db)
    monkeypatch.setattr(calls_mod, "release_channel_token", tok)
    await calls_mod._release_call_resources(_lead())
    db.assert_not_awaited()
    tok.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_end_falls_through_to_todays_path_on_none(monkeypatch):
    monkeypatch.setattr(calls_mod, "_v2_release", AsyncMock(return_value=None))
    db = AsyncMock(return_value=None)
    monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", db)
    await calls_mod._release_call_resources(_lead())
    db.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_outbound_call_ending_in_v2_pending_gives_todays_gate_its_line_back(
    monkeypatch, seen
):
    """Fable M3: while a number is v2_pending, today's DB gate still admits its inbound
    calls (the busy list is seeded only at the end of the phase). An outbound call ending
    then must decrement DB ``channels`` and return its token like today, or the gate only
    climbs and inbound is refused while lines are free."""
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="v2_pending"))
    busy = AsyncMock(return_value=[0, 0])
    monkeypatch.setattr(R.scripts, "release", busy)
    number = NS(id="N1", provider=CallProvider.PLIVO)
    monkeypatch.setattr(
        calls_mod, "get_telephony_number_by_id", AsyncMock(return_value=number)
    )
    decrement = AsyncMock()
    token = AsyncMock()
    monkeypatch.setattr(calls_mod, "decrement_telephony_number_channels", decrement)
    monkeypatch.setattr(calls_mod, "release_channel_token", token)
    await calls_mod._release_call_resources(_lead())
    decrement.assert_awaited_once_with("N1")
    token.assert_awaited_once_with("N1")
    busy.assert_not_awaited()  # nothing of the number is on the busy list yet


@pytest.mark.asyncio
async def test_todays_capacity_rule_comes_before_v2_for_a_non_dispatchable_lead(
    monkeypatch,
):
    """Fable M12: an outbound lead that is not dispatchable (e.g. the outbound leg of a
    hold transfer) never took a line, in either dialler. Today's capacity rule says so
    first; v2's release is not consulted."""
    v2 = AsyncMock(return_value=False)
    monkeypatch.setattr(calls_mod, "_v2_release", v2)
    number = NS(id="N1", provider=CallProvider.PLIVO)
    monkeypatch.setattr(
        calls_mod, "get_telephony_number_by_id", AsyncMock(return_value=number)
    )
    decrement = AsyncMock()
    monkeypatch.setattr(calls_mod, "decrement_telephony_number_channels", decrement)
    lead = _lead()
    lead.execution_mode = ExecutionMode.HOLD_TRANSFER
    await calls_mod._release_call_resources(lead)
    v2.assert_not_awaited()
    decrement.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_hung_release_never_stalls_a_calls_end(monkeypatch, seen):
    # Both release attempts are bounded like every v2 call on today's paths
    # (rule 42); the ledger frees a finished lead's holder later
    monkeypatch.setattr(R, "number_mode_or_none", AsyncMock(return_value="v2"))
    monkeypatch.setattr(R, "TODAYS_PATH_TIMEOUT_S", 0.05)
    attempts = []

    async def hung(number_id, holder):
        attempts.append(holder)
        await asyncio.Event().wait()

    monkeypatch.setattr(R.scripts, "release", hung)
    assert await asyncio.wait_for(R.release_lead_line(_lead()), timeout=2) is False
    assert attempts == ["lead:L1", "lead:L1"]
