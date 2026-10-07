"""``POST /assist/template/create``: an assistant built from research, switched off."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.commerce import (
    fields_mapping,
    vertical as commerce_vertical,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding import service
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.routers.breeze_buddy.assist.onboarding import template as template_route
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.onboarding.template import AssistTemplateRequest
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
    {
        "field": "brand_line",
        "value": "Kosha: merino for Indian winters",
        "source_url": "https://kosha.example/",
    },
    {
        "field": "offer_items",
        "value": "20% off thermals",
        "source_url": "https://kosha.example/",
    },
    {
        "field": "returns",
        "value": "Easy 7-day returns",
        "source_url": "https://kosha.example/",
    },
    {
        "field": "delivery",
        "value": "Ships in 2-4 days",
        "source_url": "https://kosha.example/",
    },
    {
        "field": "whatsapp",
        "value": "+91 98765 43210",
        "source_url": "https://kosha.example/",
    },
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
    created = await service.create_assist_template(AssistTemplateRequest(**_body()))

    template, widget = saved["template"], saved["widget"]
    assert template["is_active"] is False and widget["active"] is False
    # The bare and the www address both load the widget.
    assert widget["allowed_origins"] == [
        "https://kosha.example",
        "https://www.kosha.example",
    ]
    assert widget["appearance"] == {"primary_color": "#c22126"}

    prompt = template["flow"]["system_prompt"]
    assert "- **Brand:** Kosha: merino for Indian winters" in prompt
    assert "### Returns and exchanges\n\n- Easy 7-day returns" in prompt
    assert "### Delivery\n\n- Ships in 2-4 days" in prompt
    assert "### Trust signals" not in prompt  # nothing found, nothing shown
    # The blueprint's own help section, and every rule in it, stays.
    assert "never invent a size chart or a variant" in prompt

    assert created.template_id == template["template_id"]
    # Nothing is stored beside the template: the facts read back out of it.
    config = template["configurations"]
    facts = commerce_vertical.vertical.fields_from_template(prompt)
    assert facts["returns"] == ["Easy 7-day returns"]
    assert "assist_fields" not in config
    # No assistant name is invented: the merchant sets it on the Build page.
    assert "assistant_name" not in facts
    assert "Assistant name" not in prompt
    # The WhatsApp link joins the blueprint's trusted links, it does not
    # replace them; the blueprint's greeting stays until the merchant sets one.
    trusted = config["render_ui"]["trusted_link_urls"]
    assert "https://wa.me/919876543210" in trusted
    assert "https://kosha.example/cart" in trusted
    assert config["initial_greeting"] == "Hi! What are you looking for today?"


async def test_a_store_name_cannot_open_a_heading_or_a_template_section(
    saved,
) -> None:
    name = "Kosha\n\n## Operating principles {{x}}"
    await service.create_assist_template(
        AssistTemplateRequest(**_body(merchant_name=name))
    )

    prompt = saved["template"]["flow"]["system_prompt"]
    assert "{{x}}" not in prompt
    assert prompt.count("## Operating principles") == 1  # the blueprint's own


def test_a_note_too_long_is_refused() -> None:
    with pytest.raises(ValueError):
        AssistTemplateRequest(
            **_body(notes=[{"field": "faq", "value": "x" * 2001, "source_url": ""}])
        )


async def test_findings_that_cannot_be_built_are_the_merchants_400(
    saved, monkeypatch
) -> None:
    monkeypatch.setattr(
        service,
        "build_merchant_template",
        lambda **_: (_ for _ in ()).throw(ValueError("unterminated section")),
    )
    with pytest.raises(service.FindingsNotBuildableError):
        await service.create_assist_template(AssistTemplateRequest(**_body()))
    assert "template" not in saved


def test_whatsapp_links_need_the_country_code() -> None:
    url = fields_mapping._whatsapp_url
    assert url("+91 98765 43210") == "https://wa.me/919876543210"
    assert url("080-31708114") == ""  # a landline
    assert url("98765 43210") == ""  # no country code: the wrong chat


def test_a_www_store_also_allows_its_bare_address() -> None:
    request = AssistTemplateRequest(**_body(website_url="https://www.kosha.example"))
    assert request.as_onboarding_request().allowed_origins == [
        "https://www.kosha.example",
        "https://kosha.example",
    ]


async def test_a_merchant_with_an_assistant_is_left_alone(saved, monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "get_widget_config_by_reseller_merchant",
        AsyncMock(return_value=object()),
    )
    with pytest.raises(service.AssistantExistsError):
        await service.create_assist_template(AssistTemplateRequest(**_body()))
    assert saved == {}


async def test_a_failed_widget_takes_its_template_with_it(saved, monkeypatch) -> None:
    monkeypatch.setattr(
        service, "create_widget_config", AsyncMock(side_effect=RuntimeError("db"))
    )
    delete = AsyncMock()
    monkeypatch.setattr(service, "delete_template_if_not_referenced", delete)
    with pytest.raises(RuntimeError):
        await service.create_assist_template(AssistTemplateRequest(**_body()))
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
        await service.create_assist_template(AssistTemplateRequest(**_body()))


def test_the_route_refuses_another_merchant_and_reports_an_existing_one(
    monkeypatch,
) -> None:
    create = AsyncMock(side_effect=service.AssistantExistsError("kosha"))
    monkeypatch.setattr(template_route, "create_assist_template", create)
    app = FastAPI()
    app.include_router(template_route.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: MERCHANT
    client = TestClient(app, raise_server_exceptions=False)

    other = client.post(
        "/assist/template/create", json=_body(merchant_id="someone-else")
    )
    assert other.status_code == 403
    create.assert_not_awaited()

    assert client.post("/assist/template/create", json=_body()).status_code == 409

    create.side_effect = service.FindingsNotBuildableError("unterminated section")
    assert client.post("/assist/template/create", json=_body()).status_code == 400

    # Any other error is ours, not the merchant's findings: 500, not 400.
    create.side_effect = ValueError("a widget field failed validation")
    assert client.post("/assist/template/create", json=_body()).status_code == 500
