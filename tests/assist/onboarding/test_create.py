"""``POST /assist/create``: an assistant built from research, switched off."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.commerce import (
    vertical as commerce_vertical,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding import service
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.routers.breeze_buddy.assist import onboarding
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.onboarding import AssistCreateRequest
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
MERCHANT = UserInfo(
    id="m-1",
    username="merchant",
    role=UserRole.MERCHANT,
    reseller_ids=["BB_ASSIST"],
    merchant_ids=["kosha"],
)
NOTES = [
    {"field": "brand_line", "value": "Kosha: merino for Indian winters"},
    {"field": "offer_items", "value": "20% off thermals"},
    {"field": "returns", "value": "Easy 7-day returns"},
    {"field": "whatsapp", "value": "+91 98765 43210"},
]


def _body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "reseller_id": "BB_ASSIST",
        "merchant_id": "kosha",
        "merchant_name": "Kosha",
        "website_url": "https://kosha.example/",
        "platform": "web",
        "notes": NOTES,
        "appearance": {"primary_color": "#c22126"},
    }
    body.update(overrides)
    return body


def _blueprint() -> TemplateModel:
    body = json.loads((FIXTURE / "buddy-assist-default.v2.json").read_text())
    return TemplateModel(
        id="00000000-0000-0000-0000-00000000b1e0",
        reseller_id="BB_ASSIST",
        merchant_id=None,
        name=body["name"],
        flow=body["flow"],
        expected_payload_schema=body["expected_payload_schema"],
        expected_callback_response_schema=body["expected_callback_response_schema"]
        or {},
        configurations=body["configurations"],
        secrets={},
        is_active=True,
        supported_channels=body["supported_channels"],
    )


@pytest.fixture
def saved(monkeypatch) -> Dict[str, Any]:
    """Stubs the store: no assistant yet, the blueprint exists; records writes."""
    writes: Dict[str, Any] = {}

    async def template_in_scope(reseller_id, merchant_id, name):
        if (
            merchant_id is None
            and name == commerce_vertical.DEFAULT_ASSIST_TEMPLATE_NAME
        ):
            return _blueprint()
        return None

    async def create_template(**kwargs):
        writes["template"] = kwargs
        return TemplateModel(
            id=kwargs["template_id"],
            reseller_id=kwargs["reseller_id"],
            merchant_id=kwargs["merchant_id"],
            name=kwargs["name"],
            flow=kwargs["flow"],
            configurations=kwargs["configurations"],
            is_active=kwargs["is_active"],
        )

    async def create_widget(**kwargs):
        writes["widget"] = kwargs
        return WidgetConfigResponse(
            id="00000000-0000-0000-0000-000000000020",
            reseller_id=kwargs["reseller_id"],
            merchant_id=kwargs["merchant_id"],
            public_widget_key="public-key",
            template_id=kwargs["template_id"],
            allowed_origins=kwargs["allowed_origins"],
            active=kwargs["active"],
            appearance=kwargs["appearance"],
        )

    monkeypatch.setattr(
        service, "get_widget_config_by_reseller_merchant", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service, "get_template_in_scope", template_in_scope)
    monkeypatch.setattr(service, "_ensure_assist_merchant", AsyncMock())
    monkeypatch.setattr(service, "create_template", create_template)
    monkeypatch.setattr(service, "create_widget_config", create_widget)
    return writes


async def test_the_assistant_is_built_from_the_findings_and_switched_off(
    saved,
) -> None:
    created = await service.create_assistant(AssistCreateRequest(**_body()))

    template, widget = saved["template"], saved["widget"]
    assert template["is_active"] is False and widget["active"] is False
    assert widget["allowed_origins"] == ["https://kosha.example"]
    assert widget["appearance"] == {"primary_color": "#c22126"}

    prompt = template["flow"]["system_prompt"]
    assert "- **Brand:** Kosha: merino for Indian winters" in prompt
    assert "### Store policies\n\n- Easy 7-day returns" in prompt
    assert "### Trust signals" not in prompt  # nothing found, nothing shown
    # No deciding question was filled, so the blueprint's own section stays.
    assert "### Sizing and selection help" in prompt

    config = template["configurations"]
    assert config["assist_fields"] == created.fields
    assert created.fields["policies"] == ["Easy 7-day returns"]
    # The names research did not supply are filled in, so a save can rebuild
    # the brand block from the fields alone.
    assert created.fields["assistant_name"] == ["Kosha Assist"]
    assert "- **Assistant name:** Kosha Assist" in prompt
    # The WhatsApp link joins the blueprint's trusted links, it does not
    # replace them; the blueprint's greeting stays until the merchant sets one.
    trusted = config["render_ui"]["trusted_link_urls"]
    assert "https://wa.me/919876543210" in trusted
    assert "https://kosha.example/cart" in trusted
    assert config["initial_greeting"] == "Hi! What are you looking for today?"


def test_filled_questions_replace_the_blueprint_section() -> None:
    section = commerce_vertical.vertical.vertical_section(
        {
            "question": ["Which size should I get?"],
            "question_answer": ["Sizes run true to chest."],
            "question_2": ["Will it survive a wash?"],
            "question_answer_2": ["Machine wash cold."],
            "question_3": ["Left without an answer"],
        }
    )
    assert section == (
        "### Which size should I get?\n\nSizes run true to chest.\n\n"
        "#### Will it survive a wash?\n\nMachine wash cold.\n"
    )


async def test_a_merchant_with_an_assistant_is_left_alone(saved, monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "get_widget_config_by_reseller_merchant",
        AsyncMock(return_value=object()),
    )
    with pytest.raises(service.AssistantExistsError):
        await service.create_assistant(AssistCreateRequest(**_body()))
    assert saved == {}


async def test_a_failed_widget_takes_its_template_with_it(saved, monkeypatch) -> None:
    monkeypatch.setattr(
        service, "create_widget_config", AsyncMock(side_effect=RuntimeError("db"))
    )
    delete = AsyncMock()
    monkeypatch.setattr(service, "delete_template_if_not_referenced", delete)
    with pytest.raises(RuntimeError):
        await service.create_assistant(AssistCreateRequest(**_body()))
    delete.assert_awaited_once_with(saved["template"]["template_id"])


async def test_a_create_that_loses_a_race_is_reported_as_existing(
    saved, monkeypatch
) -> None:
    # Two tabs: the other request's widget landed between our check and ours.
    monkeypatch.setattr(
        service,
        "get_widget_config_by_reseller_merchant",
        AsyncMock(side_effect=[None, object()]),
    )
    monkeypatch.setattr(
        service, "create_widget_config", AsyncMock(side_effect=RuntimeError("unique"))
    )
    monkeypatch.setattr(service, "delete_template_if_not_referenced", AsyncMock())
    with pytest.raises(service.AssistantExistsError):
        await service.create_assistant(AssistCreateRequest(**_body()))


def test_the_route_refuses_another_merchant_and_reports_an_existing_one(
    monkeypatch,
) -> None:
    create = AsyncMock(side_effect=service.AssistantExistsError("kosha"))
    monkeypatch.setattr(onboarding, "create_assistant", create)
    app = FastAPI()
    app.include_router(onboarding.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: MERCHANT
    client = TestClient(app)

    other = client.post("/assist/create", json=_body(merchant_id="someone-else"))
    assert other.status_code == 403
    create.assert_not_awaited()

    assert client.post("/assist/create", json=_body()).status_code == 409
