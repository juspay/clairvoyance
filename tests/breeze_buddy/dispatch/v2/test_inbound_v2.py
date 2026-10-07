from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.ai.voice.agents.breeze_buddy.managers import inbound_channel as IC
from app.schemas import CallDirection, CallProvider, LeadCallStatus


@pytest.mark.asyncio
async def test_v2_number_admits_through_busy_list(monkeypatch):
    adm = AsyncMock(return_value=True)
    monkeypatch.setattr(IC, "_v2_admit", adm)
    db = AsyncMock()
    monkeypatch.setattr(IC, "increment_telephony_number_channels", db)
    assert await IC.admit_inbound_call("N1", call_id="C1") is True
    adm.assert_awaited_once_with("N1", "C1")
    db.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_number_full_refuses_without_db(monkeypatch):
    monkeypatch.setattr(IC, "_v2_admit", AsyncMock(return_value=False))
    db = AsyncMock()
    monkeypatch.setattr(IC, "increment_telephony_number_channels", db)
    assert await IC.admit_inbound_call("N1", call_id="C1") is False
    db.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_number_uses_db_gate(monkeypatch):
    monkeypatch.setattr(IC, "_v2_admit", AsyncMock(return_value=None))
    db = AsyncMock(return_value=object())
    monkeypatch.setattr(IC, "increment_telephony_number_channels", db)
    assert await IC.admit_inbound_call("N1", call_id="C1") is True
    db.assert_awaited_once_with("N1")


@pytest.mark.asyncio
async def test_no_call_id_on_legacy_number_uses_db_gate(monkeypatch):
    monkeypatch.setattr(IC, "_v2_admit", AsyncMock(return_value=None))
    monkeypatch.setattr(
        IC, "increment_telephony_number_channels", AsyncMock(return_value=None)
    )
    assert await IC.admit_inbound_call("N1") is False


@pytest.mark.asyncio
async def test_v2_admit_wrapper_verdicts(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.dispatch.v2 import latch, routes, scripts

    adm = AsyncMock(return_value=True)
    monkeypatch.setattr(scripts, "admit_inbound", adm)
    monkeypatch.setattr(latch, "v2_seen", AsyncMock(return_value=False))
    assert await IC._v2_admit("N1", "C1") is None
    monkeypatch.setattr(latch, "v2_seen", AsyncMock(return_value=True))
    mode = AsyncMock(return_value="legacy")
    monkeypatch.setattr(routes, "number_mode_or_none", mode)
    assert await IC._v2_admit("N1", "C1") is None
    mode.return_value = "v2"
    assert await IC._v2_admit("N1", "C1") is True
    adm.assert_awaited_once_with("N1", "C1")
    assert await IC._v2_admit("N1", None) is False  # v2 number, no call id
    adm.return_value = None  # Redis error: refuse
    assert await IC._v2_admit("N1", "C1") is False
    mode.return_value = None  # mode unreadable: refuse
    assert await IC._v2_admit("N1", "C1") is False


def _lead() -> Any:
    return NS(
        id="L1",
        call_id="C1",
        telephony_number_id="N1",
        call_direction=CallDirection.INBOUND,
        status=LeadCallStatus.PROCESSING,
    )


@pytest.mark.asyncio
async def test_release_delegates_to_busy_list(monkeypatch):
    monkeypatch.setattr(IC, "_v2_release", AsyncMock(return_value=True))
    db = AsyncMock()
    monkeypatch.setattr(IC, "get_telephony_number_by_id", db)
    assert await IC.release_inbound_channel(_lead()) is True
    db.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_falls_back_to_todays_path(monkeypatch):
    monkeypatch.setattr(IC, "_v2_release", AsyncMock(return_value=None))
    dec = AsyncMock()
    monkeypatch.setattr(
        IC,
        "get_telephony_number_by_id",
        AsyncMock(return_value=NS(id="N1", provider=CallProvider.PLIVO)),
    )
    monkeypatch.setattr(IC, "decrement_telephony_number_channels", dec)
    assert await IC.release_inbound_channel(_lead()) is True
    dec.assert_awaited_once()
