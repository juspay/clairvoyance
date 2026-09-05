from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.ai.voice.agents.breeze_buddy.services.telephony import (
    callback_correlation,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo import (
    correlation as plivo_correlation,
)
from app.schemas import CallProvider


async def test_bind_outbound_call_identity_ignores_non_plivo(monkeypatch) -> None:
    called: list[dict[str, Any]] = []

    async def bind_plivo(**kwargs: Any) -> bool:
        called.append(kwargs)
        return True

    monkeypatch.setattr(
        callback_correlation, "bind_plivo_outbound_call_uuid", bind_plivo
    )

    result = await callback_correlation.bind_outbound_call_identity(
        provider=CallProvider.TWILIO,
        params={"lead_id": "lead-1", "telephony_number_id": "num-1"},
        call_id="CA-test",
    )

    assert result is False
    assert called == []


async def test_bind_outbound_call_identity_binds_plivo_callback(monkeypatch) -> None:
    called: list[dict[str, Any]] = []

    async def bind_plivo(**kwargs: Any) -> bool:
        called.append(kwargs)
        return True

    monkeypatch.setattr(
        callback_correlation, "bind_plivo_outbound_call_uuid", bind_plivo
    )

    result = await callback_correlation.bind_outbound_call_identity(
        provider="plivo",
        params={"lead_id": "lead-1", "telephony_number_id": "num-1"},
        call_id="plivo-call-uuid",
    )

    assert result is True
    assert called == [
        {
            "lead_id": "lead-1",
            "call_uuid": "plivo-call-uuid",
            "telephony_number_id": "num-1",
        }
    ]


async def test_plivo_callback_binds_submitted_request_id(monkeypatch) -> None:
    called: list[dict[str, Any]] = []

    async def bind_submitted_call_uuid(*args: Any, **kwargs: Any) -> Any:
        called.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id="lead-1")

    async def update_lead_call_details(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("BACKLOG fallback should not run after bind succeeds")

    monkeypatch.setattr(
        plivo_correlation, "bind_submitted_call_uuid", bind_submitted_call_uuid
    )
    monkeypatch.setattr(
        plivo_correlation, "update_lead_call_details", update_lead_call_details
    )

    result = await plivo_correlation.bind_plivo_outbound_call_uuid(
        lead_id="lead-1",
        call_uuid="plivo-call-uuid",
        telephony_number_id="num-1",
    )

    assert result is True
    assert called[0]["args"][0] == "lead-1"
    assert called[0]["args"][1] == "plivo-call-uuid"
    assert called[0]["args"][3] == "num-1"
