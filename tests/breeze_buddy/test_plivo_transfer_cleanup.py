from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from fastapi import Request

from app.ai.voice.agents.breeze_buddy.handlers.internal import warm_transfer
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.api.routers.breeze_buddy.telephony.callbacks import (
    handlers as callback_handlers,
)
from app.schemas import CallProvider


class _FailingConferenceService:
    async def handle_transfer(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": False,
            "reason": "call_not_found",
            "error": "call not found",
        }


class _Request:
    def __init__(self, query_params: dict[str, str]):
        self.query_params = query_params

    async def form(self) -> dict[str, str]:
        return {}


async def test_legacy_transfer_failure_clears_transfer_flag(monkeypatch):
    cleared: list[str] = []

    async def _set_transfer_flag(**kwargs: Any) -> bool:
        return True

    async def _clear_transfer_flag_safely(call_sid: str, reason: str) -> bool:
        cleared.append(call_sid)
        return True

    monkeypatch.setattr(warm_transfer, "set_transfer_flag", _set_transfer_flag)
    monkeypatch.setattr(
        warm_transfer, "clear_transfer_flag_safely", _clear_transfer_flag_safely
    )

    context = SimpleNamespace(
        call_sid="call-1",
        provider=CallProvider.PLIVO,
        lead=SimpleNamespace(
            reseller_id="res-1",
            merchant_id="merchant-1",
            metaData={},
        ),
    )

    result = await warm_transfer._transfer_legacy(
        cast(TemplateContext, context),
        conference_service=_FailingConferenceService(),
        agent_phone_number="+15551234567",
        conference_name="conf-1",
        telephony_number="+15559999999",
        customer_phone_number="+14155552671",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "call_not_found"
    assert cleared == ["call-1"]


async def test_transfer_conclude_callback_clears_transfer_flag(monkeypatch):
    cleared: list[str] = []

    async def _clear_transfer_flag_safely(call_sid: str, reason: str) -> bool:
        cleared.append(call_sid)
        return True

    monkeypatch.setattr(
        callback_handlers, "clear_transfer_flag_safely", _clear_transfer_flag_safely
    )

    response = await callback_handlers.handle_call_transfer(
        cast(Request, _Request({"customer_call_sid": "call-2"})), "plivo", "conclude"
    )

    assert response.status_code == 200
    assert cleared == ["call-2"]
