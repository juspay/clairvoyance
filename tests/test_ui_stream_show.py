"""Tests for the catalog-v2 ``show`` op path through ui_stream (RFC-001).

Covers parse_op_line's show branch (component gating, bind-ref grammar,
props collisions, parent rules), process_op_line's resolver threading
(healer bypass, no-resolver suppression, hydrated emission), the
data_bound `add`-op rejection, and the RFC §9.2 golden test: a fixture
search_catalog result + a show ProductGrid line → the exact hydrated op.
"""

from __future__ import annotations

import json

from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    BindingStore,
    resolve_show_op,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.healer import (
    HealerContext,
    make_healer_fn,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import (
    parse_op_line,
    process_op_line,
)

# Load template package first — same circular-import precaution as the
# sibling ui_stream / ui_binding tests.
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import ensure_group_loaded

# The commerce components these fixtures target are a lazily-loaded flavor
# group — register them the way a commerce-enabled template resolution
# would.
ensure_group_loaded("commerce")

ALLOW = {"Stack", "Tile", "ProductGrid", "ProductCard", "CartView"}


def _show_line(**overrides) -> str:
    op = {
        "op": "show",
        "id": "root",
        "component": "ProductGrid",
        "bind": {"products": "$tool:search_catalog#/products"},
        "props": {"max_items": 6, "layout": "carousel"},
    }
    op.update(overrides)
    return json.dumps(op)


# ---------------------------------------------------------------------------
# parse_op_line — show branch
# ---------------------------------------------------------------------------


def test_show_parses_unhydrated_with_marker():
    result = parse_op_line(_show_line(), allowlist=ALLOW)
    assert result.error is None
    assert result.op is not None
    assert result.op["op"] == "show"  # unhydrated — resolver marker
    assert result.op["bind"] == {"products": "$tool:search_catalog#/products"}


def test_show_unknown_component():
    result = parse_op_line(_show_line(component="MysteryGrid"), allowlist=ALLOW)
    assert result.error == "show_unknown_component:'MysteryGrid'"


def test_show_component_disabled():
    result = parse_op_line(_show_line(), allowlist={"Stack", "Tile"})
    assert result.error == "show_component_disabled:ProductGrid"


def test_show_component_not_data_bound():
    result = parse_op_line(_show_line(component="Tile"), allowlist=ALLOW)
    assert result.error == "show_component_not_data_bound:Tile"


def test_show_missing_bind():
    line = json.dumps({"op": "show", "id": "root", "component": "ProductGrid"})
    result = parse_op_line(line, allowlist=ALLOW)
    assert result.error == "show_missing_bind"


def test_show_bad_bind_ref():
    result = parse_op_line(
        _show_line(bind={"products": "search_catalog/products"}), allowlist=ALLOW
    )
    assert result.error == "bad_bind_ref:products"


def test_show_bind_props_collision():
    result = parse_op_line(
        _show_line(props={"products": [], "layout": "grid"}), allowlist=ALLOW
    )
    assert result.error == "bind_props_collision:products"


def test_show_parent_rules_match_add():
    assert parse_op_line(_show_line(id="root"), allowlist=ALLOW).error is None
    assert (
        parse_op_line(_show_line(id="pg9"), allowlist=ALLOW).error == "missing_parent"
    )
    assert (
        parse_op_line(_show_line(id="pg9", parent="root"), allowlist=ALLOW).error
        is None
    )


def test_add_of_data_bound_component_is_rejected():
    line = json.dumps(
        {
            "op": "add",
            "id": "root",
            "type": "ProductGrid",
            "props": {"products": [{"id": "x", "title": "T", "price": 1}]},
        }
    )
    result = parse_op_line(line, allowlist=ALLOW)
    assert result.error == "data_bound_requires_show:ProductGrid"


# ---------------------------------------------------------------------------
# process_op_line — resolver threading
# ---------------------------------------------------------------------------


def _envelope(payload: dict) -> dict:
    return {"status": "success", "data": json.dumps(payload)}


SEARCH_FIXTURE = {
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


def _resolver(store: BindingStore, allowlist=None):
    def _resolve(op):
        return resolve_show_op(op, store, allowlist)

    return _resolve


def test_process_show_without_resolver_drops():
    events = process_op_line(_show_line(id="root"), allowlist=ALLOW)
    assert [e.event for e in events] == ["ui_op_dropped"]
    assert events[0].data["reason"] == "show_no_resolver"


def test_process_show_hydrates_and_emits_ui_op():
    store = BindingStore()
    store.record("search_catalog", "t1", _envelope(SEARCH_FIXTURE))
    events = process_op_line(
        _show_line(id="root"),
        allowlist=ALLOW,
        show_resolver=_resolver(store, ALLOW),
    )
    assert [e.event for e in events] == ["ui_op"]
    op = events[0].data["op"]
    assert op["op"] == "add" and op["type"] == "ProductGrid" and op["v"] == 2


def test_process_show_unresolved_bind_drops_with_reason():
    events = process_op_line(
        _show_line(id="root"),
        allowlist=ALLOW,
        show_resolver=_resolver(BindingStore(), ALLOW),
    )
    assert [e.event for e in events] == ["ui_op_dropped"]
    assert events[0].data["reason"] == "bind_unresolved:search_catalog:/products"


def test_process_show_bypasses_healer():
    """The healer must never touch a show op — validate-or-drop only. A
    show line with an invalid ref drops even though a healer is active,
    and no healer_applied event fires for it."""
    store = BindingStore()
    store.record("search_catalog", "t1", _envelope(SEARCH_FIXTURE))
    healer = make_healer_fn(HealerContext(session_data={}, known_ids=set()))
    bad = _show_line(id="root", bind={"products": "not-a-ref"})
    events = process_op_line(
        bad,
        healer=healer,
        known_ids=set(),
        allowlist=ALLOW,
        show_resolver=_resolver(store, ALLOW),
    )
    assert [e.event for e in events] == ["ui_op_dropped"]
    assert events[0].data["reason"] == "bad_bind_ref:products"

    # And a valid show line passes through the same healer-armed path
    # untouched (no healer_applied noise).
    events = process_op_line(
        _show_line(id="root"),
        healer=healer,
        known_ids=set(),
        allowlist=ALLOW,
        show_resolver=_resolver(store, ALLOW),
    )
    assert [e.event for e in events] == ["ui_op"]


def test_process_show_with_parent_anchors_root():
    """A hydrated show parented to `root` gets the same Stack-root
    injection as any add op — the anchoring path is shared."""
    store = BindingStore()
    store.record("search_catalog", "t1", _envelope(SEARCH_FIXTURE))
    events = process_op_line(
        _show_line(id="pg1", parent="root"),
        known_ids=set(),
        allowlist=ALLOW,
        show_resolver=_resolver(store, ALLOW),
    )
    assert [e.event for e in events] == ["ui_op", "ui_op"]
    assert events[0].data["op"]["id"] == "root"  # injected Stack anchor
    assert events[1].data["op"]["type"] == "ProductGrid"


# ---------------------------------------------------------------------------
# Golden test (RFC-001 §9.2): fixture search_catalog result + show line →
# EXACT hydrated op.
# ---------------------------------------------------------------------------


def test_golden_search_to_hydrated_product_grid():
    store = BindingStore()
    store.record("search_catalog", "toolu_01", _envelope(SEARCH_FIXTURE))
    line = (
        '{"op":"show","id":"root","component":"ProductGrid",'
        '"bind":{"products":"$tool:search_catalog#/products"},'
        '"props":{"max_items":6,"layout":"carousel"}}'
    )
    events = process_op_line(
        line, allowlist=ALLOW, show_resolver=_resolver(store, ALLOW)
    )
    assert len(events) == 1 and events[0].event == "ui_op"
    assert events[0].data["op"] == {
        "op": "add",
        "id": "root",
        "type": "ProductGrid",
        "props": {
            "products": [
                {
                    "id": "gid://shopify/Product/1",
                    "title": "High Support Sports Bra",
                    "url": "https://shop.example/products/bra",
                    "image": {"src": "https://cdn.example/bra.jpg", "alt": "Bra"},
                    "price": {"amount": 1699.0, "currency": "INR"},
                    "tags": ["bestseller"],
                    # Stage-B variant projection: empty for products whose
                    # payload names no (or one) variant — same
                    # empty-collection precedent as tags.
                    "variants": [],
                },
                {
                    "id": "gid://shopify/Product/2",
                    "title": "Seamless Leggings",
                    "price": {"amount": 2199.0, "currency": "INR"},
                    "tags": [],
                    "variants": [],
                },
            ],
            "max_items": 6,
            "layout": "carousel",
        },
        "v": 2,
    }
