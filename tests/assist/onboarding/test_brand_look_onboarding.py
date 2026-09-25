"""Onboarding saves the detected look as the widget's starting appearance,
never overwrites what the merchant set, and never fails because of it."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

from app.ai.voice.agents.breeze_buddy.assist.commerce import (
    vertical as commerce_vertical,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.models import (
    BrandColor,
    BrandLook,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.research.website import (
    WebsiteScrapingResult,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    FetchFailedError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding import service
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry as verticals
from app.schemas.breeze_buddy.widget_config import WidgetAppearance
from tests.assist.onboarding.test_stream import _default_template, _request, _widget

RED = "#c22126"
LOGO = "https://cdn.example/logo.png"


def _look(primary: Optional[str] = RED, logo: Optional[str] = LOGO) -> BrandLook:
    colors = [BrandColor(role="primary", hex=primary, source="page")] if primary else []
    return BrandLook(colors=colors, logo_url=logo, sources=["page"])


def test_starting_appearance_fills_only_the_two_known_keys():
    look = _look()
    look.colors.append(BrandColor(role="accent", hex="#1f6feb", source="page"))

    appearance = service.starting_appearance(look, {})

    assert appearance == {"primary_color": RED, "header_logo_url": LOGO}
    assert appearance is not None
    assert set(appearance) <= set(WidgetAppearance.model_fields)


def test_starting_appearance_never_overwrites_the_merchant():
    existing = {"primary_color": "#000000", "header_title": "Hi"}
    assert service.starting_appearance(_look(), existing) == {
        "primary_color": "#000000",
        "header_title": "Hi",
        "header_logo_url": LOGO,
    }
    full = {"primary_color": "#000000", "header_logo_url": "https://x.example/a.png"}
    assert service.starting_appearance(_look(), full) is None


def test_starting_appearance_drops_values_the_widget_schema_rejects():
    assert service.starting_appearance(_look(logo="http://cdn.example/a.png"), {}) == {
        "primary_color": RED
    }
    assert service.starting_appearance(_look(primary=None, logo=None), {}) is None
    assert service.starting_appearance(None, {"primary_color": RED}) is None


async def test_detect_look_never_raises(monkeypatch):
    monkeypatch.setattr(
        service, "probe_site", AsyncMock(side_effect=FetchFailedError("reset"))
    )
    assert (
        await service.detect_look("https://store.example", registry.resolve("generic"))
        is None
    )


def _run_first_onboarding(monkeypatch, detect) -> tuple[List[Any], Dict[str, Any]]:
    default = _default_template()
    saved: Dict[str, Any] = {}

    async def template_in_scope(reseller_id, merchant_id, name):
        if (
            merchant_id is None
            and name == commerce_vertical.DEFAULT_ASSIST_TEMPLATE_NAME
        ):
            return default
        return None

    async def create_template(**kwargs):
        return service.build_merchant_template(
            default_template=default,
            body=_request(),
            website_context="ctx",
            template_id=kwargs["template_id"],
            existing_template=None,
            adapter=registry.resolve("shopify"),
            vertical=verticals.resolve("commerce"),
        )

    async def create_widget(**kwargs):
        saved.update(kwargs)
        return _widget(kwargs["template_id"])

    monkeypatch.setattr(service, "detect_look", detect)
    monkeypatch.setattr(
        service, "get_widget_config_by_reseller_merchant", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service, "get_template_in_scope", template_in_scope)
    monkeypatch.setattr(
        service,
        "scrape_website",
        AsyncMock(
            return_value=WebsiteScrapingResult(
                text="ctx",
                provider="google",
                status="generated",
                provider_response={},
                url_context_metadata=[],
            )
        ),
    )
    monkeypatch.setattr(service, "create_template", create_template)
    monkeypatch.setattr(service, "create_widget_config", create_widget)
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())

    async def collect():
        return [event async for event in service.stream_assist_onboarding(_request())]

    return asyncio.run(collect()), saved


def test_first_onboarding_saves_the_detected_look(monkeypatch):
    events, saved = _run_first_onboarding(monkeypatch, AsyncMock(return_value=_look()))

    assert events[-1].event == "complete"
    assert saved["appearance"] == {"primary_color": RED, "header_logo_url": LOGO}
    brand = events[-1].data["personalization"]["brand"]
    assert brand["status"] == "applied"
    assert brand["primary_color"] == RED
    steps = [e.data["step"] for e in events if e.event == "progress"]
    assert "reading_brand" in steps


def test_a_slow_brand_read_leaves_the_widget_defaults(monkeypatch):
    monkeypatch.setattr(service, "_BRAND_TIMEOUT_SECONDS", 0.01)

    async def never(*_args):
        await asyncio.sleep(10)

    events, saved = _run_first_onboarding(monkeypatch, never)

    assert events[-1].event == "complete"
    assert saved["appearance"] is None
    assert events[-1].data["personalization"]["brand"] == {"status": "skipped"}


def test_reonboarding_keeps_the_merchants_colour(monkeypatch):
    default = _default_template()
    existing = service.build_merchant_template(
        default_template=default,
        body=_request(),
        website_context="Old",
        template_id="00000000-0000-0000-0000-000000000010",
        existing_template=None,
        adapter=registry.resolve("shopify"),
        vertical=verticals.resolve("commerce"),
    )
    widget = _widget(existing.id).model_copy(
        update={"appearance": {"primary_color": "#000000"}}
    )
    update = AsyncMock(return_value=widget)
    monkeypatch.setattr(service, "detect_look", AsyncMock(return_value=_look()))
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
                text="ctx",
                provider="google",
                status="generated",
                provider_response={},
                url_context_metadata=[],
            )
        ),
    )
    monkeypatch.setattr(service, "replace_template", AsyncMock(return_value=existing))
    monkeypatch.setattr(service, "update_widget_config", update)
    monkeypatch.setattr(service, "invalidate_template", AsyncMock())

    async def collect():
        return [event async for event in service.stream_assist_onboarding(_request())]

    events = asyncio.run(collect())

    assert events[-1].event == "complete"
    assert update.await_args is not None
    assert update.await_args.kwargs["appearance"] == {
        "primary_color": "#000000",
        "header_logo_url": LOGO,
    }
