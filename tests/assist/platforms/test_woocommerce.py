"""WooCommerce gateway: Store API records in, UCP out.

Records are trimmed from a live store's Store API. The fake mirrors its
query behavior: ``include`` omits variations unless ``type=variation``,
``parent`` takes a comma list, ``/products/{id}`` answers for any id.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from datetime import timedelta

import httpx
import pytest
from mcp.client.session_group import StreamableHttpParameters

from app.ai.voice.agents.breeze_buddy import mcp as mcp_mod
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import hooks
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
    CartLineP,
    ProductDetailP,
    ProductP,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.tool_meta import (
    _verify_cart_mutation,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce import (
    gateway,
    store_api,
)
from app.ai.voice.agents.breeze_buddy.mcp import local_gateway
from app.core.security import ssrf

HOST = "www.shopyvision.com"
_INR = {"currency_code": "INR", "currency_minor_unit": 2, "price_range": None}

SIMPLE = {
    "id": 347693,
    "parent": 0,
    "name": "Aiwa AIWP75P-GR 7.5kg Sentakki Semi-Automatic Washing Machine",
    "type": "simple",
    "permalink": f"https://{HOST}/product/aiwa-aiwp75p-gr/",
    "on_sale": True,
    "prices": {"price": "1059900", "regular_price": "1499000", **_INR},
    "images": [{"src": f"https://{HOST}/wp-content/uploads/aiwa.jpg", "alt": ""}],
    "variations": [],
    "is_purchasable": True,
    "is_in_stock": True,
    "variation": "",
}
VARIABLE = {
    **SIMPLE,
    "id": 1128586,
    "name": "Acer Wireless Mouse With 1600 DPI",
    "type": "variable",
    "permalink": f"https://{HOST}/product/acer-wireless-mouse-with-1600-dpi/",
    "prices": {"price": "69900", "regular_price": "149900", **_INR},
    "images": [{"src": f"https://{HOST}/wp-content/uploads/acer.jpg", "alt": ""}],
    "variations": [
        {"id": 1128598, "attributes": [{"name": "Color", "value": "blue"}]},
        {"id": 1128599, "attributes": [{"name": "Color", "value": "green"}]},
    ],
    "description": "<p>1600 DPI optical sensor.</p>",
}
BLUE = {
    **VARIABLE,
    "id": 1128598,
    "parent": 1128586,
    "type": "variation",
    "variations": [],
    "variation": "Color: Blue",
}
GREEN = {**BLUE, "id": 1128599, "variation": "Color: Green", "is_in_stock": False}
_PRODUCTS = [SIMPLE, VARIABLE]
_VARIATIONS = [BLUE, GREEN]


def _ids(value):
    return {int(i) for i in str(value).split(",")}


@pytest.fixture
def store(monkeypatch):
    """Fake Store API; records every request as ``(path, params)``."""
    calls = []

    async def fake_get(host, path, params=None):
        assert host == HOST
        calls.append((path, params))
        params = params or {}
        if path.startswith("/products/"):
            wanted = int(path.rsplit("/", 1)[1])
            found = [r for r in _PRODUCTS + _VARIATIONS if r["id"] == wanted]
            if not found:
                return httpx.Response(404, json={"code": "invalid_id"})
            return httpx.Response(200, json=copy.deepcopy(found[0]))
        pool = _VARIATIONS if params.get("type") == "variation" else _PRODUCTS
        if "parent" in params:
            pool = [r for r in pool if r["parent"] in _ids(params["parent"])]
        if "include" in params:
            pool = [r for r in pool if r["id"] in _ids(params["include"])]
        # Only the free-text search spans several pages here.
        pages = "3" if "search" in params else "1"
        return httpx.Response(200, json=pool, headers={"x-wp-totalpages": pages})

    monkeypatch.setattr(store_api, "get", fake_get)
    monkeypatch.setattr(gateway, "_STORE_CURRENCY", {})
    return calls


async def test_search_maps_to_ucp_products_with_variant_stock(store):
    out = await gateway.call_tool(
        HOST,
        "search_catalog",
        {
            "catalog": {
                "query": "mouse",
                "filters": {
                    "price": {"max": 100000},
                    "categories": ["Bluetooth Speakers", "Kitchen & Home"],
                },
                "pagination": {"limit": 5},
            }
        },
    )

    # The store's decimals are read once, for the price filter.
    assert store[0] == ("/products", {"per_page": 1})
    assert store[1] == (
        "/products",
        {
            "search": "mouse",
            "category": "bluetooth-speakers,kitchen-home",
            "max_price": 100000,
            "per_page": 5,
            "page": 1,
        },
    )
    # One extra call fetches every variable product's variations.
    assert store[2] == (
        "/products",
        {"type": "variation", "parent": "1128586", "per_page": 100},
    )
    assert out["pagination"] == {"has_next_page": True, "cursor": "2"}
    simple, variable = (ProductP.model_validate(p) for p in out["products"])
    # Minor units at exponent 2 — the template's scale rules divide by 100.
    assert simple.price.amount == 1059900
    assert simple.list_price is not None and simple.list_price.amount == 1499000
    assert simple.default_variant_id == "347693"
    assert simple.variants == []
    assert [(v.id, v.title, v.available) for v in variable.variants] == [
        ("1128598", "Blue", True),
        ("1128599", "Green", False),
    ]
    # One available variation: add-to-cart can default to it, as on Shopify.
    assert variable.default_variant_id == "1128598"


async def test_price_filter_uses_the_store_decimals(store, monkeypatch):
    monkeypatch.setattr(gateway, "_STORE_CURRENCY", {HOST: (0, "INR")})
    await gateway.call_tool(
        HOST, "search_catalog", {"catalog": {"filters": {"price": {"max": 100000}}}}
    )

    # UCP 100000 = Rs 1,000; a 0-decimal store reads that as 1000.
    assert store[0][1]["max_price"] == 1000


async def test_page_size_is_clamped_to_the_store_api_limit(store):
    await gateway.call_tool(
        HOST, "search_catalog", {"catalog": {"pagination": {"limit": 500}}}
    )

    assert store[0][1]["per_page"] == 100


async def test_empty_cart_uses_the_store_currency(store, monkeypatch):
    monkeypatch.setattr(gateway, "_STORE_CURRENCY", {HOST: (2, "USD")})
    cart = await gateway.call_tool(HOST, "get_cart", {"id": ""})

    assert cart["totals"][-1] == {"type": "total", "amount": 0, "currency": "USD"}


async def test_get_product_lists_variations_with_stock(store):
    out = await gateway.call_tool(HOST, "get_product", {"catalog": {"id": "1128586"}})

    detail = ProductDetailP.model_validate(out["product"])
    assert detail.description == "1600 DPI optical sensor."
    assert [(v.id, v.title, v.available) for v in detail.variants] == [
        ("1128598", "Blue", True),
        ("1128599", "Green", False),
    ]


async def test_variations_past_the_first_page_are_fetched(monkeypatch):
    async def paged_get(host, path, params=None):
        params = params or {}
        if params.get("type") != "variation":
            return httpx.Response(200, json=[VARIABLE])
        page = [BLUE] if params.get("page", 1) == 1 else [GREEN]
        return httpx.Response(200, json=page, headers={"x-wp-totalpages": "2"})

    monkeypatch.setattr(store_api, "get", paged_get)
    out = await gateway.call_tool(HOST, "search_catalog", {"catalog": {"query": "x"}})

    assert [v["id"] for v in out["products"][0]["variants"]] == ["1128598", "1128599"]


async def test_lookup_resolves_a_variation_id_to_its_parent(store):
    out = await gateway.call_tool(
        HOST, "lookup_catalog", {"catalog": {"ids": ["347693", "1128598"]}}
    )

    assert [p["id"] for p in out["products"]] == ["347693", "1128586"]
    assert [v["id"] for v in out["products"][1]["variants"]] == ["1128598", "1128599"]


async def test_cart_lives_in_its_id_and_checks_out_by_link(store):
    args = {
        "cart": {
            "line_items": [
                {"item": {"id": "347693"}, "quantity": 1},
                {"item": {"id": "1128598"}, "quantity": 2},
            ]
        }
    }
    cart = await gateway.call_tool(HOST, "create_cart", args)

    assert cart["id"] == "347693:1,1128598:2"
    assert cart["continue_url"] == (
        f"https://{HOST}/checkout-link/?products=347693:1,1128598:2"
    )
    assert cart["totals"][-1] == {"type": "total", "amount": 1199700, "currency": "INR"}
    assert _verify_cart_mutation(args, cart) is None
    line = CartLineP.model_validate(cart["line_items"][1])
    assert (line.variant_id, line.variant_title, line.qty) == ("1128598", "Blue", 2)

    again = await gateway.call_tool(HOST, "get_cart", {"id": cart["id"]})
    assert again["line_items"] == cart["line_items"]


async def test_update_cart_replaces_the_line_set(store):
    args = {
        "id": "347693:1,1128598:2",
        "cart": {
            "line_items": [
                {"item": {"id": "347693"}, "quantity": 0},
                {"item": {"id": "1128598"}, "quantity": 3},
            ]
        },
    }
    cart = await gateway.call_tool(HOST, "update_cart", args)

    assert cart["id"] == "1128598:3"
    assert _verify_cart_mutation(args, cart) is None


async def test_unbuyable_lines_are_dropped_with_a_message(store):
    # A variable parent needs a variation; GREEN is out of stock.
    args = {
        "cart": {
            "line_items": [
                {"item": {"id": "1128586"}, "quantity": 1},
                {"item": {"id": "1128599"}, "quantity": 1},
            ]
        }
    }
    cart = await gateway.call_tool(HOST, "create_cart", args)

    assert cart["id"] == "" and cart["line_items"] == []
    assert "continue_url" not in cart
    assert [m["type"] for m in cart["messages"]] == ["warning", "warning"]
    assert _verify_cart_mutation(args, cart) is not None


async def test_shopify_quirks_leave_woocommerce_data_alone(store):
    out = await gateway.call_tool(HOST, "search_catalog", {"catalog": {"query": "x"}})
    variants = out["products"][1]["variants"]
    assert hooks.normalize_variants(copy.deepcopy(variants)) == variants
    assert hooks.repair_description("Line one.\nLine two.") == "Line one.\nLine two."


def _handler(url: str, tool: str):
    return mcp_mod._create_direct_http_tool_handler(
        StreamableHttpParameters(url=url, headers={}, timeout=timedelta(seconds=5)),
        tool,
    )


async def test_direct_handler_reaches_the_local_gateway(store):
    out = await _handler(f"local://woocommerce/{HOST}", "lookup_catalog")(
        {"catalog": {"ids": ["347693"]}}, None
    )

    assert out["status"] == "success"
    assert [p["id"] for p in json.loads(out["data"])["products"]] == ["347693"]


async def test_gateway_loads_its_platform_package_on_first_call(store, monkeypatch):
    # Agent-mode voice never imports the commerce flavor, so nothing has
    # registered the gateway yet when the first tool call arrives.
    monkeypatch.setattr(local_gateway, "_GATEWAYS", {})
    monkeypatch.setattr(local_gateway, "_IMPORTED", set())
    monkeypatch.delitem(
        sys.modules, "app.ai.voice.agents.breeze_buddy.assist.platforms.woocommerce"
    )

    out = await _handler(f"local://woocommerce/{HOST}", "lookup_catalog")(
        {"catalog": {"ids": ["347693"]}}, None
    )

    assert out["status"] == "success"
    assert "woocommerce" in local_gateway._GATEWAYS


async def test_unknown_gateway_is_an_upstream_404(store):
    out = await _handler("local://nosuchplatform/x", "search_catalog")({}, None)

    assert out["status"] == "error" and out["status_code"] == 404


async def test_bad_host_is_a_tool_error_not_a_request(store):
    out = await _handler("local://woocommerce/evil.example%2Fx", "search_catalog")(
        {"catalog": {"query": "x"}}, None
    )

    assert out["status"] == "success"  # isError rides in data, as for remote UCP
    assert "not a host name" in out["data"]
    assert store == []


async def test_store_api_refuses_non_public_hosts(monkeypatch):
    monkeypatch.setattr(ssrf, "_ALLOW_PRIVATE_EGRESS", False)
    with pytest.raises(ssrf.SSRFError):
        await store_api.get("169.254.169.254", "/products")


async def test_local_gateway_applies_the_client_timeout(monkeypatch):
    async def slow(_target, _tool, _args):
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(local_gateway, "_GATEWAYS", {"slowtest": slow})
    out = await mcp_mod._create_direct_http_tool_handler(
        StreamableHttpParameters(
            url="local://slowtest/x", headers={}, timeout=timedelta(seconds=0.2)
        ),
        "search_catalog",
    )({}, None)

    assert out["status"] == "error" and out["status_code"] == 504
