"""WooCommerce on our MCP endpoint: Store API records in, the engine's UCP
product and cart shapes out.

Records are trimmed from a live store's Store API. The fake mirrors its
query behavior: ``include`` omits variations unless ``type=variation``,
``parent`` takes a comma list, ``/products/{id}`` answers for any id.
"""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, cast

import httpx
import pytest
from fastapi import FastAPI

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
    cart,
    catalog,
    store_api,
    tools,
)
from app.ai.voice.agents.breeze_buddy.chat import flavors
from app.ai.voice.agents.breeze_buddy.mcp import in_process, server
from app.ai.voice.agents.breeze_buddy.template.types import (
    McpConfig,
    McpServerConfig,
)
from app.api.routers import mcp as mcp_route
from app.core.security import ssrf

HOST = "www.shopyvision.com"
STORE_API = f"https://{HOST}/wp-json/wc/store/v1"
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
    # The live store limits this variation to 3 per order.
    "add_to_cart": {"maximum": 3},
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
        assert host == STORE_API
        calls.append((path, params))
        params = params or {}
        if path.startswith("/products/"):
            wanted = int(path.rsplit("/", 1)[1])
            found = [r for r in _PRODUCTS + _VARIATIONS if r["id"] == wanted]
            if not found:
                return httpx.Response(
                    404, json={"code": "woocommerce_rest_product_invalid_id"}
                )
            return httpx.Response(200, json=copy.deepcopy(found[0]))
        pool = _VARIATIONS if params.get("type") == "variation" else _PRODUCTS
        if "parent" in params:
            pool = [r for r in pool if r["parent"] in _ids(params["parent"])]
        if "include" in params:
            pool = [r for r in pool if r["id"] in _ids(params["include"])]
        if "_fields" in params:
            # Answer only the requested fields, as the store does: every test
            # then proves those fields are all the code reads.
            fields = params["_fields"].split(",")
            pool = [{k: v for k, v in r.items() if k in fields} for r in pool]
        # Only the free-text search spans several pages here.
        pages = "3" if "search" in params else "1"
        return httpx.Response(200, json=pool, headers={"x-wp-totalpages": pages})

    monkeypatch.setattr(store_api, "get", fake_get)
    monkeypatch.setattr(catalog, "_STORE_CURRENCY", {})
    return calls


async def test_search_maps_to_ucp_products_with_variant_stock(store):
    out = await tools.call_tool(
        STORE_API,
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
            "_fields": catalog._PRODUCT_FIELDS,
        },
    )
    # One extra call fetches every variable product's variations.
    assert store[2] == (
        "/products",
        {
            "type": "variation",
            "parent": "1128586",
            "per_page": 100,
            "_fields": catalog._VARIATION_FIELDS,
        },
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
    monkeypatch.setattr(catalog, "_STORE_CURRENCY", {STORE_API: (0, "INR")})
    await tools.call_tool(
        STORE_API,
        "search_catalog",
        {"catalog": {"filters": {"price": {"max": 100000}}}},
    )

    # UCP 100000 = Rs 1,000; a 0-decimal store reads that as 1000.
    assert store[0][1]["max_price"] == 1000


async def test_page_size_is_clamped_to_the_store_api_limit(store):
    await tools.call_tool(
        STORE_API, "search_catalog", {"catalog": {"pagination": {"limit": 500}}}
    )

    assert store[0][1]["per_page"] == 100


async def test_empty_cart_uses_the_store_currency(store, monkeypatch):
    monkeypatch.setattr(catalog, "_STORE_CURRENCY", {STORE_API: (2, "USD")})
    cart = await tools.call_tool(STORE_API, "get_cart", {"id": ""})

    assert cart["totals"][-1] == {"type": "total", "amount": 0, "currency": "USD"}


async def test_get_product_lists_variations_with_stock(store):
    out = await tools.call_tool(
        STORE_API, "get_product", {"catalog": {"id": "1128586"}}
    )

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
        # Answer only the requested fields, as the store does: the variants
        # below prove those fields are all the code reads.
        fields = params["_fields"].split(",")
        page = [{k: v for k, v in r.items() if k in fields} for r in page]
        return httpx.Response(200, json=page, headers={"x-wp-totalpages": "2"})

    monkeypatch.setattr(store_api, "get", paged_get)
    out = await tools.call_tool(
        STORE_API, "search_catalog", {"catalog": {"query": "x"}}
    )

    assert [v["id"] for v in out["products"][0]["variants"]] == ["1128598", "1128599"]


async def test_lookup_resolves_a_variation_id_to_its_parent(store):
    out = await tools.call_tool(
        STORE_API, "lookup_catalog", {"catalog": {"ids": ["347693", "1128598"]}}
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
    cart = await tools.call_tool(STORE_API, "create_cart", args)

    assert cart["id"] == "347693:1,1128598:2"
    assert cart["continue_url"] == (
        f"https://{HOST}/checkout-link/?products=347693:1,1128598:2"
    )
    assert cart["totals"][-1] == {"type": "total", "amount": 1199700, "currency": "INR"}
    assert _verify_cart_mutation(args, cart) is None
    line = CartLineP.model_validate(cart["line_items"][1])
    assert (line.variant_id, line.variant_title, line.qty) == ("1128598", "Blue", 2)

    again = await tools.call_tool(STORE_API, "get_cart", {"id": cart["id"]})
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
    cart = await tools.call_tool(STORE_API, "update_cart", args)

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
    cart = await tools.call_tool(STORE_API, "create_cart", args)

    assert cart["id"] == "" and cart["line_items"] == []
    assert "continue_url" not in cart
    assert [m["content"] for m in cart["messages"]] == [
        "'Acer Wireless Mouse With 1600 DPI' comes in options. Choose one first.",
        "'Acer Wireless Mouse With 1600 DPI' can't be added right now.",
    ]
    assert _verify_cart_mutation(args, cart) is not None


async def test_a_cart_read_keeps_a_line_that_is_out_of_stock(store):
    # GREEN (1128599) is out of stock. A read must not drop it: the engine
    # sends every line back on the next edit, so a dropped line is deleted.
    cart = await tools.call_tool(STORE_API, "get_cart", {"id": "347693:1,1128599:2"})

    assert cart["id"] == "347693:1,1128599:2"
    assert [m["content"] for m in cart["messages"]] == [
        "'Acer Wireless Mouse With 1600 DPI' is out of stock right now. "
        "It stays in your cart."
    ]


async def test_an_edit_keeps_an_out_of_stock_line_until_it_is_removed(store):
    def lines(*pairs):
        return [{"item": {"id": i}, "quantity": q} for i, q in pairs]

    add = {
        "id": "347693:1,1128599:2",
        "cart": {"line_items": lines(("347693", 1), ("1128599", 2), ("1128598", 1))},
    }
    cart = await tools.call_tool(STORE_API, "update_cart", add)
    assert cart["id"] == "347693:1,1128599:2,1128598:1"

    remove = {
        "id": cart["id"],
        "cart": {"line_items": lines(("347693", 1), ("1128599", 0), ("1128598", 1))},
    }
    cart = await tools.call_tool(STORE_API, "update_cart", remove)
    assert cart["id"] == "347693:1,1128598:1"


async def test_the_store_limit_cuts_only_what_the_cart_did_not_hold(store):
    # BLUE's limit is 3. A cart that already holds 5 keeps 5 on a read and
    # when asked for more; lowering it is always allowed.
    cart = await tools.call_tool(STORE_API, "get_cart", {"id": "1128598:5"})
    assert cart["id"] == "1128598:5"

    more = {
        "id": "1128598:5",
        "cart": {"line_items": [{"item": {"id": "1128598"}, "quantity": 7}]},
    }
    assert (await tools.call_tool(STORE_API, "update_cart", more))["id"] == "1128598:5"

    fewer = {
        "id": "1128598:5",
        "cart": {"line_items": [{"item": {"id": "1128598"}, "quantity": 2}]},
    }
    assert (await tools.call_tool(STORE_API, "update_cart", fewer))["id"] == "1128598:2"


async def test_quantity_is_capped_at_the_store_limit(store):
    args = {"cart": {"line_items": [{"item": {"id": "1128598"}, "quantity": 5}]}}
    cart = await tools.call_tool(STORE_API, "create_cart", args)

    assert cart["id"] == "1128598:3"
    assert cart["line_items"][0]["quantity"] == 3
    assert [m["content"] for m in cart["messages"]] == [
        "Only 3 of 'Acer Wireless Mouse With 1600 DPI' can be bought at once."
    ]
    # The engine's cart check tells the model the cap was applied.
    assert "requested quantity 5" in (_verify_cart_mutation(args, cart) or "")


@contextmanager
def _connectors(*names: str):
    """The turn's template connectors, as ChatAgent and the voice agent set them."""
    token = flavors._ACTIVE_CONNECTORS.set(names)
    try:
        yield
    finally:
        flavors._ACTIVE_CONNECTORS.reset(token)


def test_shopify_quirks_leave_woocommerce_data_alone():
    # Data both Shopify fixes change: a lone "Default Title" variant, and a
    # description whose paragraph break was lost.
    variants = [{"id": "1", "title": "Default Title"}]
    text = "Keeps you warmFrom early mornings"

    with _connectors("woocommerce"):
        assert hooks.normalize_variants(copy.deepcopy(variants)) == variants
        assert hooks.repair_description(text) == text
    # The same data under Shopify is changed, so the test can fail.
    with _connectors("shopify"):
        assert hooks.normalize_variants(copy.deepcopy(variants)) == []
        assert hooks.repair_description(text) != text


# --- store failures: every one is a ToolError the model can read ---


def _store_answers(monkeypatch, response):
    async def answer(base, path, params=None):
        return response

    monkeypatch.setattr(store_api, "get", answer)


async def _tool_error(tool: str, args: Dict[str, Any]) -> str:
    with pytest.raises(server.ToolError) as e:
        await tools.call_tool(STORE_API, tool, args)
    return str(e.value)


@pytest.mark.parametrize(
    "tool, args",
    [
        ("search_catalog", {"catalog": {"query": "x"}}),
        ("lookup_catalog", {"catalog": {"ids": ["1"]}}),
        ("get_cart", {"id": "347693:1"}),
        ("create_cart", {"cart": {"line_items": [{"item": {"id": "1"}}]}}),
    ],
)
async def test_a_missing_store_api_is_an_error_for_every_tool(monkeypatch, tool, args):
    # rest_no_route: the Store API is off, or the host is not WooCommerce. A
    # cart read must fail, never come back empty (add-to-cart would then
    # replace the shopper's cart).
    _store_answers(monkeypatch, httpx.Response(404, json={"code": "rest_no_route"}))

    assert "no WooCommerce Store API" in await _tool_error(tool, args)


async def test_an_unknown_product_is_not_found(store):
    assert "999 was not found" in await _tool_error(
        "get_product", {"catalog": {"id": "999"}}
    )


@pytest.mark.parametrize(
    "response, reason",
    [
        (httpx.Response(503), "HTTP 503"),
        (
            httpx.Response(301, headers={"location": "https://shopyvision.com/"}),
            "redirects to https://shopyvision.com/",
        ),
        (
            httpx.Response(200, text="<html>not a store</html>"),
            "did not answer with JSON",
        ),
    ],
)
async def test_store_failures_are_named(monkeypatch, response, reason):
    _store_answers(monkeypatch, response)

    assert reason in await _tool_error("search_catalog", {"catalog": {"query": "x"}})


async def test_a_refused_host_does_not_show_its_address(monkeypatch):
    async def refused(base, path, params=None):
        raise ssrf.SSRFError("www.shopyvision.com resolves to 10.0.0.7")

    monkeypatch.setattr(store_api, "get", refused)
    message = await _tool_error("search_catalog", {"catalog": {"query": "x"}})

    assert "not reachable" in message and "10.0.0.7" not in message


@pytest.mark.parametrize(
    "args",
    [
        {"catalog": {"query": "x", "pagination": {"cursor": "next"}}},
        {"catalog": "neckbands"},
    ],
)
async def test_a_bad_argument_from_the_model_is_named(store, args):
    assert "Invalid tool arguments" in await _tool_error("search_catalog", args)


async def test_an_unknown_tool_is_named(store):
    assert "Unknown tool 'track_order'" in await _tool_error("track_order", {})


# --- bounds on what the store can make us do ---


def test_store_values_are_kept_in_bounds():
    # The decimals are an exponent from the store; links must be https.
    assert catalog.money(100, {"currency_minor_unit": 99})["amount"] == 1
    assert catalog.money(100, {"currency_minor_unit": -5})["amount"] == 10000
    assert catalog._https("javascript:alert(1)") is None
    assert catalog._https("http://x.example/p") is None
    assert catalog._https("https://x.example/p") == "https://x.example/p"


async def test_page_size_and_page_are_clamped(store):
    await tools.call_tool(
        STORE_API,
        "search_catalog",
        {"catalog": {"pagination": {"cursor": "-3", "limit": 500}}},
    )

    assert store[0][1]["page"] == 1 and store[0][1]["per_page"] == 100


async def test_currency_is_not_cached_from_an_empty_catalog(monkeypatch):
    catalogs = [[], [SIMPLE]]

    async def fake_get(host, path, params=None):
        return httpx.Response(200, json=catalogs.pop(0) if catalogs else [SIMPLE])

    monkeypatch.setattr(store_api, "get", fake_get)
    monkeypatch.setattr(catalog, "_STORE_CURRENCY", {})

    assert await catalog.store_currency(STORE_API) == (2, "")
    assert catalog._STORE_CURRENCY == {}
    assert await catalog.store_currency(STORE_API) == (2, "INR")
    assert catalog._STORE_CURRENCY == {STORE_API: (2, "INR")}


async def test_variation_pages_are_fetched_a_few_at_a_time(monkeypatch):
    running, peak = 0, 0

    async def paged_get(host, path, params=None):
        nonlocal running, peak
        params = params or {}
        if params.get("type") != "variation":
            return httpx.Response(200, json=[VARIABLE])
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return httpx.Response(200, json=[BLUE], headers={"x-wp-totalpages": "12"})

    monkeypatch.setattr(store_api, "get", paged_get)
    await tools.call_tool(STORE_API, "search_catalog", {"catalog": {"query": "x"}})

    assert 1 < peak <= catalog._PARALLEL_REQUESTS


async def test_the_stores_page_count_cannot_start_unbounded_work(monkeypatch):
    pages_asked = []

    async def paged_get(base, path, params=None):
        params = params or {}
        if params.get("type") != "variation":
            return httpx.Response(200, json=[VARIABLE])
        pages_asked.append(params.get("page", 1))
        return httpx.Response(200, json=[BLUE], headers={"x-wp-totalpages": "10000000"})

    monkeypatch.setattr(store_api, "get", paged_get)
    await tools.call_tool(STORE_API, "search_catalog", {"catalog": {"query": "x"}})

    assert len(pages_asked) == catalog._MAX_VARIATION_PAGES


async def test_media_is_capped_and_never_copies_the_name(monkeypatch):
    # A missing alt stays missing: the widget shows the product title itself,
    # and a copied name multiplies the reply by the number of images.
    images = [{"src": f"https://{HOST}/{i}.jpg", "alt": ""} for i in range(30)]

    async def fake_get(host, path, params=None):
        return httpx.Response(200, json=[{**SIMPLE, "images": images}])

    monkeypatch.setattr(store_api, "get", fake_get)
    out = await tools.call_tool(STORE_API, "search_catalog", {"catalog": {}})

    media = out["products"][0]["media"]
    assert len(media) == catalog._MAX_IMAGES
    assert all(m["alt_text"] is None for m in media)


async def test_an_option_page_longer_than_a_store_page_is_refused(monkeypatch):
    async def fake_get(host, path, params=None):
        params = params or {}
        if params.get("type") != "variation":
            return httpx.Response(200, json=[VARIABLE])
        many = [{**BLUE, "id": 2_000_000 + i} for i in range(101)]
        return httpx.Response(200, json=many)

    monkeypatch.setattr(store_api, "get", fake_get)
    with pytest.raises(server.ToolError, match="invalid list of product options"):
        await tools.call_tool(STORE_API, "search_catalog", {"catalog": {}})


async def test_a_cart_id_must_fit_the_widgets_limit(monkeypatch):
    # 40 lines of 7-digit ids at quantity 9999 is 520 characters: over the
    # widget's 512-character cart_id, so the add is refused, not truncated.
    async def fake_get(host, path, params=None):
        ids = (params or {}).get("include", "").split(",")
        return httpx.Response(200, json=[{**SIMPLE, "id": int(i)} for i in ids])

    monkeypatch.setattr(store_api, "get", fake_get)
    lines = [{"item": {"id": str(1_000_000 + n)}, "quantity": 9999} for n in range(40)]
    with pytest.raises(server.ToolError, match="cart is full"):
        await tools.call_tool(STORE_API, "create_cart", {"cart": {"line_items": lines}})


async def test_a_cart_has_a_line_limit(monkeypatch):
    calls = []

    async def counting_get(base, path, params=None):
        calls.append(path)
        return httpx.Response(200, json=SIMPLE)

    monkeypatch.setattr(store_api, "get", counting_get)
    many = ",".join(f"{n}:1" for n in range(1, cart._MAX_CART_LINES + 2))

    assert "at most 40" in await _tool_error("get_cart", {"id": many})
    assert calls == []


async def test_a_cart_is_read_in_two_requests(store):
    many = "347693:1,1128598:2," + ",".join(f"{n}:1" for n in range(1, 11))
    out = await tools.call_tool(STORE_API, "get_cart", {"id": many})

    assert len(store) == 2  # simple products, then variations
    # Hidden products stay buyable, so a cart read must still see them.
    assert all(params["catalog_visibility"] == "any" for _, params in store)
    assert [line["id"] for line in out["line_items"]] == ["347693", "1128598"]
    assert len(out["messages"]) == 10  # the unknown ids, one warning each


async def test_checkout_link_keeps_a_subdirectory_install(monkeypatch):
    async def fake_get(base, path, params=None):
        variations = (params or {}).get("type") == "variation"
        return httpx.Response(200, json=[] if variations else [SIMPLE])

    monkeypatch.setattr(store_api, "get", fake_get)
    base = f"https://{HOST}/shop/wp-json/wc/store/v1"
    out = await tools.call_tool(base, "get_cart", {"id": "347693:1"})

    assert (
        out["continue_url"] == f"https://{HOST}/shop/checkout-link/?products=347693:1"
    )


# --- the HTTP read itself ---


def _store_transport(monkeypatch, handler):
    async def public(url):
        return None

    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(store_api, "validate_egress_url", public)
    monkeypatch.setattr(
        store_api,
        "create_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(record), **kw),
    )
    return requests


async def test_store_api_refuses_non_public_hosts(monkeypatch):
    monkeypatch.setattr(ssrf, "_ALLOW_PRIVATE_EGRESS", False)
    with pytest.raises(ssrf.SSRFError):
        await store_api.get("https://169.254.169.254/wp-json/wc/store/v1", "/products")


async def test_store_api_caps_the_body(monkeypatch):
    big = b"x" * (store_api._MAX_BODY_BYTES + 1)
    _store_transport(monkeypatch, lambda r: httpx.Response(200, content=big))

    with pytest.raises(store_api.ResponseTooLarge):
        await store_api.get(STORE_API, "/products")


async def test_store_api_does_not_follow_redirects(monkeypatch):
    requests = _store_transport(
        monkeypatch,
        lambda r: httpx.Response(301, headers={"location": "https://evil.example/"}),
    )
    resp = await store_api.get(STORE_API, "/products", {"page": 2})

    assert resp.status_code == 301 and len(requests) == 1
    assert str(requests[0].url) == f"{STORE_API}/products?page=2"


# --- the JSON-RPC server (mcp/server.py) ---


def _rpc(name: str, arguments: Any = None, **extra: Any) -> Dict[str, Any]:
    params = {"name": name, "arguments": arguments or {}}
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": params,
        **extra,
    }


async def test_server_wraps_a_result_as_mcp_text_content():
    async def call(name, arguments):
        return {"tool": name, "args": arguments}

    out = await server.handle(_rpc("get_cart", {"id": "1:1"}), call, deadline_s=5)

    assert out["id"] == "1" and "error" not in out
    content = out["result"]["content"]
    assert content[0]["type"] == "text"
    assert json.loads(content[0]["text"]) == {"tool": "get_cart", "args": {"id": "1:1"}}


async def test_server_reports_a_tool_failure_as_a_jsonrpc_error():
    async def call(name, arguments):
        raise server.ToolError("The store answered HTTP 503.")

    out = await server.handle(_rpc("get_cart"), call, deadline_s=5)

    assert "result" not in out
    assert out["error"] == {"code": -32000, "message": "The store answered HTTP 503."}


async def test_server_stops_at_its_deadline():
    async def slow(name, arguments):
        await asyncio.sleep(5)
        return {}

    out = await server.handle(_rpc("search_catalog"), slow, deadline_s=0.05)

    assert (
        out["error"]["code"] == -32000 and "did not answer" in out["error"]["message"]
    )


async def test_server_hides_unexpected_failures():
    async def boom(name, arguments):
        raise RuntimeError("internal detail 10.0.0.7")

    out = await server.handle(_rpc("search_catalog"), boom, deadline_s=5)

    assert out["error"]["code"] == -32603 and "10.0.0.7" not in json.dumps(out)


@pytest.mark.parametrize(
    "body, code",
    [
        ([], -32600),
        ({"jsonrpc": "1.0", "method": "tools/call"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, -32601),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, -32602),
        (_rpc("get_cart", arguments=["not", "an", "object"]), -32602),
    ],
)
async def test_server_refuses_malformed_requests(body, code):
    async def never(name, arguments):
        raise AssertionError("must not run")

    out = await server.handle(body, never, deadline_s=5)

    assert out["error"]["code"] == code


# --- our engine: our MCP URL is answered in process (mcp/in_process.py) ---

HOSTED = f"https://api.breezebuddy.ai/mcp/woocommerce/{HOST}"
_SCHEMAS = [{"name": name} for name in ("search_catalog", "lookup_catalog", "get_cart")]


@pytest.fixture
def no_network(monkeypatch):
    """Fail any real HTTP request: a hosted URL must never leave the process."""

    async def refuse(self, request):
        raise AssertionError(f"network request to {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def _config(url: str = HOSTED) -> McpConfig:
    return McpConfig(servers=[McpServerConfig(url=url, tool_schemas=_SCHEMAS)])


async def _voice_tools(url: str = HOSTED) -> Dict[str, Any]:
    # Telephony voice builds its tools here (agent/__init__.py).
    functions = await mcp_mod.get_mcp_global_functions(_config(url), {})
    return {f.name: f.handler for f in functions}


async def test_voice_loader_answers_a_hosted_url_in_process(store, no_network):
    handlers = await _voice_tools()
    out = await handlers["lookup_catalog"]({"catalog": {"ids": ["347693"]}}, None)

    assert out["status"] == "success"
    assert [p["id"] for p in json.loads(out["data"])["products"]] == ["347693"]


async def test_chat_loader_answers_a_hosted_url_in_process(store, no_network):
    # Chat and widget voice build their tools here (chat/agent/tooling.py).
    functions, _ = await mcp_mod.get_mcp_global_functions_cached(_config(), {}, "tpl-1")
    handlers: Dict[str, Any] = {f.name: f.handler for f in functions}
    out = await handlers["lookup_catalog"]({"catalog": {"ids": ["347693"]}}, None)

    assert out["status"] == "success" and store


async def test_an_mcp_pre_check_answers_a_hosted_url_in_process(store, no_network):
    # Pre-checks build the same tool handler as the loaders (third caller).
    from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
        HttpRequestExecutor,
    )
    from app.ai.voice.agents.breeze_buddy.managers.pre_checks.http import (
        _fetch_mcp_response,
    )
    from app.schemas import PreCheckConfig

    pre_check = PreCheckConfig(
        name="catalog",
        mcp=McpServerConfig(url=HOSTED),
        mcp_tool="lookup_catalog",
        mcp_arguments={"catalog": {"ids": ["347693"]}},
    )
    # The MCP path never uses the executor's HTTP session.
    executor = HttpRequestExecutor(session=cast(Any, None))
    payload, reason = await _fetch_mcp_response(pre_check, {}, executor)

    assert reason is None and payload is not None
    assert [p["id"] for p in payload["products"]] == ["347693"]


async def test_a_failed_cart_read_reaches_the_engine_as_an_error(
    no_network, monkeypatch
):
    # The add-to-cart flow trusts status: a store failure must not arrive as
    # a success holding an error text, or it reads as an empty cart.
    _store_answers(monkeypatch, httpx.Response(503))
    handlers = await _voice_tools()
    out = await handlers["get_cart"]({"id": "347693:1,1128598:2"}, None)

    assert out["status"] == "error" and "HTTP 503" in out["data"]


@pytest.mark.parametrize(
    "url, ours",
    [
        (HOSTED, True),
        (f"http://localhost:8002/mcp/woocommerce/{HOST.upper()}", True),
        # A session value must not pick the store.
        ("https://api.breezebuddy.ai/mcp/woocommerce/{shop_url}", False),
        ("https://{shop_url}/api/ucp/mcp", False),
        ("https://api.breezebuddy.ai/mcp/shopify/x.myshopify.com", False),
        ("https://api.breezebuddy.ai/mcp/woocommerce/not_a_host", False),
    ],
)
def test_only_hosted_urls_written_in_the_template_are_answered_in_process(url, ours):
    assert (in_process.transport(url) is not None) is ours


def test_the_public_route_is_off_by_default():
    from app.main import app

    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/mcp/")]


# --- the public route, when enabled (app/api/routers/mcp.py) ---

ROUTE = f"/mcp/woocommerce/{HOST}"
_RPC = {
    "jsonrpc": "2.0",
    "id": "1",
    "method": "tools/call",
    "params": {"name": "lookup_catalog", "arguments": {"catalog": {"ids": ["347693"]}}},
}


@pytest.fixture
def route(monkeypatch, store):
    """The route mounted on its own app; ``route.full`` fills the store's cap."""
    app = FastAPI()
    app.include_router(mcp_route.router)
    state = SimpleNamespace(full=False, store=store)

    async def rate_limit(**kwargs):
        assert kwargs["identifier"] == HOST
        return SimpleNamespace(allowed=not state.full, retry_after_seconds=1)

    monkeypatch.setattr(mcp_route, "check_rate_limit", rate_limit)
    state.client = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    return state


async def _post(route, path: str = ROUTE, **kwargs: Any) -> httpx.Response:
    async with route.client() as client:
        return await client.post(path, **kwargs)


async def test_the_route_answers_any_store_host(route):
    resp = await _post(route, json=_RPC)
    products = json.loads(resp.json()["result"]["content"][0]["text"])["products"]

    assert resp.status_code == 200 and [p["id"] for p in products] == ["347693"]


@pytest.mark.parametrize(
    "path", ["/mcp/shopify/x.myshopify.com", "/mcp/woocommerce/not_a_host"]
)
async def test_the_route_refuses_other_platforms_and_non_hosts(route, path):
    resp = await _post(route, path, json=_RPC)

    assert resp.status_code == 404 and route.store == []


async def test_the_route_caps_calls_per_store(route):
    route.full = True
    resp = await _post(route, json=_RPC)

    assert resp.status_code == 429 and resp.headers["retry-after"] == "1"
    assert route.store == []


async def test_the_route_answers_bad_json_with_a_jsonrpc_error(route):
    resp = await _post(route, content=b"{not json")

    assert resp.status_code == 200 and resp.json()["error"]["code"] == -32700
