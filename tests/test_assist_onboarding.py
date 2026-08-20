from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.assist.commerce import (
    assist_onboarding as service,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.schemas.breeze_buddy.assist_onboarding import AssistOnboardingStreamRequest
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse
from app.services.scraper.website.scraper import WebsiteScrapingResult


def _request(**overrides) -> AssistOnboardingStreamRequest:
    body = {
        "reseller_id": "BB_SHOPIFY",
        "merchant_id": "9b1086-18.myshopify.com",
        "merchant_name": "Hustle Culture",
        "website_url": "https://hustleculture.co.in/",
        "is_shopify": True,
        "allowed_origins": ["https://hustleculture.co.in/"],
        "provider": "google",
        "bot_brand_name": "Hustle Culture",
        "is_active": True,
    }
    body.update(overrides)
    return AssistOnboardingStreamRequest.model_validate(body)


def _default_template() -> TemplateModel:
    return TemplateModel(
        id="00000000-0000-0000-0000-000000000001",
        reseller_id="BB_SHOPIFY",
        merchant_id=None,
        name=service.DEFAULT_ASSIST_TEMPLATE_NAME,
        flow={
            "mode": "direct",
            "functions": [],
            "system_prompt": (
                f"{service.BRAND_IDENTITY_MARKER}\n\n"
                "## Operating principles\n"
                f"{service.SHOPIFY_OPERATING_START_MARKER}\n"
                "### Shopify commerce tools\n"
                "Use Shopify tools for live commerce facts.\n"
                f"{service.SHOPIFY_OPERATING_END_MARKER}\n"
                "{{ui_primitives_section}}"
            ),
        },
        expected_payload_schema={
            "shop_url": {"type": "string"},
            "shopify_customer_token": {"type": "string"},
        },
        expected_callback_response_schema={},
        configurations={
            "ui_catalog": {"enabled_groups": ["core", "effects"]},
            "mcp": {
                "servers": [
                    {
                        "name": service.SHOPIFY_MCP_SERVER_NAME,
                        "url": "https://{shop_url}/api/mcp",
                        "auth": {"type": "none"},
                    }
                ]
            },
            "state_reducers": [
                {"tool_name": tool_name, "set_paths": {"cart_id": "id"}}
                for tool_name in ("create_cart", "update_cart", "get_cart")
            ],
            "tool_arg_injection": [
                {
                    "tool_name": tool_name,
                    "set_paths": {"id": "state.data.cart_id"},
                }
                for tool_name in ("create_cart", "update_cart", "get_cart")
            ],
        },
        secrets={},
        is_active=True,
        supported_channels=["chat", "voice"],
    )


def _widget(template_id: str) -> WidgetConfigResponse:
    return WidgetConfigResponse(
        id="00000000-0000-0000-0000-000000000020",
        reseller_id="BB_SHOPIFY",
        merchant_id="9b1086-18.myshopify.com",
        public_widget_key="public-key",
        template_id=template_id,
        allowed_origins=["https://hustleculture.co.in"],
        active=True,
    )


def test_request_normalizes_urls_and_rejects_private_hosts() -> None:
    body = _request()
    assert body.website_url == "https://hustleculture.co.in"
    assert body.allowed_origins == ["https://hustleculture.co.in"]

    with pytest.raises(ValidationError):
        _request(website_url="https://127.0.0.1")

    with pytest.raises(ValidationError):
        _request(allowed_origins=["https://example.com/path"])


def test_template_builder_adds_and_removes_shopify_mcp() -> None:
    default = _default_template()
    shopify = service.build_merchant_template(
        default_template=default,
        body=_request(),
        website_context="Sells premium sneakers.",
        template_id="00000000-0000-0000-0000-000000000002",
        existing_template=None,
    )

    assert shopify.name == "hustle-culture-buddy-assist"
    assert "Sells premium sneakers." in shopify.flow["system_prompt"]
    assert service.BRAND_IDENTITY_MARKER not in shopify.flow["system_prompt"]
    assert "### Shopify commerce tools" in shopify.flow["system_prompt"]
    assert service.SHOPIFY_OPERATING_START_MARKER not in shopify.flow["system_prompt"]
    assert service.SHOPIFY_OPERATING_END_MARKER not in shopify.flow["system_prompt"]
    assert shopify.configurations is not None
    assert shopify.configurations.mcp is not None
    assert [server.name for server in shopify.configurations.mcp.servers] == [
        service.SHOPIFY_MCP_SERVER_NAME
    ]
    assert len(shopify.configurations.state_reducers) == 3
    assert len(shopify.configurations.tool_arg_injection) == 3
    assert shopify.secrets == {"shop_url": "hustleculture.co.in"}

    non_shopify = service.build_merchant_template(
        default_template=default,
        body=_request(is_shopify=False),
        website_context="Provides consulting.",
        template_id="00000000-0000-0000-0000-000000000003",
        existing_template=None,
    )
    assert non_shopify.configurations is not None
    assert "### Shopify commerce tools" not in non_shopify.flow["system_prompt"]
    assert non_shopify.configurations.mcp is None
    assert non_shopify.configurations.state_reducers == []
    assert non_shopify.configurations.tool_arg_injection == []
    assert non_shopify.configurations.client_context is None
    assert non_shopify.expected_payload_schema is not None
    assert "shopify_customer_token" not in non_shopify.expected_payload_schema


def test_first_onboarding_creates_template_and_widget(monkeypatch) -> None:
    default = _default_template()

    async def template_in_scope(reseller_id, merchant_id, name):
        if merchant_id is None and name == service.DEFAULT_ASSIST_TEMPLATE_NAME:
            return default
        return None

    monkeypatch.setattr(
        service, "get_widget_config_by_reseller_merchant", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service, "get_template_in_scope", template_in_scope)
    monkeypatch.setattr(
        service,
        "scrape_website",
        AsyncMock(
            return_value=WebsiteScrapingResult(
                text="Sells premium sneakers.",
                provider="google",
                status="generated",
                provider_response={},
                url_context_metadata=[],
            )
        ),
    )

    async def create_template_mock(**kwargs):
        return TemplateModel(
            id=kwargs["template_id"],
            reseller_id=kwargs["reseller_id"],
            merchant_id=kwargs["merchant_id"],
            name=kwargs["name"],
            flow=kwargs["flow"],
            expected_payload_schema=kwargs["expected_payload_schema"],
            expected_callback_response_schema=kwargs[
                "expected_callback_response_schema"
            ],
            configurations=kwargs["configurations"],
            secrets=kwargs["secrets"],
            is_active=kwargs["is_active"],
            supported_channels=kwargs["supported_channels"],
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(service, "create_template", create_template_mock)

    async def create_widget_mock(**kwargs):
        return _widget(kwargs["template_id"])

    monkeypatch.setattr(service, "create_widget_config", create_widget_mock)
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())

    async def collect_events():
        return [event async for event in service.stream_assist_onboarding(_request())]

    events = asyncio.run(collect_events())

    assert events[-1].event == "complete"
    assert events[-1].data["operation"] == "created"
    assert events[-1].data["template_name"] == "hustle-culture-buddy-assist"
    assert (
        events[-1].data["widget_config"]["template_id"]
        == events[-1].data["template_id"]
    )


def test_existing_widget_updates_its_referenced_template(monkeypatch) -> None:
    default = _default_template()
    existing = service.build_merchant_template(
        default_template=default,
        body=_request(),
        website_context="Old context",
        template_id="00000000-0000-0000-0000-000000000010",
        existing_template=None,
    )
    widget = _widget(existing.id)

    monkeypatch.setattr(
        service,
        "get_widget_config_by_reseller_merchant",
        AsyncMock(return_value=widget),
    )
    monkeypatch.setattr(service, "get_template_by_id", AsyncMock(return_value=existing))
    monkeypatch.setattr(
        service, "get_template_in_scope", AsyncMock(return_value=default)
    )
    monkeypatch.setattr(
        service,
        "scrape_website",
        AsyncMock(
            return_value=WebsiteScrapingResult(
                text="Fresh context",
                provider="google",
                status="generated",
                provider_response={},
                url_context_metadata=[],
            )
        ),
    )

    async def replace_template_mock(**kwargs):
        return TemplateModel(
            id=kwargs["template_id"],
            reseller_id=kwargs["reseller_id"],
            merchant_id=kwargs["merchant_id"],
            name=kwargs["name"],
            flow=kwargs["flow"],
            expected_payload_schema=kwargs["expected_payload_schema"],
            expected_callback_response_schema=kwargs[
                "expected_callback_response_schema"
            ],
            configurations=kwargs["configurations"],
            secrets=kwargs["secrets"],
            is_active=kwargs["is_active"],
            supported_channels=kwargs["supported_channels"],
        )

    monkeypatch.setattr(service, "replace_template", replace_template_mock)
    monkeypatch.setattr(service, "update_widget_config", AsyncMock(return_value=widget))
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())

    async def collect_events():
        return [event async for event in service.stream_assist_onboarding(_request())]

    events = asyncio.run(collect_events())

    assert events[-1].event == "complete"
    assert events[-1].data["operation"] == "updated"
    assert events[-1].data["template_id"] == existing.id
    assert events[-1].data["widget_config"]["id"] == widget.id


def test_orphan_template_is_recovered_when_widget_is_missing(monkeypatch) -> None:
    default = _default_template()
    orphan = service.build_merchant_template(
        default_template=default,
        body=_request(),
        website_context="Old context",
        template_id="00000000-0000-0000-0000-000000000030",
        existing_template=None,
    )

    async def template_in_scope(reseller_id, merchant_id, name):
        return default if merchant_id is None else orphan

    monkeypatch.setattr(
        service, "get_widget_config_by_reseller_merchant", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service, "get_template_in_scope", template_in_scope)
    monkeypatch.setattr(
        service,
        "scrape_website",
        AsyncMock(
            return_value=WebsiteScrapingResult(
                text="Fresh context",
                provider="google",
                status="generated",
                provider_response={},
                url_context_metadata=[],
            )
        ),
    )
    monkeypatch.setattr(service, "replace_template", AsyncMock(return_value=orphan))
    create_template_mock = AsyncMock()
    monkeypatch.setattr(service, "create_template", create_template_mock)
    monkeypatch.setattr(
        service,
        "create_widget_config",
        AsyncMock(return_value=_widget(orphan.id)),
    )
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())

    async def collect_events():
        return [event async for event in service.stream_assist_onboarding(_request())]

    events = asyncio.run(collect_events())

    assert events[-1].event == "complete"
    assert events[-1].data["operation"] == "recovered"
    assert events[-1].data["template_id"] == orphan.id
    create_template_mock.assert_not_awaited()
