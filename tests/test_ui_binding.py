"""Tests for catalog-v2 server-side data binding (RFC-001).

Covers the bind-ref grammar, RFC 6901 pointer walks, the per-turn
BindingStore, and resolve_show_op — hydration, schema validation,
max_items capping, and the bind_unresolved / bind_validation_failed
error family.
"""

from __future__ import annotations

import json

import pytest

from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    BindingStore,
    parse_bind_ref,
    resolve_json_pointer,
    resolve_show_op,
)

# Load template package first so its __init__ chain completes before any
# `chat/*` module import drags in field_resolver mid-load (same trap as
# the sibling ui_stream / session_state tests).
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import ensure_group_loaded

# The commerce components these fixtures bind against are a lazily-loaded
# flavor group — register them the way a commerce-enabled template
# resolution would.
ensure_group_loaded("commerce")

# ---------------------------------------------------------------------------
# Fixtures — post-pipeline (projected/transformed) tool results
# ---------------------------------------------------------------------------


def _envelope(payload: dict, status: str = "success") -> dict:
    """The dispatcher's FlowResult envelope (MCP shape: JSON-string data)."""
    return {"status": status, "data": json.dumps(payload)}


PRODUCTS_FIXTURE = {
    "products": [
        {
            "id": "gid://shopify/Product/1",
            "title": "High Support Sports Bra",
            "url": "https://shop.example/products/bra",
            "image": {"src": "https://cdn.example/bra.jpg", "alt": "Bra"},
            "price": {"amount": 1699.0, "currency": "INR"},
            "tags": ["bestseller"],
        },
        {
            "id": "gid://shopify/Product/2",
            "title": "Seamless Leggings",
            "price": {"amount": 2199.0, "currency": "INR"},
        },
    ]
}


def _store_with(name: str, payload: dict, tool_use_id: str = "toolu_1"):
    store = BindingStore()
    assert store.record(name, tool_use_id, _envelope(payload))
    return store


# ---------------------------------------------------------------------------
# parse_bind_ref — grammar
# ---------------------------------------------------------------------------


def test_parse_bind_ref_plain():
    ref = parse_bind_ref("$tool:search_catalog#/products")
    assert ref is not None
    assert ref.tool_name == "search_catalog"
    assert ref.tool_use_id is None
    assert ref.pointer == "/products"


def test_parse_bind_ref_with_tool_use_id():
    ref = parse_bind_ref("$tool:update_cart@toolu_abc123#/line_items")
    assert ref is not None
    assert ref.tool_name == "update_cart"
    assert ref.tool_use_id == "toolu_abc123"
    assert ref.pointer == "/line_items"


def test_parse_bind_ref_whole_payload_pointer():
    ref = parse_bind_ref("$tool:get_cart#")
    assert ref is not None
    assert ref.pointer == ""


def test_parse_bind_ref_nested_pointer():
    ref = parse_bind_ref("$tool:search_catalog#/products/0/price")
    assert ref is not None
    assert ref.pointer == "/products/0/price"


@pytest.mark.parametrize(
    "bad",
    [
        "search_catalog#/products",  # missing $tool: prefix
        "$tool:search_catalog",  # missing # pointer separator
        "$tool:#/products",  # empty tool name
        "$tool:search catalog#/products",  # space in tool name
        "$tool:search_catalog#products",  # pointer must start with /
        "$state:cart_id#/",  # unknown scheme
        42,  # not a string
    ],
)
def test_parse_bind_ref_rejects_bad_grammar(bad):
    assert parse_bind_ref(bad) is None


# ---------------------------------------------------------------------------
# resolve_json_pointer — RFC 6901 walks
# ---------------------------------------------------------------------------


def test_pointer_walks_dicts_and_lists():
    doc = {"a": [{"b": 7}]}
    value, found = resolve_json_pointer(doc, "/a/0/b")
    assert found and value == 7


def test_pointer_empty_returns_whole_doc():
    doc = {"a": 1}
    value, found = resolve_json_pointer(doc, "")
    assert found and value is doc


def test_pointer_unescapes_tilde_tokens():
    doc = {"a/b": {"c~d": 3}}
    value, found = resolve_json_pointer(doc, "/a~1b/c~0d")
    assert found and value == 3


@pytest.mark.parametrize(
    "pointer",
    ["/missing", "/a/5", "/a/x", "/a/0/b/deeper", "/a/00"],
)
def test_pointer_misses_report_not_found(pointer):
    doc = {"a": [{"b": 1}]}
    _, found = resolve_json_pointer(doc, pointer)
    assert not found


# ---------------------------------------------------------------------------
# BindingStore
# ---------------------------------------------------------------------------


def test_store_unwraps_envelope_and_resolves_latest():
    store = BindingStore()
    store.record("search_catalog", "t1", _envelope({"products": ["old"]}))
    store.record("search_catalog", "t2", _envelope({"products": ["new"]}))
    assert store.resolve("search_catalog") == {"products": ["new"]}
    assert store.resolve("search_catalog", "t1") == {"products": ["old"]}


def test_store_skips_error_envelopes():
    store = BindingStore()
    assert not store.record("update_cart", "t1", _envelope({}, status="error"))
    assert store.resolve("update_cart") is None


def test_store_unknown_tool_resolves_none():
    store = BindingStore()
    assert store.resolve("never_ran") is None


# ---------------------------------------------------------------------------
# resolve_show_op — hydration + validation + caps
# ---------------------------------------------------------------------------


def _grid_show_op(**overrides):
    op = {
        "op": "show",
        "id": "pg1",
        "component": "ProductGrid",
        "bind": {"products": "$tool:search_catalog#/products"},
        "props": {"max_items": 6, "layout": "carousel"},
    }
    op.update(overrides)
    return op


def test_resolve_hydrates_product_grid():
    store = _store_with("search_catalog", PRODUCTS_FIXTURE)
    result = resolve_show_op(_grid_show_op(), store)
    assert result.error is None
    op = result.op
    assert op is not None
    assert op["op"] == "add"
    assert op["type"] == "ProductGrid"
    assert op["v"] == 2
    props = op["props"]
    assert props["layout"] == "carousel"
    assert [p["title"] for p in props["products"]] == [
        "High Support Sports Bra",
        "Seamless Leggings",
    ]
    # Hydrated values are schema-normalised (HttpUrl → str, defaults filled).
    assert props["products"][0]["image"]["src"] == "https://cdn.example/bra.jpg"
    assert props["products"][0]["price"] == {"amount": 1699.0, "currency": "INR"}


def test_resolve_caps_bound_list_at_max_items():
    many = {
        "products": [
            {"id": f"p{i}", "title": f"P{i}", "price": {"amount": 1.0}}
            for i in range(10)
        ]
    }
    store = _store_with("search_catalog", many)
    op = _grid_show_op(props={"max_items": 3})
    result = resolve_show_op(op, store)
    assert result.error is None
    assert result.op is not None
    assert len(result.op["props"]["products"]) == 3


def test_resolve_caps_default_max_items_from_schema():
    many = {
        "products": [
            {"id": f"p{i}", "title": f"P{i}", "price": {"amount": 1.0}}
            for i in range(12)
        ]
    }
    store = _store_with("search_catalog", many)
    result = resolve_show_op(_grid_show_op(props={}), store)
    assert result.error is None
    assert result.op is not None
    # ProductGrid.max_items defaults to 10 (= one full UCP search page,
    # so LLM context and the rendered grid always agree — user decision
    # 2026-07-29 "everything 10").
    assert len(result.op["props"]["products"]) == 10


def _many_products(n: int) -> dict:
    return {
        "products": [
            {"id": f"p{i}", "title": f"P{i}", "price": {"amount": 1.0}}
            for i in range(n)
        ]
    }


def test_items_selects_by_id_regardless_of_rank():
    """Fuzzy-search fix: the shopper's product may sit at ANY rank in the
    page — items[] picks it (and orders the render) by id."""
    store = _store_with("search_catalog", _many_products(10))
    op = _grid_show_op(props={"items": [{"id": "p7"}, {"id": "p2"}]})
    result = resolve_show_op(op, store)
    assert result.error is None
    assert result.op is not None
    assert [p["id"] for p in result.op["props"]["products"]] == ["p7", "p2"]
    # Selection directive is applied server-side, never a render prop.
    assert "items" not in result.op["props"]


def test_items_unknown_entries_are_ignored():
    """A mangled id selects nothing — the model cannot inject items; the
    well-formed ids still render."""
    store = _store_with("search_catalog", _many_products(4))
    op = _grid_show_op(props={"items": [{"id": "p3"}, {"id": "gid://bogus/999"}]})
    result = resolve_show_op(op, store)
    assert result.error is None
    assert result.op is not None
    assert [p["id"] for p in result.op["props"]["products"]] == ["p3"]


def test_items_all_unknown_falls_open_to_full_list():
    """Every id bogus → filter ignored entirely; the full (capped) tool
    list beats an empty render."""
    store = _store_with("search_catalog", _many_products(3))
    op = _grid_show_op(props={"items": [{"id": "nope-1"}, {"id": "nope-2"}]})
    result = resolve_show_op(op, store)
    assert result.error is None
    assert result.op is not None
    assert [p["id"] for p in result.op["props"]["products"]] == ["p0", "p1", "p2"]


def test_items_selection_runs_before_max_items_cap():
    store = _store_with("search_catalog", _many_products(12))
    op = _grid_show_op(
        props={
            "items": [{"id": "p11"}, {"id": "p10"}, {"id": "p9"}],
            "max_items": 2,
        }
    )
    result = resolve_show_op(op, store)
    assert result.error is None
    assert result.op is not None
    # Selection first (rank-independent), then the cap applies to it.
    assert [p["id"] for p in result.op["props"]["products"]] == ["p11", "p10"]


def test_items_feature_variant_rederives_hero_and_stamps_id():
    """RFC-003 §4 variant continuity: feature_variant re-derives the card
    hero (price) from that variant record and stamps featured_variant_id;
    an unknown variant id is fail-open (entry untouched).

    The transform is registered by the commerce group, so the op must be
    resolved with that group in scope — a template without it gets plain
    id selection (asserted at the end)."""
    payload = {
        "products": [
            {
                "id": "p1",
                "title": "CoreFlex Shorts",
                "price": {"amount": 999.0},
                "variants": [
                    {"id": "v-black", "title": "S / Black", "price": {"amount": 999.0}},
                    {"id": "v-pink", "title": "S / Pink", "price": {"amount": 1099.0}},
                ],
            }
        ]
    }
    store = _store_with("search_catalog", payload)
    op = _grid_show_op(props={"items": [{"id": "p1", "feature_variant": "v-pink"}]})
    result = resolve_show_op(op, store, None, ["commerce"])
    assert result.error is None
    assert result.op is not None
    entry = result.op["props"]["products"][0]
    assert entry["featured_variant_id"] == "v-pink"
    assert entry["price"]["amount"] == 1099.0
    # Unknown variant id → untouched entry, no stamp, original hero.
    op2 = _grid_show_op(props={"items": [{"id": "p1", "feature_variant": "v-nope"}]})
    result2 = resolve_show_op(op2, store, None, ["commerce"])
    assert result2.error is None
    assert result2.op is not None
    entry2 = result2.op["props"]["products"][0]
    assert "featured_variant_id" not in entry2
    assert entry2["price"]["amount"] == 999.0
    # Out of scope, the selector key is inert: the entry is selected by id
    # and nothing re-derives it.
    result3 = resolve_show_op(
        _grid_show_op(props={"items": [{"id": "p1", "feature_variant": "v-pink"}]}),
        store,
    )
    assert result3.op is not None
    entry3 = result3.op["props"]["products"][0]
    assert "featured_variant_id" not in entry3
    assert entry3["price"]["amount"] == 999.0


def test_resolve_unresolved_tool_drops_with_reason():
    store = BindingStore()  # search_catalog never ran this turn
    result = resolve_show_op(_grid_show_op(), store)
    assert result.op is None
    assert result.error == "bind_unresolved:search_catalog:/products"


def test_resolve_unresolved_pointer_drops_with_reason():
    store = _store_with("search_catalog", {"items": []})
    result = resolve_show_op(_grid_show_op(), store)
    assert result.op is None
    assert result.error == "bind_unresolved:search_catalog:/products"


def test_resolve_stale_disambiguator_drops():
    store = _store_with("search_catalog", PRODUCTS_FIXTURE, tool_use_id="t1")
    op = _grid_show_op(bind={"products": "$tool:search_catalog@t9#/products"})
    result = resolve_show_op(op, store)
    assert result.op is None
    assert result.error is not None and result.error.startswith("bind_unresolved:")


def test_resolve_invalid_hydrated_props_fail_validation():
    # products missing required fields → schema failure, structural detail only.
    store = _store_with("search_catalog", {"products": [{"nope": True}]})
    result = resolve_show_op(_grid_show_op(props={}), store)
    assert result.op is None
    assert result.error is not None
    assert result.error.startswith("bind_validation_failed:ProductGrid:")
    assert "nope" not in result.error  # never echoes input values


def test_resolve_rejects_non_data_bound_component():
    store = _store_with("search_catalog", PRODUCTS_FIXTURE)
    result = resolve_show_op(_grid_show_op(component="Tile"), store)
    assert result.op is None
    assert result.error is not None and "Tile" in result.error


def test_resolve_respects_allowlist():
    store = _store_with("search_catalog", PRODUCTS_FIXTURE)
    result = resolve_show_op(_grid_show_op(), store, allowlist={"CartView"})
    assert result.op is None
    assert result.error == "show_component_disabled:ProductGrid"


def test_resolve_carries_parent_through():
    store = _store_with("search_catalog", PRODUCTS_FIXTURE)
    result = resolve_show_op(_grid_show_op(parent="root"), store)
    assert result.error is None
    assert result.op is not None
    assert result.op["parent"] == "root"


def test_resolve_cart_view_from_raw_ucp_shape():
    """CartLineP's UCP lift: raw cart lines (item.title / quantity / totals)
    validate without a template projection."""
    cart = {
        "id": "gid://shopify/Cart/c1?key=k1",
        "line_items": [
            {
                "id": "line1",
                "quantity": 2,
                "item": {
                    "id": "gid://shopify/ProductVariant/9",
                    "title": "High Support Sports Bra - M",
                    "image_url": "https://cdn.example/bra.jpg",
                    "price": 1699.0,
                },
                "totals": [{"type": "total", "amount": 3398.0}],
            }
        ],
        "totals": [{"type": "total", "amount": 3398.0}],
        "cart_token": "c1%3Fkey%3Dk1",
    }
    store = _store_with("update_cart", cart)
    op = {
        "op": "show",
        "id": "root",
        "component": "CartView",
        "bind": {
            "cart_id": "$tool:update_cart#/id",
            "line_items": "$tool:update_cart#/line_items",
            "totals": "$tool:update_cart#/totals",
            "cart_token": "$tool:update_cart#/cart_token",
        },
        "props": {
            "checkout": {
                "label": "Review and checkout",
                "url": "https://shop.example/cart",
            }
        },
    }
    result = resolve_show_op(op, store)
    assert result.error is None
    assert result.op is not None
    props = result.op["props"]
    assert props["cart_id"] == "gid://shopify/Cart/c1?key=k1"
    line = props["line_items"][0]
    assert line["title"] == "High Support Sports Bra - M"
    assert line["qty"] == 2
    assert line["line_total"] == {"amount": 3398.0, "currency": "INR"}
    assert line["variant_id"] == "gid://shopify/ProductVariant/9"
    assert props["checkout"]["url"] == "https://shop.example/cart"
    assert props["cart_token"] == "c1%3Fkey%3Dk1"


def test_resolve_product_grid_from_raw_ucp_shape():
    """ProductP's UCP lift: price_range.min → price, media[0] → image."""
    raw = {
        "products": [
            {
                "id": "gid://shopify/Product/7",
                "title": "Trail Shoe",
                "price_range": {"min": {"amount": 4999.0, "currency": "INR"}},
                "media": [{"url": "https://cdn.example/shoe.jpg", "alt_text": "Shoe"}],
            }
        ]
    }
    store = _store_with("search_catalog", raw)
    result = resolve_show_op(_grid_show_op(props={}), store)
    assert result.error is None
    assert result.op is not None
    product = result.op["props"]["products"][0]
    assert product["price"] == {"amount": 4999.0, "currency": "INR"}
    assert product["image"] == {"src": "https://cdn.example/shoe.jpg", "alt": "Shoe"}


def test_resolve_projects_default_variant_id_for_single_variant():
    """Exactly one available variant → its GID lands in default_variant_id
    (drives the widget's built-in add_to_cart); multi-variant products get
    None (routed through view_product instead)."""
    raw = {
        "products": [
            {
                "id": "p-single",
                "title": "One Size Cap",
                "price": {"amount": 499.0},
                "variants": [{"id": "gid://shopify/ProductVariant/11"}],
            },
            {
                "id": "p-multi",
                "title": "Sports Bra",
                "price": {"amount": 1699.0},
                "variants": [
                    {
                        "id": "gid://shopify/ProductVariant/21",
                        "availability": "available",
                    },
                    {
                        "id": "gid://shopify/ProductVariant/22",
                        "availability": "available",
                    },
                ],
            },
            {
                "id": "p-one-left",
                "title": "Last Colour Tee",
                "price": {"amount": 899.0},
                "variants": [
                    {
                        "id": "gid://shopify/ProductVariant/31",
                        "availability": "sold_out",
                    },
                    {
                        "id": "gid://shopify/ProductVariant/32",
                        "availability": "available",
                    },
                ],
            },
        ]
    }
    store = _store_with("search_catalog", raw)
    result = resolve_show_op(_grid_show_op(props={}), store)
    assert result.error is None
    assert result.op is not None
    by_id = {p["id"]: p for p in result.op["props"]["products"]}
    assert by_id["p-single"]["default_variant_id"] == "gid://shopify/ProductVariant/11"
    assert "default_variant_id" not in by_id["p-multi"]  # exclude_none dump
    assert (
        by_id["p-one-left"]["default_variant_id"] == "gid://shopify/ProductVariant/32"
    )
