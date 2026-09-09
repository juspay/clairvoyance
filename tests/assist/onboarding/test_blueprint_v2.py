"""The v2 Assist blueprint contract (plan §3.1).

The reseller blueprint IS the fleet skeleton: a Shopify build must land on the
byte-identical operating core the 26 live stores run, a generic build must
drop every Shopify-only section, ``{{shop_domain}}`` resolves everywhere, the
template is named ``<store>-assist``, re-onboarding merges widget origins,
a failed site read only proceeds when the caller allows it, and a merchant
may run the stream for their own store.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.assist.engine.prompt_core import (
    core_hash,
    shared_core,
    split_prompt,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding import service
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.routers.breeze_buddy.assist.onboarding import _ONBOARDING_ROLES
from app.schemas import UserRole
from app.schemas.breeze_buddy.assist.onboarding import AssistOnboardingStreamRequest
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "buddy-assist-default.v2.json"
)
BLUEPRINT_ID = "00000000-0000-0000-0000-00000000b1e0"


def _blueprint(reseller_id: str = "BB_SHOPIFY") -> TemplateModel:
    body = json.loads(FIXTURE.read_text())
    return TemplateModel(
        id=BLUEPRINT_ID,
        reseller_id=reseller_id,
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


def _widget(template_id: str, origins=None) -> WidgetConfigResponse:
    return WidgetConfigResponse(
        id="00000000-0000-0000-0000-000000000020",
        reseller_id="BB_SHOPIFY",
        merchant_id="9b1086-18.myshopify.com",
        public_widget_key="public-key",
        template_id=template_id,
        allowed_origins=origins or ["https://hustleculture.co.in"],
        active=True,
    )


def _build(is_shopify: bool = True) -> TemplateModel:
    return service.build_merchant_template(
        default_template=_blueprint(),
        body=_request(is_shopify=is_shopify),
        website_context="Sells premium sneakers.",
        template_id="00000000-0000-0000-0000-000000000002",
        existing_template=None,
    )


def test_fixture_is_the_fleet_skeleton() -> None:
    blueprint = _blueprint()
    service._validate_default_template(blueprint)
    assert service.blueprint_shape_warnings(blueprint) == []
    prompt = blueprint.flow["system_prompt"]
    assert prompt.count(service.BRAND_IDENTITY_MARKER) == 1
    assert len(service._shopify_sections(prompt)) == 2
    assert blueprint.configurations is not None
    assert service.SHOP_DOMAIN_PLACEHOLDER in json.dumps(
        blueprint.configurations.model_dump()
    )


def test_shopify_build_lands_on_the_fleet_core() -> None:
    blueprint = _blueprint()
    built = _build(is_shopify=True)
    prompt = built.flow["system_prompt"]

    # markers gone, brand block filled, Shopify sections kept in place
    for marker in (
        service.BRAND_IDENTITY_MARKER,
        service.SHOPIFY_OPERATING_START_MARKER,
        service.SHOPIFY_OPERATING_END_MARKER,
        service.SHOP_DOMAIN_PLACEHOLDER,
    ):
        assert marker not in prompt
    brand, _ = split_prompt(prompt)
    assert "{shop_url}" in brand and "Hustle Culture" in brand
    assert "### Tools — Universal Commerce Protocol" in prompt
    assert "### Cart-cookie sync (Shopify storefront)" in prompt

    # the operating core is byte-for-byte the blueprint's (slots normalized)
    skeleton = blueprint.flow["system_prompt"]
    skeleton = skeleton.replace(service.SHOPIFY_OPERATING_START_MARKER, "").replace(
        service.SHOPIFY_OPERATING_END_MARKER, ""
    )
    assert shared_core(prompt) == shared_core(skeleton)
    assert core_hash(prompt) == core_hash(skeleton)

    # {{shop_domain}} resolved everywhere in the config
    assert built.configurations is not None
    config = built.configurations.model_dump(mode="json", exclude_none=True)
    blob = json.dumps(config)
    assert service.SHOP_DOMAIN_PLACEHOLDER not in blob
    assert (
        config["ui_intents"]["urls"]["checkout_page"]
        == "https://hustleculture.co.in/cart"
    )
    assert (
        "https://hustleculture.co.in/pages/contact"
        in config["render_ui"]["trusted_link_urls"]
    )
    servers = config["mcp"]["servers"]
    assert len(servers) == 1 and servers[0]["name"] in service.SHOPIFY_MCP_SERVER_NAMES
    assert (
        "https://hustleculture.co.in/cart"
        in servers[0]["tool_ui_instructions"]["get_cart"]["instructions"]
    )
    assert (
        'link={"label": "Contact us", "url": "https://hustleculture.co.in/pages/contact"}'
        in prompt
    )

    assert built.name == "hustle-culture-assist"
    assert built.supported_channels == ["chat"]
    assert built.flow["functions"] == []
    assert service.blueprint_shape_warnings(built) == []


def test_generic_build_drops_every_shopify_section() -> None:
    built = _build(is_shopify=False)
    prompt = built.flow["system_prompt"]
    for gone in (
        "### Tools — Universal Commerce Protocol",
        "### Cart-cookie sync (Shopify storefront)",
        "### Review-and-checkout flow",
        "### Store policy / refunds / shipping / FAQ",
        service.SHOPIFY_OPERATING_START_MARKER,
        service.SHOPIFY_OPERATING_END_MARKER,
    ):
        assert gone not in prompt
    for kept in (
        "### Action clicks arrive as natural-language messages",
        "### Contacts and external links are buttons, never plain text",
        "### Guided shopping — drive to intent",
        "### Sizing and selection help — solve it here first",
        "{{ui_primitives_section}}",
    ):
        assert kept in prompt
    assert built.configurations is not None
    assert built.configurations.mcp is None
    assert built.configurations.state_reducers == []
    assert built.configurations.tool_arg_injection == []


@pytest.mark.parametrize(
    "prompt",
    [
        "{{brand_identity_section}}\n## Operating principles\nno sections",
        "{{brand_identity_section}}\n## Operating principles\n{{#shopify_operating_section}}open",
        "{{brand_identity_section}}\n## Operating principles\n{{#shopify_operating_section}}a{{#shopify_operating_section}}b{{/shopify_operating_section}}",
        "{{brand_identity_section}}\n## Operating principles\n{{#shopify_operating_section}}a{{/shopify_operating_section}} stray {{/shopify_operating_section}}",
    ],
)
def test_marker_validation_rejects_bad_shapes(prompt: str) -> None:
    blueprint = _blueprint()
    blueprint.flow["system_prompt"] = prompt
    with pytest.raises(service.OnboardingFailure) as failure:
        service._validate_default_template(blueprint)
    assert failure.value.code == "DEFAULT_TEMPLATE_INVALID"


def test_old_shape_blueprint_only_warns() -> None:
    old = _blueprint()
    old.flow["functions"] = [{"name": "get_order_status"}]
    old.supported_channels = ["chat", "voice"]
    assert old.configurations is not None
    assert old.configurations.llm_configurations is not None
    old.configurations.llm_configurations.model = "gemini-2.5-flash"
    warnings = service.blueprint_shape_warnings(old)
    assert len(warnings) == 3
    assert any("functions" in w for w in warnings)
    assert any("supported_channels" in w for w in warnings)
    assert any("gemini-2.5-flash" in w for w in warnings)
    service._validate_default_template(old)  # tolerated until the data rows are v2


def test_template_name_is_store_assist() -> None:
    assert service._template_name("Hustle Culture") == "hustle-culture-assist"
    assert service._template_name("  ") == "store-assist"


def test_update_widget_merges_origins(monkeypatch) -> None:
    existing = _widget(
        "00000000-0000-0000-0000-000000000002",
        origins=["https://9b1086-18.myshopify.com", "https://www.hustleculture.co.in"],
    )
    captured = {}

    async def update_widget_config(widget_id, **kwargs):
        captured.update(kwargs)
        return existing

    monkeypatch.setattr(service, "update_widget_config", update_widget_config)
    asyncio.run(
        service._update_widget(
            existing,
            _request(
                allowed_origins=[
                    "https://9b1086-18.myshopify.com",
                    "https://hustleculture.co.in",
                ]
            ),
            existing.template_id,
        )
    )
    assert captured["allowed_origins"] == [
        "https://9b1086-18.myshopify.com",
        "https://www.hustleculture.co.in",
        "https://hustleculture.co.in",
    ]


def _wire_first_onboarding(monkeypatch, scrape) -> None:
    blueprint = _blueprint()

    async def template_in_scope(reseller_id, merchant_id, name):
        if merchant_id is None and name == service.DEFAULT_ASSIST_TEMPLATE_NAME:
            return blueprint
        return None

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

    async def create_widget_mock(**kwargs):
        return _widget(kwargs["template_id"])

    monkeypatch.setattr(
        service, "get_widget_config_by_reseller_merchant", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service, "get_template_in_scope", template_in_scope)
    monkeypatch.setattr(service, "scrape_website", scrape)
    monkeypatch.setattr(service, "create_template", create_template_mock)
    monkeypatch.setattr(service, "create_widget_config", create_widget_mock)
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())


def _run_stream(body: AssistOnboardingStreamRequest):
    async def collect():
        return [event async for event in service.stream_assist_onboarding(body)]

    return asyncio.run(collect())


def test_failed_site_read_aborts_unless_allowed(monkeypatch) -> None:
    _wire_first_onboarding(
        monkeypatch, AsyncMock(side_effect=WebsiteScrapingUpstreamError("blocked"))
    )

    events = _run_stream(_request())
    assert events[-1].event == "error"
    assert events[-1].data["code"] == "SCRAPING_UPSTREAM_FAILED"

    events = _run_stream(_request(allow_unpersonalized=True))
    assert events[-1].event == "complete"
    assert events[-1].data["personalization"]["status"] == "skipped_scrape_failed"
    scrape_done = [
        e
        for e in events
        if e.event == "progress" and e.data["step"] == "scraping_website"
    ][-1]
    assert scrape_done.data["personalized"] is False
    assert events[-1].data["template_name"] == "hustle-culture-assist"


def test_merchant_may_onboard_their_own_store() -> None:
    assert UserRole.MERCHANT in _ONBOARDING_ROLES
    assert UserRole.USER not in _ONBOARDING_ROLES


def test_platform_alias_must_agree_with_is_shopify() -> None:
    assert _request(platform="shopify").is_shopify is True
    with pytest.raises(ValidationError):
        _request(platform="web", is_shopify=True)
