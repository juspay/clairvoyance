"""Tests for the commerce UI-intent flavor on the typed intent engine
(RFC-001 §3.3).

Covers: per-intent payload validation (typed 422 errors), the registered
policy table + agent-turn rewrite, and run_direct_intent's no-LLM
execution — MCP dispatch is mocked at ``ChatAgent._dispatch_tool_call``
so the REAL inject_tool_args → binding-store → apply_state_reducers path
runs, and the emitted stream is asserted end-to-end: user bubble → cart
tools → hydrated CartView (v:2) → turn_end.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp import intents as ci
from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent
from app.ai.voice.agents.breeze_buddy.chat.intents import router as ir
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent

# Importing ``ci`` above registered the commerce policies; the public
# loader must agree (and stay idempotent).
ir.ensure_flavor_intents(["commerce"])

# Load template package first — same circular-import precaution as the
# sibling chat-layer tests.
from app.ai.voice.agents.breeze_buddy.template.types import (
    StateReducer,
    ToolArgInjection,
    UiCatalogConfig,
    UiIntentsConfig,
)
from app.schemas.breeze_buddy.chat import ChatSessionStatus

# ---------------------------------------------------------------------------
# parse_ui_intent — payload validation
# ---------------------------------------------------------------------------


def _wire(intent: str, payload: Dict[str, Any], **extra) -> Dict[str, Any]:
    return {"intent": intent, "component_id": "pg1", "payload": payload, **extra}


def test_parse_valid_add_to_cart():
    parsed = ir.parse_ui_intent(
        _wire(
            "add_to_cart",
            {"variant_id": "gid://shopify/ProductVariant/123", "qty": 2},
            display="Add High Support Sports Bra — M",
        )
    )
    assert parsed.policy.route is ir.IntentRoute.DIRECT
    assert isinstance(parsed.payload, ci.AddToCartPayload)
    assert parsed.payload.qty == 2
    assert parsed.intent.display == "Add High Support Sports Bra — M"


def test_parse_qty_defaults_to_one():
    parsed = ir.parse_ui_intent(_wire("add_to_cart", {"variant_id": "v1"}))
    assert isinstance(parsed.payload, ci.AddToCartPayload)
    assert parsed.payload.qty == 1


def test_parse_unknown_intent_raises_typed_error():
    with pytest.raises(ir.IntentValidationError) as exc:
        ir.parse_ui_intent(_wire("teleport", {}))
    assert exc.value.detail["code"] == "unknown_intent"


def test_parse_invalid_payload_raises_structural_errors_only():
    with pytest.raises(ir.IntentValidationError) as exc:
        ir.parse_ui_intent(_wire("add_to_cart", {"qty": "lots"}))
    detail = exc.value.detail
    assert detail["code"] == "invalid_intent_payload"
    locs = {e["loc"] for e in detail["errors"]}
    assert "variant_id" in locs
    assert "lots" not in json.dumps(detail)  # never echoes input values


def test_parse_ignores_extra_payload_keys():
    """Payload-compat policy (see intents.py docstring): unknown extra keys
    are accepted and dropped — never a 422. Drift logging is covered in
    test_intent_payload_contract.py."""
    parsed = ir.parse_ui_intent(
        _wire("add_to_cart", {"variant_id": "v1", "price": 1.0})
    )
    assert isinstance(parsed.payload, ci.AddToCartPayload)
    assert "price" not in parsed.payload.model_dump()


def test_parse_rejects_malformed_wire_body():
    with pytest.raises(ir.IntentValidationError) as exc:
        ir.parse_ui_intent({"intent": "add_to_cart"})  # no component_id
    assert exc.value.detail["code"] == "invalid_intent"


def test_parse_set_qty_allows_zero():
    parsed = ir.parse_ui_intent(_wire("set_qty", {"line_id": "l1", "qty": 0}))
    assert isinstance(parsed.payload, ci.SetQtyPayload)
    assert parsed.payload.qty == 0


# ---------------------------------------------------------------------------
# Policy table + agent-turn rewrite
# ---------------------------------------------------------------------------


def test_policy_routes_match_rfc_table():
    routes = {name: p.route.value for name, p in ir.INTENT_POLICY.items()}
    assert routes == {
        "add_to_cart": "direct",
        "remove_line": "direct",
        "set_qty": "direct",
        "view_product": "direct",
        "enrich_product": "agent_turn",
        "checkout": "client",
        "track_order": "client",
    }


def test_view_product_is_direct_and_silent():
    """view_product drives the full-panel detail overlay: DIRECT (a
    deterministic get_product read — no LLM), silent (no visible thread
    trace), hydrating the server-only ProductDetail component."""
    parsed = ir.parse_ui_intent(
        _wire(
            "view_product",
            {"product_id": "gid://shopify/Product/9", "title": "Trail Shoe"},
        )
    )
    assert parsed.policy.route is ir.IntentRoute.DIRECT
    assert parsed.policy.silent is True
    assert parsed.policy.drive is not None
    assert parsed.policy.show_op is not None
    op = parsed.policy.show_op("get_product", {"product": {}}, cast(Any, object()))
    assert op["component"] == "ProductDetail"
    assert op["bind"] == {"product": "$tool:get_product#/product"}


def test_enrich_product_is_internal_agent_turn():
    """enrich_product (the overlay's markdown product brief) is an
    AGENT_TURN with internal persistence: the rewritten instruction names
    the product, forbids tools, and demands grounded markdown output."""
    parsed = ir.parse_ui_intent(
        _wire(
            "enrich_product",
            {"product_id": "gid://shopify/Product/9", "title": "Trail Shoe"},
        )
    )
    assert parsed.policy.route is ir.IntentRoute.AGENT_TURN
    assert parsed.policy.internal is True
    assert parsed.policy.silent is False  # silent is the DIRECT-only flag
    rewritten = ir.agent_turn_content(parsed)
    assert '"Trail Shoe" (gid://shopify/Product/9)' in rewritten
    assert "Do not call any tools" in rewritten
    assert "markdown" in rewritten
    assert "Ground every claim" in rewritten


def test_enrich_product_rewrite_without_title_uses_id_only():
    parsed = ir.parse_ui_intent(
        _wire("enrich_product", {"product_id": "gid://shopify/Product/9"})
    )
    rewritten = ir.agent_turn_content(parsed)
    assert "gid://shopify/Product/9" in rewritten
    assert '""' not in rewritten  # no empty-quote artifact when title absent


# ---------------------------------------------------------------------------
# run_direct_intent — mocked MCP dispatch, real pipeline glue
# ---------------------------------------------------------------------------


def _envelope(payload: dict, status: str = "success") -> dict:
    return {"status": status, "data": json.dumps(payload)}


def _cart_payload(lines: List[dict]) -> dict:
    return {
        "id": "gid://shopify/Cart/c1?key=k",
        "continue_url": "https://shop.example/cart",
        "totals": [{"type": "total", "amount": 3398.0}],
        "line_items": lines,
        "cart_token": "c1%3Fkey%3Dk",
    }


def _cart_line(variant: str, qty: int, title: str = "Bra - M") -> dict:
    return {
        "id": f"line-{variant[-1]}",
        "quantity": qty,
        "item": {
            "id": variant,
            "title": title,
            "image_url": "https://cdn.example/bra.jpg",
            "price": 1699.0,
        },
        "totals": [{"type": "total", "amount": 1699.0 * qty}],
    }


def _template() -> Any:
    """Duck-typed template (typed Any so it passes where TemplateModel is
    annotated): real reducer/injection/ui_catalog configs, no flow
    (ChatAgent tolerates flow=None on the direct path)."""
    reducers = [
        StateReducer(
            tool_name=t,
            set_paths={"cart_id": "id", "checkout_url": "continue_url"},
        )
        for t in ("create_cart", "update_cart", "get_cart")
    ]
    injections = [
        ToolArgInjection(tool_name=t, set_paths={"id": "state.data.cart_id"})
        for t in ("update_cart", "get_cart")
    ]
    return SimpleNamespace(
        id="t1",
        flow=None,
        configurations=SimpleNamespace(
            state_reducers=reducers,
            tool_arg_injection=injections,
            client_context=None,
            ui_catalog=UiCatalogConfig(enabled_groups=["core", "commerce"]),
        ),
    )


def _session(catalog_version: Optional[str] = "v2") -> SimpleNamespace:
    widget: Dict[str, Any] = {}
    if catalog_version:
        widget["catalog_version"] = catalog_version
    return SimpleNamespace(
        status=ChatSessionStatus.ACTIVE,
        current_node=None,
        template_id="t1",
        metadata={"template_vars": {}, "widget": widget},
    )


class _Recorder:
    """Captures dispatches + persisted rows for assertions."""

    def __init__(self, responses: Dict[str, List[dict]]) -> None:
        self.responses = responses
        self.dispatched: List[tuple] = []
        self.inserted: List[Dict[str, Any]] = []
        self.state_patches: List[Dict[str, Any]] = []


def _patch_boundary(
    monkeypatch,
    recorder: _Recorder,
    *,
    session: Optional[SimpleNamespace] = None,
    initial_state: Optional[Dict[str, Any]] = None,
    template: Optional[Any] = None,
):
    session = session or _session()
    template = template or _template()

    async def _get_session(session_id):
        return session

    async def _get_template(template_id):
        return template

    async def _get_state(session_id):
        return SimpleNamespace(data=dict(initial_state)) if initial_state else None

    async def _vars(tpl, persisted):
        return {}

    async def _insert(**kwargs):
        recorder.inserted.append(kwargs)
        return SimpleNamespace(idx=len(recorder.inserted))

    async def _upsert(*, chat_session_id, patch):
        recorder.state_patches.append(patch)

    async def _after_turn(*, session_id, current_node):
        return None

    async def _close_pool(pool):
        return None

    class _FakeHttpSession:
        async def close(self):
            return None

    async def _prepare(self, current_node):
        return SimpleNamespace(global_funcs=[]), {"name": "start", "functions": []}

    async def _dispatch(self, call, node, global_funcs, injected_args=None):
        recorder.dispatched.append((call.function_name, injected_args))
        return recorder.responses[call.function_name].pop(0), None

    monkeypatch.setattr(ir, "get_chat_session_by_id", _get_session)
    monkeypatch.setattr(ir, "get_template_by_id_cached", _get_template)
    monkeypatch.setattr(ir, "get_agent_session_state", _get_state)
    monkeypatch.setattr(ir, "build_render_template_vars", _vars)
    monkeypatch.setattr(ir, "insert_chat_message", _insert)
    monkeypatch.setattr(ir, "upsert_agent_session_state_merge", _upsert)
    monkeypatch.setattr(ir, "update_chat_session_after_turn", _after_turn)
    monkeypatch.setattr(ir, "close_mcp_pool", _close_pool)
    monkeypatch.setattr(ir, "create_aiohttp_session", lambda: _FakeHttpSession())
    monkeypatch.setattr(ChatAgent, "prepare_direct_dispatch", _prepare)
    monkeypatch.setattr(ChatAgent, "_dispatch_tool_call", _dispatch)


async def _collect(agen) -> List[SSEEvent]:
    return [ev async for ev in agen]


async def test_add_to_cart_without_cart_creates_and_hydrates(monkeypatch):
    new_cart = _cart_payload([_cart_line("gid://shopify/ProductVariant/9", 1)])
    recorder = _Recorder({"create_cart": [_envelope(new_cart)]})
    _patch_boundary(monkeypatch, recorder)

    parsed = ir.parse_ui_intent(
        _wire(
            "add_to_cart",
            {"variant_id": "gid://shopify/ProductVariant/9"},
            display="Add High Support Sports Bra — M",
        )
    )
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))

    # The "Done." acknowledgement precedes the cart op on the wire so the
    # bubble renders ABOVE the component (the ack is what keeps this turn
    # anchored after a later mutation's CartView sweeps this one).
    assert [e.event for e in events] == [
        "user_committed",
        "function_call_started",
        "function_call_completed",
        "assistant_message",
        "ui_op",
        "turn_end",
    ]
    # No cart in state → create_cart with the one line, nothing else.
    assert [d[0] for d in recorder.dispatched] == ["create_cart"]
    assert recorder.dispatched[0][1] == {
        "cart": {
            "line_items": [
                {"item": {"id": "gid://shopify/ProductVariant/9"}, "quantity": 1}
            ]
        }
    }
    # Hydrated CartView — v2 wire shape, checkout from continue_url,
    # cart_token for the widget-side cookie effect.
    op = events[4].data["op"]
    assert op["op"] == "add" and op["type"] == "CartView" and op["v"] == 2
    props = op["props"]
    assert props["cart_id"] == "gid://shopify/Cart/c1?key=k"
    assert props["line_items"][0]["qty"] == 1
    assert props["checkout"] == {
        "label": "Review and checkout",
        "url": "https://shop.example/cart",
    }
    assert props["cart_token"] == "c1%3Fkey%3Dk"
    # Reducers persisted the lifted identifiers.
    assert recorder.state_patches == [
        {
            "cart_id": "gid://shopify/Cart/c1?key=k",
            "checkout_url": "https://shop.example/cart",
        }
    ]
    # user bubble shows `display`; the typed intent rides an internal block.
    assert events[0].data["content"] == "Add High Support Sports Bra — M"
    user_row = recorder.inserted[0]
    internal = user_row["content_blocks"][1]
    assert internal.get("visibility") == "internal"
    assert "add_to_cart" in internal["text"]
    # One assistant row carries BOTH the ack prose and the cart block —
    # resume replays them as a text bubble + a (sweepable) ui block.
    assert recorder.inserted[-1]["content"] == "Done."
    assert recorder.inserted[-1]["ui_blocks"] == [op]
    assert events[3].data == {
        "idx": len(recorder.inserted),
        "content": "Done.",
    }
    assert events[5].data == {
        "session_status": "ACTIVE",
        "assistant_idx": len(recorder.inserted),
    }


async def test_add_to_cart_with_cart_merges_full_desired_set(monkeypatch):
    existing = _cart_payload([_cart_line("gid://shopify/ProductVariant/9", 1)])
    updated = _cart_payload(
        [
            _cart_line("gid://shopify/ProductVariant/9", 1),
            _cart_line("gid://shopify/ProductVariant/7", 2, title="Leggings"),
        ]
    )
    recorder = _Recorder(
        {"get_cart": [_envelope(existing)], "update_cart": [_envelope(updated)]}
    )
    _patch_boundary(
        monkeypatch,
        recorder,
        initial_state={"cart_id": "gid://shopify/Cart/c1?key=k"},
    )

    parsed = ir.parse_ui_intent(
        _wire("add_to_cart", {"variant_id": "gid://shopify/ProductVariant/7", "qty": 2})
    )
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))

    assert [d[0] for d in recorder.dispatched] == ["get_cart", "update_cart"]
    # get_cart's id was injected from state (arg-injection reuse).
    assert recorder.dispatched[0][1] == {"id": "gid://shopify/Cart/c1?key=k"}
    # update_cart carries the FULL desired set: existing line kept, new added.
    assert recorder.dispatched[1][1]["cart"]["line_items"] == [
        {"item": {"id": "gid://shopify/ProductVariant/9"}, "quantity": 1},
        {"item": {"id": "gid://shopify/ProductVariant/7"}, "quantity": 2},
    ]
    op = next(e for e in events if e.event == "ui_op").data["op"]
    assert op["type"] == "CartView"
    assert len(op["props"]["line_items"]) == 2


async def test_add_to_cart_same_variant_increments_qty(monkeypatch):
    existing = _cart_payload([_cart_line("gid://shopify/ProductVariant/9", 1)])
    recorder = _Recorder(
        {"get_cart": [_envelope(existing)], "update_cart": [_envelope(existing)]}
    )
    _patch_boundary(monkeypatch, recorder, initial_state={"cart_id": "c"})

    parsed = ir.parse_ui_intent(
        _wire("add_to_cart", {"variant_id": "gid://shopify/ProductVariant/9"})
    )
    await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))
    assert recorder.dispatched[1][1]["cart"]["line_items"] == [
        {"item": {"id": "gid://shopify/ProductVariant/9"}, "quantity": 2}
    ]


async def test_remove_line_omits_the_line(monkeypatch):
    existing = _cart_payload(
        [
            _cart_line("gid://shopify/ProductVariant/9", 1),
            _cart_line("gid://shopify/ProductVariant/7", 2),
        ]
    )
    after = _cart_payload([_cart_line("gid://shopify/ProductVariant/7", 2)])
    recorder = _Recorder(
        {"get_cart": [_envelope(existing)], "update_cart": [_envelope(after)]}
    )
    _patch_boundary(monkeypatch, recorder, initial_state={"cart_id": "c"})

    parsed = ir.parse_ui_intent(_wire("remove_line", {"line_id": "line-9"}))
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))
    assert recorder.dispatched[1][1]["cart"]["line_items"] == [
        {"item": {"id": "gid://shopify/ProductVariant/7"}, "quantity": 2}
    ]
    assert any(e.event == "ui_op" for e in events)


async def test_set_qty_changes_quantity(monkeypatch):
    existing = _cart_payload([_cart_line("gid://shopify/ProductVariant/9", 1)])
    recorder = _Recorder(
        {"get_cart": [_envelope(existing)], "update_cart": [_envelope(existing)]}
    )
    _patch_boundary(monkeypatch, recorder, initial_state={"cart_id": "c"})

    parsed = ir.parse_ui_intent(_wire("set_qty", {"line_id": "line-9", "qty": 5}))
    await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))
    assert recorder.dispatched[1][1]["cart"]["line_items"] == [
        {"item": {"id": "gid://shopify/ProductVariant/9"}, "quantity": 5}
    ]


async def test_unknown_line_errors_without_mutation(monkeypatch):
    existing = _cart_payload([_cart_line("gid://shopify/ProductVariant/9", 1)])
    recorder = _Recorder({"get_cart": [_envelope(existing)]})
    _patch_boundary(monkeypatch, recorder, initial_state={"cart_id": "c"})

    parsed = ir.parse_ui_intent(_wire("remove_line", {"line_id": "nope"}))
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))
    assert [d[0] for d in recorder.dispatched] == ["get_cart"]  # no update_cart
    # Business failures ride the in-thread `intent_failed` event, never
    # the `error` event (which drives the delivery-failure banner).
    failed = next(e for e in events if e.event == "intent_failed")
    assert failed.data["code"] == "intent_line_not_found"
    assert not any(e.event == "error" for e in events)
    assert events[-1].event == "turn_end"
    assert events[-1].data["session_status"] == "ACTIVE"


async def test_tool_error_envelope_ends_cleanly(monkeypatch):
    recorder = _Recorder({"create_cart": [_envelope({}, status="error")]})
    _patch_boundary(monkeypatch, recorder)

    parsed = ir.parse_ui_intent(_wire("add_to_cart", {"variant_id": "v1"}))
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))
    failed = next(e for e in events if e.event == "intent_failed")
    assert failed.data["code"] == "intent_tool_failed"
    assert events[-1].data["session_status"] == "ACTIVE"
    assert not any(e.event == "ui_op" for e in events)


async def test_sold_out_add_surfaces_the_ucp_warning(monkeypatch):
    """Live regression (CoreFlex Tee, 2026-07-28): create_cart 'succeeds'
    with an EMPTY cart + a UCP messages[] sold-out warning. The verifier
    converts the result to an error envelope; the shopper must see the
    precise warning text in-thread, not generic retry copy."""
    empty_sold_out_cart = {
        "id": "gid://shopify/Cart/c9",
        "continue_url": "https://shop.example/cart",
        "line_items": [],
        "totals": [{"type": "total", "amount": 0.0}],
        "messages": [
            {
                "type": "warning",
                "content_type": "plain",
                "code": "merchandise_out_of_stock",
                "content": "The product 'CoreFlex Tee - Mehendi - L' is already sold out.",
            }
        ],
    }
    recorder = _Recorder({"create_cart": [_envelope(empty_sold_out_cart)]})
    _patch_boundary(monkeypatch, recorder)

    parsed = ir.parse_ui_intent(
        _wire(
            "add_to_cart", {"variant_id": "gid://shopify/ProductVariant/53511605420323"}
        )
    )
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))
    failed = next(e for e in events if e.event == "intent_failed")
    assert failed.data["code"] == "intent_tool_failed"
    assert failed.data["message"] == (
        "The product 'CoreFlex Tee - Mehendi - L' is already sold out."
    )
    assert not any(e.event == "ui_op" for e in events)
    assert events[-1].data["session_status"] == "ACTIVE"


async def test_ui_intents_config_overrides_names_and_labels(monkeypatch):
    """A template's ``configurations.ui_intents`` renames the cart tools,
    state keys, and checkout label without code changes; unset roles keep
    the UCP defaults (get_cart below stays the default name)."""
    template = _template()
    template.configurations.ui_intents = UiIntentsConfig(
        tools={"create_cart": "shop_cart_create", "update_cart": "shop_cart_update"},
        state_keys={"cart_id": "shop_cart_id"},
        labels={"checkout": "Go to checkout"},
    )
    existing = _cart_payload([_cart_line("gid://shopify/ProductVariant/9", 1)])
    updated = _cart_payload(
        [
            _cart_line("gid://shopify/ProductVariant/9", 1),
            _cart_line("gid://shopify/ProductVariant/7", 1, title="Leggings"),
        ]
    )
    recorder = _Recorder(
        {"get_cart": [_envelope(existing)], "shop_cart_update": [_envelope(updated)]}
    )
    _patch_boundary(
        monkeypatch,
        recorder,
        template=template,
        # cart id lives under the RENAMED state key — the default key being
        # absent proves the driver read the configured one.
        initial_state={"shop_cart_id": "gid://shopify/Cart/c1?key=k"},
    )

    parsed = ir.parse_ui_intent(
        _wire("add_to_cart", {"variant_id": "gid://shopify/ProductVariant/7"})
    )
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))

    # Existing cart (found under the renamed key) → get_cart (default name,
    # role unset) then the RENAMED update tool. create path never fires.
    assert [d[0] for d in recorder.dispatched] == ["get_cart", "shop_cart_update"]
    op = next(e for e in events if e.event == "ui_op").data["op"]
    assert op["type"] == "CartView"
    assert op["props"]["checkout"]["label"] == "Go to checkout"


def test_agent_prunes_data_bound_components_without_v2():
    """Catalog-version negotiation: a session that didn't declare v2 loses
    the data-bound components from the allowlist (prompt + show ops both
    gate on it); a v2 session keeps them."""
    v1_agent = ChatAgent(
        session_id="s1", template=_template(), llm=None, catalog_version=None
    )
    assert v1_agent.ui_allowlist & {"ProductCard", "ProductGrid", "CartView"} == set()
    v2_agent = ChatAgent(
        session_id="s1", template=_template(), llm=None, catalog_version="v2"
    )
    assert {"ProductCard", "ProductGrid", "CartView"} <= v2_agent.ui_allowlist


def test_agent_show_resolver_gated_on_v2():
    assert (
        ChatAgent(
            session_id="s1", template=_template(), llm=None, catalog_version=None
        )._show_resolver()
        is None
    )
    assert (
        ChatAgent(
            session_id="s1", template=_template(), llm=None, catalog_version="v2"
        )._show_resolver()
        is not None
    )


async def test_ended_session_fails_terminally(monkeypatch):
    recorder = _Recorder({})
    ended = _session()
    ended.status = ChatSessionStatus.ENDED
    _patch_boundary(monkeypatch, recorder, session=ended)

    parsed = ir.parse_ui_intent(_wire("add_to_cart", {"variant_id": "v1"}))
    events = await _collect(ir.run_direct_intent(session_id="s1", parsed=parsed))
    assert [e.event for e in events] == ["error", "turn_end"]
    assert events[1].data["session_status"] == "FAILED"
    assert recorder.dispatched == []


def test_add_to_cart_payload_accepts_widget_shape():
    """ProductCard sends product_id alongside variant_id/qty (RFC-001 widget
    shape); the schema must accept it (regression: extra_forbidden 422)."""
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
        AddToCartPayload,
        RemoveLinePayload,
        SetQtyPayload,
    )

    p = AddToCartPayload.model_validate(
        {
            "variant_id": "gid://shopify/ProductVariant/54224673210659",
            "qty": 1,
            "product_id": "gid://shopify/Product/9967507833123",
        }
    )
    assert p.product_id and p.qty == 1

    r = RemoveLinePayload.model_validate(
        {
            "line_id": "line-1",
            "variant_id": "gid://shopify/ProductVariant/1",
            "cart_id": "gid://shopify/Cart/x?key=y",
        }
    )
    assert r.line_id == "line-1"

    s = SetQtyPayload.model_validate(
        {
            "line_id": "line-1",
            "qty": 3,
            "variant_id": "gid://shopify/ProductVariant/1",
            "cart_id": "gid://shopify/Cart/x?key=y",
        }
    )
    assert s.qty == 3


def test_checkout_page_url_config_overrides_continue_url():
    """ui_intents.urls.checkout_page (2026-08-03): a configured fixed
    destination (e.g. the storefront /cart page — the CartView cookie
    sync makes it show the built cart) WINS over the tool's
    checkout-bound continue_url; unset keeps continue_url (today's
    default — the template does NOT set it yet)."""
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
        _cart_view_show_op,
        resolve_cart_config,
    )

    template = _template()
    template.configurations.ui_intents = UiIntentsConfig(
        urls={"checkout_page": "https://shop.example/cart"}
    )
    assert (
        resolve_cart_config(template).checkout_page_url == "https://shop.example/cart"
    )

    class _Agent:
        agent_state: dict = {}
        template = None

    agent = _Agent()
    agent.template = template
    result = _envelope(
        {**_cart_payload([]), "continue_url": "https://shop.example/checkouts/abc"}
    )
    op = _cart_view_show_op("update_cart", result, agent)
    assert op["props"]["checkout"]["url"] == "https://shop.example/cart"

    # Unset → continue_url keeps winning (current live behavior).
    template.configurations.ui_intents = None
    op = _cart_view_show_op("update_cart", result, agent)
    assert op["props"]["checkout"]["url"] == "https://shop.example/checkouts/abc"


def test_checkout_page_url_config_applies_to_render_ui_path_too():
    """The MODEL-DRIVEN render_ui path must resolve the CartView checkout
    button with the SAME precedence as the DIRECT-intent path above.

    It did not. ``_cart_view_show_op`` honoured
    ``ui_intents.urls.checkout_page`` while ``_finalize_commerce`` never
    read ``urls`` at all, so ONE button had TWO destinations: the
    storefront /cart after a widget tap, the platform checkout after a
    model-driven render. The model-driven path is the common one — every
    cart tool's ``tool_ui_instructions`` render CartView through it — so
    most shoppers got the destination the merchant had NOT configured,
    skipping the page the CartView cookie sync exists to populate.

    The model cannot paper over this: ``render_ui_tool`` deliberately
    refuses to read ``checkout`` from model args (server policy), so the
    fix has to live here.
    """
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.render_ui import (
        _finalize_commerce,
    )
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import CartView
    from app.ai.voice.agents.breeze_buddy.chat.ui.binding import BindingStore

    store = BindingStore()
    store.record(
        "get_cart",
        "tu1",
        _envelope(
            {**_cart_payload([]), "continue_url": "https://shop.example/checkouts/abc"}
        ),
    )
    bind = {"cart_id": "$tool:get_cart#/id"}

    def _checkout(template: Any, state_values: Optional[Dict[str, Any]] = None) -> Any:
        props: Dict[str, Any] = {}
        _finalize_commerce(
            "CartView",
            CartView,
            props,
            bind=bind,
            store=store,
            template=template,
            state_values=state_values,
        )
        return props.get("checkout")

    # 1. Configured fixed destination WINS over the tool's continue_url.
    template = _template()
    template.configurations.ui_intents = UiIntentsConfig(
        urls={"checkout_page": "https://shop.example/cart"}
    )
    assert _checkout(template)["url"] == "https://shop.example/cart"

    # 2. Unset → continue_url still wins. The pre-existing precedence for
    #    every template that does NOT configure a page is untouched.
    template.configurations.ui_intents = None
    assert _checkout(template)["url"] == "https://shop.example/checkouts/abc"

    # 3. Blank/non-string config is ignored rather than emitting an empty
    #    href (template JSON is merchant-edited — "" is a live typo).
    template.configurations.ui_intents = UiIntentsConfig(urls={"checkout_page": ""})
    assert _checkout(template)["url"] == "https://shop.example/checkouts/abc"

    # 4. Reducer-state fallback stays LAST: it applies only when neither a
    #    configured page nor a continue_url is available.
    empty = BindingStore()
    empty.record("get_cart", "tu1", _envelope({"id": "gid://shopify/Cart/c1"}))
    props: Dict[str, Any] = {}
    _finalize_commerce(
        "CartView",
        CartView,
        props,
        bind=bind,
        store=empty,
        template=_template(),
        state_values={"checkout_url": "https://shop.example/from-state"},
    )
    assert props["checkout"]["url"] == "https://shop.example/from-state"


def test_pick_checkout_url_is_the_single_precedence_both_paths_share():
    """The order lives in ONE place now. Duplicating it is what let the
    configured tier ship on the DIRECT path and go missing on the
    model-driven one."""
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.roles import (
        pick_checkout_url,
    )

    assert pick_checkout_url("/cfg", "/payload", "/state") == "/cfg"
    assert pick_checkout_url(None, "/payload", "/state") == "/payload"
    assert pick_checkout_url(None, None, "/state") == "/state"
    assert pick_checkout_url(None, None, None) is None
    # Empty and wrong-typed candidates fall through instead of becoming an
    # href — template JSON is merchant-edited.
    assert pick_checkout_url("", "/payload", "/state") == "/payload"
    assert pick_checkout_url(0, ["/nope"], "/state") == "/state"


class TestCartLineIntegrity:
    """``update_cart`` REPLACES the cart with the set we send, so how the
    desired set is built is a data-loss surface, not a formatting detail."""

    def test_unencodable_line_raises_instead_of_being_dropped(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
            UnaddressableCartLine,
            _desired_line_items,
        )

        # A line the gateway ships without item.id (bundle / gift wrap), and
        # one whose quantity arrives as a string — silently skipping either
        # would DELETE it from the shopper's cart.
        for bad in (
            {"id": "line-2", "item": {}, "quantity": 1},
            {"id": "line-2", "item": {"id": "v2"}, "quantity": "2"},
        ):
            lines = [
                {"id": "line-1", "item": {"id": "v1"}, "quantity": 1},
                bad,
            ]
            with pytest.raises(UnaddressableCartLine):
                _desired_line_items(lines)

    def test_desired_set_pairs_each_entry_with_its_line_id(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
            _desired_line_items,
        )

        lines = [
            {"id": "line-A", "item": {"id": "v9"}, "quantity": 1},
            {"id": "line-B", "item": {"id": "v9"}, "quantity": 3},
        ]
        pairs = _desired_line_items(lines)
        assert [line_id for line_id, _ in pairs] == ["line-A", "line-B"]
        # Same variant on both lines — the pairing is the only way to tell
        # them apart downstream.
        assert all(e["item"]["id"] == "v9" for _, e in pairs)


class TestDuplicateVariantLines:
    """A cart may hold two lines of the SAME variant (different
    attributes). Mutations select on the line id; keying on the variant
    would hit both."""

    @staticmethod
    def _apply(payload, lines):
        """Mirror of the driver's mutation block against a desired set."""
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
            RemoveLinePayload,
            _desired_line_items,
        )

        pairs = _desired_line_items(lines)
        new_qty = 0 if isinstance(payload, RemoveLinePayload) else payload.qty
        for line_id, entry in pairs:
            if line_id is not None and line_id == payload.line_id:
                entry["quantity"] = new_qty
                break
        return [e for _, e in pairs if e["quantity"] > 0]

    def test_remove_one_line_keeps_its_same_variant_sibling(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
            RemoveLinePayload,
        )

        lines = [
            {"id": "line-A", "item": {"id": "v9"}, "quantity": 1},
            {"id": "line-B", "item": {"id": "v9"}, "quantity": 3},
        ]
        out = self._apply(RemoveLinePayload(line_id="line-A"), lines)
        assert out == [{"item": {"id": "v9"}, "quantity": 3}]

    def test_set_qty_touches_only_the_named_line(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.intents import (
            SetQtyPayload,
        )

        lines = [
            {"id": "line-A", "item": {"id": "v9"}, "quantity": 1},
            {"id": "line-B", "item": {"id": "v9"}, "quantity": 3},
        ]
        out = self._apply(SetQtyPayload(line_id="line-A", qty=5), lines)
        assert out == [
            {"item": {"id": "v9"}, "quantity": 5},
            {"item": {"id": "v9"}, "quantity": 3},
        ]
