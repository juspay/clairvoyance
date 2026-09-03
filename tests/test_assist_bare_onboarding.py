"""Bare-metal (non-streaming, no-scrape) Assist onboarding.

Mirrors tests/test_assist_onboarding.py's fixture style: service-level,
every accessor AsyncMock'ed, no DB. The property under test throughout:
``onboard_assist_bare`` NEVER touches the scraper, derives tenancy from
the merchant domain server-side, and otherwise keeps the stream path's
idempotency contract (created / updated / recovered, rollback on widget
failure).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.assist.commerce import (
    assist_onboarding as service,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.tenancy import (
    assist_tenant,
    assist_tenant_candidates,
    normalize_merchant_domain,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.schemas.breeze_buddy.assist_onboarding import AssistOnboardRequest
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse

MERCHANT_DOMAIN = "yuqe0m-xr.myshopify.com"
# Default host app is the standalone buddy-assist app → BB_ASSIST namespace.
RESELLER_ID, MERCHANT_ID = assist_tenant("buddy-assist", MERCHANT_DOMAIN)


def _request(**overrides) -> AssistOnboardRequest:
    body = {
        "merchant_domain": MERCHANT_DOMAIN,
        "merchant_name": "Zodiac",
        "allowed_origins": ["https://zodiaconline.com"],
        "appearance": {"header_title": "Zodiac Assist", "launcher_label": ""},
    }
    body.update(overrides)
    return AssistOnboardRequest.model_validate(body)


def _default_template() -> TemplateModel:
    return TemplateModel(
        id="00000000-0000-0000-0000-000000000001",
        reseller_id=RESELLER_ID,
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
            ),
        },
        expected_payload_schema={
            "shop_url": {"type": "string"},
            "shopify_customer_token": {"type": "string"},
        },
        expected_callback_response_schema={},
        configurations={
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
                {"tool_name": tool, "set_paths": {"cart_id": "id"}}
                for tool in ("create_cart", "update_cart", "get_cart")
            ],
            "tool_arg_injection": [
                {"tool_name": tool, "set_paths": {"id": "state.data.cart_id"}}
                for tool in ("create_cart", "update_cart", "get_cart")
            ],
        },
        secrets={},
        is_active=True,
        supported_channels=["chat"],
    )


def _widget(template_id: str, **overrides) -> WidgetConfigResponse:
    fields = {
        "id": "00000000-0000-0000-0000-000000000020",
        "reseller_id": RESELLER_ID,
        "merchant_id": MERCHANT_ID,
        "public_widget_key": "public-key-123",
        "template_id": template_id,
        "allowed_origins": [f"https://{MERCHANT_DOMAIN}", "https://zodiaconline.com"],
        "active": True,
        "appearance": {"header_title": "Zodiac Assist"},
    }
    fields.update(overrides)
    return WidgetConfigResponse(**fields)


@pytest.fixture()
def wired(monkeypatch):
    """Patch every accessor; return the mock bundle for assertions."""
    mocks = {
        "get_merchant": AsyncMock(return_value=None),
        "create_merchant": AsyncMock(return_value=object()),
        "get_widget": AsyncMock(return_value=None),
        "get_template_by_id": AsyncMock(return_value=None),
        "get_template_in_scope": AsyncMock(),
        "create_template": AsyncMock(),
        "replace_template": AsyncMock(),
        "create_widget_config": AsyncMock(),
        "update_widget_config": AsyncMock(),
        "delete_template": AsyncMock(),
        "invalidate": AsyncMock(),
        "scrape": AsyncMock(
            side_effect=AssertionError("bare onboarding must never scrape")
        ),
    }
    monkeypatch.setattr(
        service, "get_merchant_by_merchant_identifier", mocks["get_merchant"]
    )
    monkeypatch.setattr(service, "create_merchant", mocks["create_merchant"])
    monkeypatch.setattr(
        service, "get_widget_config_by_reseller_merchant", mocks["get_widget"]
    )
    monkeypatch.setattr(service, "get_template_by_id", mocks["get_template_by_id"])
    monkeypatch.setattr(
        service, "get_template_in_scope", mocks["get_template_in_scope"]
    )
    monkeypatch.setattr(service, "create_template", mocks["create_template"])
    monkeypatch.setattr(service, "replace_template", mocks["replace_template"])
    monkeypatch.setattr(service, "create_widget_config", mocks["create_widget_config"])
    monkeypatch.setattr(service, "update_widget_config", mocks["update_widget_config"])
    monkeypatch.setattr(
        service, "delete_template_if_not_referenced", mocks["delete_template"]
    )
    monkeypatch.setattr(service, "invalidate_template", mocks["invalidate"])
    monkeypatch.setattr(service, "scrape_website", mocks["scrape"])
    return mocks


def _scope_lookup(existing_merchant_template, default):
    """get_template_in_scope: merchant-scoped call first, blueprint second."""

    async def lookup(reseller_id, merchant_id, name):
        if merchant_id is None:
            assert name == service.DEFAULT_ASSIST_TEMPLATE_NAME
            return default
        return existing_merchant_template

    return lookup


def test_tenancy_helpers() -> None:
    assert normalize_merchant_domain("HTTPS://Zodiaconline.COM/") == "zodiaconline.com"
    # Host app decides the reseller: standalone app gets its own namespace
    # (prefixed — the merchants PK is global, plain would collide with the
    # voice row); the breeze-buddy tab shares the voice tenant.
    assert assist_tenant("buddy-assist", MERCHANT_DOMAIN) == (
        "BB_ASSIST",
        f"assist-{MERCHANT_DOMAIN}",
    )
    assert assist_tenant("breeze-buddy", MERCHANT_DOMAIN) == (
        "BB_SHOPIFY",
        MERCHANT_DOMAIN,
    )
    # Domain-only resolution probes the standalone namespace first.
    assert assist_tenant_candidates(MERCHANT_DOMAIN) == (
        ("BB_ASSIST", f"assist-{MERCHANT_DOMAIN}"),
        ("BB_SHOPIFY", MERCHANT_DOMAIN),
    )
    with pytest.raises(ValueError):
        normalize_merchant_domain("not a domain")


def test_breeze_buddy_host_app_lands_on_shared_voice_tenant(wired) -> None:
    default = _default_template()
    default.reseller_id = "BB_SHOPIFY"
    wired["get_template_in_scope"].side_effect = _scope_lookup(None, default)
    wired["create_template"].side_effect = lambda **kwargs: _echo_template(kwargs)
    wired["create_widget_config"].return_value = _widget(
        "00000000-0000-0000-0000-000000000002",
        reseller_id="BB_SHOPIFY",
        merchant_id=MERCHANT_DOMAIN,
    )

    asyncio.run(service.onboard_assist_bare(_request(host_app="breeze-buddy")))

    merchant_kwargs = wired["create_merchant"].call_args.kwargs
    assert merchant_kwargs["reseller_id"] == "BB_SHOPIFY"
    assert merchant_kwargs["merchant_id"] == MERCHANT_DOMAIN
    widget_kwargs = wired["create_widget_config"].call_args.kwargs
    assert widget_kwargs["reseller_id"] == "BB_SHOPIFY"
    assert widget_kwargs["merchant_id"] == MERCHANT_DOMAIN


def test_appearance_urls_must_be_https() -> None:
    with pytest.raises(ValidationError):
        _request(
            appearance={"custom_skin_url": "http://cdn.example.com/skin/index.html"}
        )
    body = _request(
        appearance={
            "custom_skin_url": "https://cdn.example.com/skin/index.html",
            "custom_style_url": "https://cdn.example.com/skin/theme.css",
            "position": "bottom-left",
            "draggable": "false",
        }
    )
    assert body.appearance is not None
    assert body.appearance.custom_skin_url == "https://cdn.example.com/skin/index.html"


def test_request_rejects_path_origins() -> None:
    with pytest.raises(ValidationError):
        _request(allowed_origins=["https://zodiaconline.com/pages/contact"])


def test_created_path_never_scrapes_and_derives_tenancy(wired) -> None:
    default = _default_template()
    wired["get_template_in_scope"].side_effect = _scope_lookup(None, default)
    wired["create_template"].side_effect = lambda **kwargs: _echo_template(kwargs)
    created_widget = _widget("00000000-0000-0000-0000-000000000002")
    wired["create_widget_config"].return_value = created_widget

    result = asyncio.run(service.onboard_assist_bare(_request()))

    assert result.operation == "created"
    assert result.merchant_created is True
    wired["scrape"].assert_not_called()

    merchant_kwargs = wired["create_merchant"].call_args.kwargs
    assert merchant_kwargs["merchant_id"] == MERCHANT_ID
    assert merchant_kwargs["reseller_id"] == RESELLER_ID

    template_kwargs = wired["create_template"].call_args.kwargs
    assert template_kwargs["reseller_id"] == RESELLER_ID
    assert template_kwargs["merchant_id"] == MERCHANT_ID
    prompt = template_kwargs["flow"]["system_prompt"]
    assert service.BRAND_IDENTITY_MARKER not in prompt
    assert "has not been personalized" in prompt
    assert "### Shopify commerce tools" in prompt

    widget_kwargs = wired["create_widget_config"].call_args.kwargs
    # Exact list: merchant domain origin always first, extras preserved.
    assert widget_kwargs["allowed_origins"] == [
        f"https://{MERCHANT_DOMAIN}",
        "https://zodiaconline.com",
    ]
    assert widget_kwargs["appearance"] == {
        "header_title": "Zodiac Assist",
        "launcher_label": "",
    }
    assert result.widget_config["appearance"] == {"header_title": "Zodiac Assist"}


def test_updated_path_keeps_existing_widget_and_merchant(wired) -> None:
    default = _default_template()
    existing_template = _echo_template(
        {
            "template_id": "00000000-0000-0000-0000-000000000009",
            "reseller_id": RESELLER_ID,
            "merchant_id": MERCHANT_ID,
            "flow": default.flow,
        }
    )
    widget = _widget(existing_template.id)
    wired["get_merchant"].return_value = object()
    wired["get_widget"].return_value = widget
    wired["get_template_by_id"].return_value = existing_template
    wired["update_widget_config"].return_value = widget

    result = asyncio.run(service.onboard_assist_bare(_request()))

    assert result.operation == "updated"
    assert result.merchant_created is False
    wired["create_merchant"].assert_not_called()
    # Existing templates are NEVER rebuilt — a reinstall must not wipe a
    # personalized prompt back to bare-metal, and no blueprint is needed.
    wired["create_template"].assert_not_called()
    wired["replace_template"].assert_not_called()
    assert result.template_id == existing_template.id
    update_kwargs = wired["update_widget_config"].call_args.kwargs
    assert update_kwargs["appearance"] == {
        "header_title": "Zodiac Assist",
        "launcher_label": "",
    }


def test_widget_create_failure_rolls_back_new_template(wired) -> None:
    default = _default_template()
    wired["get_template_in_scope"].side_effect = _scope_lookup(None, default)
    wired["create_template"].side_effect = lambda **kwargs: _echo_template(kwargs)
    wired["create_widget_config"].side_effect = RuntimeError("insert failed")

    with pytest.raises(RuntimeError):
        asyncio.run(service.onboard_assist_bare(_request()))

    wired["delete_template"].assert_awaited_once()


def test_merchant_create_race_settles_as_existing(wired) -> None:
    default = _default_template()
    # First existence check misses; create blows up (unique violation);
    # re-check finds the row the concurrent install wrote.
    wired["get_merchant"].side_effect = [None, object()]
    wired["create_merchant"].side_effect = RuntimeError("duplicate key")
    wired["get_template_in_scope"].side_effect = _scope_lookup(None, default)
    wired["create_template"].side_effect = lambda **kwargs: _echo_template(kwargs)
    wired["create_widget_config"].return_value = _widget(
        "00000000-0000-0000-0000-000000000002"
    )

    result = asyncio.run(service.onboard_assist_bare(_request()))
    assert result.merchant_created is False


def test_missing_blueprint_fails_closed(wired) -> None:
    wired["get_template_in_scope"].side_effect = _scope_lookup(None, None)

    with pytest.raises(service.OnboardingFailure) as exc:
        asyncio.run(service.onboard_assist_bare(_request()))
    assert exc.value.code == "DEFAULT_TEMPLATE_NOT_FOUND"
    wired["create_widget_config"].assert_not_called()


def _echo_template(kwargs) -> TemplateModel:
    """Build a TemplateModel from create/replace kwargs (the accessors echo
    the persisted row back)."""
    return TemplateModel(
        id=kwargs.get("template_id", "00000000-0000-0000-0000-000000000002"),
        reseller_id=kwargs.get("reseller_id", RESELLER_ID),
        merchant_id=kwargs.get("merchant_id", MERCHANT_ID),
        name=kwargs.get("name", "zodiac-buddy-assist"),
        flow=kwargs.get("flow", {"mode": "direct", "system_prompt": "x"}),
        expected_payload_schema=kwargs.get("expected_payload_schema", {}),
        expected_callback_response_schema=kwargs.get(
            "expected_callback_response_schema", {}
        ),
        configurations=kwargs.get("configurations"),
        secrets=kwargs.get("secrets", {}),
        is_active=kwargs.get("is_active", True),
        supported_channels=list(kwargs.get("supported_channels", ["chat"])),
    )
