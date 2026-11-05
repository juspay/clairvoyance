"""Lead push — chat-agent telephony gate.

Agent types are exclusive (2026-07-13): ``'chat' in supported_channels``
makes a template a chat (widget) agent; a ``'voice'`` entry alongside it
only enables in-widget WebRTC voice mode, never telephony. Leads ARE
telephony, so ``push_lead_handler`` must reject chat templates with a 400 —
including ``['chat', 'voice']``, which is the case a naive
``'voice' not in channels`` check would wave through.

The widget's own voice escalation (``/widget/session/{id}/voice/connect``)
creates its lead directly via ``create_lead_call_tracker`` and never passes
through this handler, so the gate cannot break widget voice mode.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.leads.handlers as lead_handlers
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.schemas.breeze_buddy.auth import UserInfo, UserRole

TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"


def _req() -> PushLeadRequest:
    return PushLeadRequest(
        request_id="order-1",
        payload={"customer_mobile_number": "+919999999999"},
        template_id=TEMPLATE_ID,
        reseller_id="RESELLER",
    )


def _user() -> UserInfo:
    # Only .username / .role are touched before the gate fires.
    return UserInfo(id="u-test", username="tester", role=UserRole.ADMIN)


def _template(channels: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=TEMPLATE_ID,
        name="t",
        reseller_id="RESELLER",
        merchant_id=None,
        supported_channels=channels,
        expected_payload_schema=None,
    )


@pytest.mark.parametrize("channels", [["chat"], ["chat", "voice"], ["voice", "chat"]])
@pytest.mark.asyncio
async def test_chat_template_rejected(monkeypatch, channels):
    monkeypatch.setattr(
        lead_handlers, "get_template_by_id", _fake_get(_template(channels))
    )
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(), _user())
    assert exc.value.status_code == 400
    assert "chat (widget) agent" in exc.value.detail


@pytest.mark.asyncio
async def test_voice_template_passes_the_gate(monkeypatch):
    """A voice template sails past the gate (next stop: blacklist check)."""
    monkeypatch.setattr(
        lead_handlers, "get_template_by_id", _fake_get(_template(["voice"]))
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("reached blacklist check")

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _boom)
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(), _user())
    # The generic except wraps the sentinel in a 500 — the point is it got
    # PAST the gate (a gate rejection would be the 400 asserted above).
    assert exc.value.status_code == 500


def _fake_get(template):
    async def _get(_id):
        return template

    return _get
