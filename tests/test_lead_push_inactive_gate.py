"""Lead push — inactive-template gate.

``is_active=false`` is the merchant's off switch: ``push_lead_handler`` must
refuse leads for an inactive template with a 409. The dialler stops leads
already queued (``tests/breeze_buddy/dispatch/test_inactive_template.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.leads.handlers as lead_handlers
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.schemas import ExecutionMode
from app.schemas.breeze_buddy.auth import UserInfo, UserRole

TEMPLATE_ID = "6b1f0d3c-8a2e-4f5b-9c7d-1e2a3b4c5d6e"


def _req(**extra) -> PushLeadRequest:
    return PushLeadRequest(
        request_id="order-1",
        payload={"customer_mobile_number": "+919999999999"},
        template_id=TEMPLATE_ID,
        reseller_id="RESELLER",
        **extra,
    )


def _user() -> UserInfo:
    return UserInfo(id="u-test", username="tester", role=UserRole.ADMIN)


def _template(is_active: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=TEMPLATE_ID,
        name="t",
        reseller_id="RESELLER",
        merchant_id=None,
        supported_channels=["voice"],
        expected_payload_schema=None,
        is_active=is_active,
    )


def _use_template(monkeypatch, template) -> None:
    async def _get(_id):
        return template

    monkeypatch.setattr(lead_handlers, "get_template_by_id", _get)


def _stop_after_the_gate(monkeypatch) -> None:
    """The next check after the gate is the blacklist; make it raise so a
    push that got past the gate surfaces as the handler's generic 500."""

    async def _boom(*_a, **_k):
        raise RuntimeError("reached blacklist check")

    monkeypatch.setattr(lead_handlers, "is_number_blacklisted", _boom)


@pytest.mark.asyncio
async def test_inactive_template_rejects_customer_lead(monkeypatch):
    _use_template(monkeypatch, _template(is_active=False))
    _stop_after_the_gate(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(), _user())
    assert exc.value.status_code == 409
    assert "is inactive" in exc.value.detail


@pytest.mark.parametrize(
    "extra",
    [
        {"execution_mode": ExecutionMode.TELEPHONY_TEST},
        {"is_playground": True},
    ],
    ids=["test-call", "playground"],
)
@pytest.mark.asyncio
async def test_inactive_template_still_takes_test_and_playground(monkeypatch, extra):
    _use_template(monkeypatch, _template(is_active=False))
    _stop_after_the_gate(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(**extra), _user())
    # 500 = it reached the blacklist sentinel, i.e. got PAST the gate.
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_active_template_passes_the_gate(monkeypatch):
    _use_template(monkeypatch, _template(is_active=True))
    _stop_after_the_gate(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await lead_handlers.push_lead_handler(_req(), _user())
    assert exc.value.status_code == 500
