"""The storefront brand block: only the validated permanent host is asked,
and only through the engine's guarded fetcher."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from app.ai.voice.agents.breeze_buddy.assist.engine.models import SiteProfile
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    FetchFailedError,
    FetchResult,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify import (
    adapter as shopify_adapter,
    brand,
)

BRAND_FILE = pathlib.Path(brand.__file__)


def _profile(shop_literal: Any = None) -> SiteProfile:
    literals = (
        {} if shop_literal is None else {shopify_adapter.SHOP_LITERAL: shop_literal}
    )
    return SiteProfile(
        url="https://hustleculture.co.in/",
        final_url="https://hustleculture.co.in/",
        status=200,
        inline_literals=literals,
    )


def _answer(monkeypatch, payload: Any = None, status: int = 200, error=None):
    calls: List[Dict[str, Any]] = []

    async def fake_fetch(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if error is not None:
            raise error
        return FetchResult(
            url=url, final_url=url, status=status, body=json.dumps(payload)
        )

    monkeypatch.setattr(brand, "fetch_page", fake_fetch)
    return calls


BRAND_PAYLOAD = {
    "data": {
        "shop": {
            "brand": {
                "colors": {
                    "primary": [{"background": "#C22126"}, None],
                    "secondary": [],
                },
                "logo": {"image": {"url": "https://cdn.example/logo.png"}},
                "squareLogo": None,
            }
        }
    }
}


async def test_asks_the_permanent_host_through_the_guarded_fetcher(monkeypatch):
    calls = _answer(monkeypatch, BRAND_PAYLOAD)

    look = await shopify_adapter.adapter.brand(_profile("9b1086-18.myshopify.com"))

    assert calls[0]["url"] == f"https://9b1086-18.myshopify.com{brand.API_PATH}"
    assert calls[0]["json_body"] == {"query": brand.QUERY}
    assert look is not None
    assert look.color("primary") == "#c22126"
    assert look.logo_url == "https://cdn.example/logo.png"


async def test_a_forged_or_missing_shop_literal_asks_nobody(monkeypatch):
    calls = _answer(monkeypatch, BRAND_PAYLOAD)
    for literal in (
        None,
        "169.254.169.254",
        "evil.example",
        "a.b.myshopify.com",
        "store.myshopify.com.evil.example",
    ):
        assert await shopify_adapter.adapter.brand(_profile(literal)) is None
    assert calls == []


async def test_an_empty_brand_block_or_failed_read_is_none(monkeypatch):
    empty = {"data": {"shop": {"brand": {"colors": {}, "logo": None}}}}
    _answer(monkeypatch, empty)
    assert await brand.brand_look("shop.myshopify.com") is None

    _answer(monkeypatch, {}, status=403)
    assert await brand.brand_look("shop.myshopify.com") is None

    _answer(monkeypatch, error=FetchFailedError("reset"))
    assert await brand.brand_look("shop.myshopify.com") is None


def test_brand_module_never_opens_its_own_http_client():
    source = BRAND_FILE.read_text()
    assert "aiohttp" not in source
    assert "httpx" not in source


def test_stock_colours_come_from_the_adapter():
    assert shopify_adapter.adapter.stock_colors() == brand.STOCK_COLORS
