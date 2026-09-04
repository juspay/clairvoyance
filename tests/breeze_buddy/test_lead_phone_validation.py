from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.leads.handlers as lead_handlers
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.schemas import ExecutionMode, UserInfo, UserRole

TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"


def _request(phone: Any) -> PushLeadRequest:
    return PushLeadRequest(
        request_id="order-1",
        payload={"customer_mobile_number": phone},
        template_id=TEMPLATE_ID,
        reseller_id="reseller-1",
        execution_mode=ExecutionMode.TELEPHONY_TEST,
    )


def _user() -> UserInfo:
    return UserInfo(id="test", username="test", role=UserRole.ADMIN)


def _template() -> SimpleNamespace:
    return SimpleNamespace(
        id=TEMPLATE_ID,
        name="phone-test",
        reseller_id="reseller-1",
        merchant_id=None,
        supported_channels=["voice"],
        expected_payload_schema=None,
        configurations=None,
    )


@pytest.mark.asyncio
async def test_generic_ingress_canonicalizes_before_persistence(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def get_template(_template_id: str):
        return _template()

    async def is_blacklisted(phone: str, _reseller_id: str) -> bool:
        captured["blacklist_phone"] = phone
        return False

    async def get_config(_template_id: str):
        return SimpleNamespace(initial_offset=0)

    async def language(*_args, **_kwargs):
        return "en", "English"

    async def tts(*_args, **_kwargs):
        return None

    async def create_lead(**kwargs):
        captured["persisted_payload"] = kwargs["payload"]
        return SimpleNamespace(id=kwargs["id"])

    async def schedule(**_kwargs):
        return True

    monkeypatch.setattr(lead_handlers, "get_template_by_id", get_template)
    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", is_blacklisted)
    monkeypatch.setattr(
        lead_handlers, "get_call_execution_config_by_template_id", get_config
    )
    monkeypatch.setattr(lead_handlers, "determine_language_for_call", language)
    monkeypatch.setattr(lead_handlers, "determine_tts_provider_for_call", tts)
    monkeypatch.setattr(lead_handlers, "create_lead_call_tracker", create_lead)
    monkeypatch.setattr(lead_handlers, "schedule_lead", schedule)

    result = await lead_handlers.push_lead_handler(_request("+91 98765 43210"), _user())

    assert result["status"] == "queued"
    assert captured["blacklist_phone"] == "+919876543210"
    assert captured["persisted_payload"]["customer_mobile_number"] == "+919876543210"


@pytest.mark.parametrize(
    ("phone", "message"),
    [
        ("9876543210", "must start with '+'"),
        ("not-a-phone", "must start with '+'"),
        ("", "phone number is required"),
        (None, "phone number must be a string"),
    ],
)
@pytest.mark.asyncio
async def test_generic_ingress_rejects_non_e164_phone(
    monkeypatch, phone: Any, message: str
) -> None:
    async def get_template(_template_id: str):
        return _template()

    monkeypatch.setattr(lead_handlers, "get_template_by_id", get_template)

    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_request(phone), _user())

    assert exc.value.status_code == 400
    assert message in exc.value.detail
