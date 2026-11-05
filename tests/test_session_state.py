"""Tests for the generic session-state engine (reducer + arg injection).

Both engines are commerce-agnostic — the tests assert that an arbitrary
key shape (cart_id today, foo_handle in a hypothetical other template)
flows correctly. No commerce logic is hardcoded in the engine.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.template.session_state import (
    apply_state_reducers,
    inject_tool_args,
)

# Order matters: template.types fully loads the template package (whose
# __init__ pulls in field_resolver / http_requester / hooks) BEFORE we
# touch the session_state module, otherwise the StateReducer/
# ToolArgInjection imports trigger handlers.transport.utils.__init__ →
# field_resolver → template.types again (mid-load) and the import fails.
from app.ai.voice.agents.breeze_buddy.template.types import (
    StateReducer,
    ToolArgInjection,
)

# ---------------------------------------------------------------------------
# apply_state_reducers
# ---------------------------------------------------------------------------


def _flow_result(payload: dict, status: str = "success") -> dict:
    """Wrap a parsed payload in the dispatcher's FlowResult envelope."""
    import json as _json

    return {"status": status, "data": _json.dumps(payload)}


def test_reducer_lifts_single_field_from_payload():
    state: dict = {}
    rule = StateReducer(
        tool_name="update_cart",
        set_paths={"cart_id": "cart.id"},
    )
    result = _flow_result({"cart": {"id": "gid://shopify/Cart/abc"}})
    out = apply_state_reducers(state, "update_cart", result, [rule])
    assert out == {"cart_id": "gid://shopify/Cart/abc"}


def test_reducer_lifts_multiple_fields_in_one_rule():
    rule = StateReducer(
        tool_name="update_cart",
        set_paths={
            "cart_id": "cart.id",
            "checkout_url": "cart.checkout_url",
            "currency": "cart.cost.total_amount.currency",
        },
    )
    result = _flow_result(
        {
            "cart": {
                "id": "c1",
                "checkout_url": "https://shop/c/c1",
                "cost": {"total_amount": {"amount": "49.95", "currency": "INR"}},
            }
        }
    )
    out = apply_state_reducers({}, "update_cart", result, [rule])
    assert out["cart_id"] == "c1"
    assert out["checkout_url"] == "https://shop/c/c1"
    assert out["currency"] == "INR"


def test_reducer_does_not_match_other_tools():
    rule = StateReducer(tool_name="update_cart", set_paths={"cart_id": "cart.id"})
    result = _flow_result({"cart": {"id": "x"}})
    out = apply_state_reducers({}, "search_catalog", result, [rule])
    assert out == {}


def test_reducer_preserves_prior_state_for_unset_paths():
    rule = StateReducer(tool_name="update_cart", set_paths={"cart_id": "cart.id"})
    result = _flow_result({"cart": {"id": "new"}})
    out = apply_state_reducers(
        {"cart_id": "old", "customer_id": "cust1"},
        "update_cart",
        result,
        [rule],
    )
    # cart_id replaced from new payload; customer_id untouched.
    assert out == {"cart_id": "new", "customer_id": "cust1"}


def test_reducer_skips_on_jmespath_no_match():
    rule = StateReducer(tool_name="update_cart", set_paths={"cart_id": "cart.id"})
    result = _flow_result({"errors": [{"message": "cart does not exist"}]})
    out = apply_state_reducers({"cart_id": "kept"}, "update_cart", result, [rule])
    # cart.id is None — rule skipped; prior state preserved.
    assert out == {"cart_id": "kept"}


def test_reducer_skips_on_error_envelope_when_only_on_success():
    rule = StateReducer(
        tool_name="update_cart",
        set_paths={"cart_id": "cart.id"},
        only_on_success=True,
    )
    result = _flow_result({"cart": {"id": "x"}}, status="error")
    out = apply_state_reducers({"cart_id": "kept"}, "update_cart", result, [rule])
    assert out == {"cart_id": "kept"}


def test_reducer_applies_on_error_when_only_on_success_false():
    rule = StateReducer(
        tool_name="update_cart",
        set_paths={"cart_id": "cart.id"},
        only_on_success=False,
    )
    result = _flow_result({"cart": {"id": "x"}}, status="error")
    out = apply_state_reducers({}, "update_cart", result, [rule])
    assert out == {"cart_id": "x"}


def test_reducer_input_state_is_not_mutated():
    """Engine returns a new dict — callers can safely pass in a live state."""
    rule = StateReducer(tool_name="update_cart", set_paths={"cart_id": "cart.id"})
    initial = {"cart_id": "old"}
    result = _flow_result({"cart": {"id": "new"}})
    out = apply_state_reducers(initial, "update_cart", result, [rule])
    assert initial == {"cart_id": "old"}  # untouched
    assert out == {"cart_id": "new"}


def test_reducer_with_empty_rules_returns_copy_of_state():
    initial = {"cart_id": "x"}
    out = apply_state_reducers(initial, "any_tool", _flow_result({}), [])
    assert out == initial
    assert out is not initial  # still a defensive copy


def test_reducer_works_for_arbitrary_non_commerce_key():
    """The engine is commerce-agnostic — a hypothetical travel template
    using `booking_handle` works the same."""
    rule = StateReducer(
        tool_name="book_flight",
        set_paths={"booking_handle": "reservation.handle"},
    )
    result = _flow_result({"reservation": {"handle": "PNR-XYZ123"}})
    out = apply_state_reducers({}, "book_flight", result, [rule])
    assert out == {"booking_handle": "PNR-XYZ123"}


# ---------------------------------------------------------------------------
# inject_tool_args
# ---------------------------------------------------------------------------


def test_injection_fills_missing_arg_from_state():
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
    )
    out = inject_tool_args(
        tool_name="update_cart",
        args={"add_items": [{"product_variant_id": "v1", "quantity": 1}]},
        state_data={"cart_id": "c1"},
        chat_session_id="sess-uuid",
        injections=[rule],
    )
    assert out["cart_id"] == "c1"
    assert out["add_items"] == [{"product_variant_id": "v1", "quantity": 1}]


def test_injection_preserves_explicit_llm_value_by_default():
    """only_if_missing=True (default) — LLM's value wins."""
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
    )
    out = inject_tool_args(
        tool_name="update_cart",
        args={"cart_id": "llm-provided"},
        state_data={"cart_id": "from-state"},
        chat_session_id="sess",
        injections=[rule],
    )
    assert out["cart_id"] == "llm-provided"


def test_injection_force_override_when_only_if_missing_false():
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
        only_if_missing=False,
    )
    out = inject_tool_args(
        tool_name="update_cart",
        args={"cart_id": "llm-hallucinated"},
        state_data={"cart_id": "canonical"},
        chat_session_id="sess",
        injections=[rule],
    )
    assert out["cart_id"] == "canonical"


def test_injection_no_state_value_leaves_args_alone():
    """No source value → rule no-ops; the dispatcher hits the upstream
    without cart_id, which lets the MCP server mint a new one
    (the existing fallback path)."""
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
    )
    out = inject_tool_args(
        tool_name="update_cart",
        args={"add_items": []},
        state_data={},  # no cart_id yet
        chat_session_id="sess",
        injections=[rule],
    )
    assert "cart_id" not in out
    assert out == {"add_items": []}


def test_injection_only_matches_named_tool():
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
    )
    out = inject_tool_args(
        tool_name="search_catalog",
        args={"query": "snowboard"},
        state_data={"cart_id": "c1"},
        chat_session_id="sess",
        injections=[rule],
    )
    assert "cart_id" not in out


def test_injection_can_read_session_id():
    """A future _meta-injection target can pull the session id directly."""
    rule = ToolArgInjection(
        tool_name="any_tool",
        set_paths={"buddy_session_id": "session_id"},
    )
    out = inject_tool_args(
        tool_name="any_tool",
        args={},
        state_data={},
        chat_session_id="abc-123",
        injections=[rule],
    )
    assert out["buddy_session_id"] == "abc-123"


def test_injection_args_not_mutated():
    """inject_tool_args returns a copy — caller's args dict is safe."""
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
    )
    original = {"add_items": [{"qty": 1}]}
    out = inject_tool_args(
        tool_name="update_cart",
        args=original,
        state_data={"cart_id": "c1"},
        chat_session_id="sess",
        injections=[rule],
    )
    assert "cart_id" not in original
    assert out["cart_id"] == "c1"


def test_injection_with_empty_rules_returns_copy_of_args():
    original = {"a": 1}
    out = inject_tool_args(
        tool_name="x",
        args=original,
        state_data={},
        chat_session_id="s",
        injections=[],
    )
    assert out == original
    assert out is not original


# ---------------------------------------------------------------------------
# Generators (uuid_v4, timestamps, …) — commerce-agnostic
# ---------------------------------------------------------------------------


def test_injection_generator_uuid_v4_fills_missing_arg():
    """S3.2: idempotency-key style — engine stamps a fresh UUID when
    the LLM didn't provide one."""
    import uuid as _uuid

    rule = ToolArgInjection(
        tool_name="create_cart",
        generators={"idempotency_key": "uuid_v4"},
    )
    out = inject_tool_args(
        tool_name="create_cart",
        args={"cart": {"line_items": []}},
        state_data={},
        chat_session_id="sess",
        injections=[rule],
    )
    assert "idempotency_key" in out
    # Round-trips as a UUID and is a v4
    parsed = _uuid.UUID(out["idempotency_key"])
    assert parsed.version == 4


def test_injection_generator_respects_only_if_missing():
    rule = ToolArgInjection(
        tool_name="create_cart",
        generators={"idempotency_key": "uuid_v4"},
    )
    out = inject_tool_args(
        tool_name="create_cart",
        args={"idempotency_key": "caller-supplied"},
        state_data={},
        chat_session_id="sess",
        injections=[rule],
    )
    # Caller value wins — same discipline as set_paths
    assert out["idempotency_key"] == "caller-supplied"


def test_injection_generator_force_override():
    rule = ToolArgInjection(
        tool_name="create_cart",
        generators={"idempotency_key": "uuid_v4"},
        only_if_missing=False,
    )
    out = inject_tool_args(
        tool_name="create_cart",
        args={"idempotency_key": "stale-value"},
        state_data={},
        chat_session_id="sess",
        injections=[rule],
    )
    assert out["idempotency_key"] != "stale-value"


def test_injection_generator_unknown_name_is_silently_skipped():
    """An unknown generator name should not crash the turn — it logs
    a warning and leaves the arg dict alone (same defensive stance as
    set_paths)."""
    rule = ToolArgInjection(
        tool_name="x",
        generators={"foo": "not_a_real_generator"},
    )
    out = inject_tool_args(
        tool_name="x",
        args={},
        state_data={},
        chat_session_id="sess",
        injections=[rule],
    )
    assert "foo" not in out


def test_injection_generator_each_call_produces_fresh_value():
    rule = ToolArgInjection(
        tool_name="x",
        generators={"req_id": "uuid_v4"},
    )
    a = inject_tool_args(
        tool_name="x", args={}, state_data={}, chat_session_id="s", injections=[rule]
    )
    b = inject_tool_args(
        tool_name="x", args={}, state_data={}, chat_session_id="s", injections=[rule]
    )
    assert a["req_id"] != b["req_id"]


def test_injection_generator_timestamp_iso8601():
    rule = ToolArgInjection(
        tool_name="x",
        generators={"ts": "timestamp_iso8601"},
    )
    out = inject_tool_args(
        tool_name="x", args={}, state_data={}, chat_session_id="s", injections=[rule]
    )
    # ISO-8601 UTC like 2026-05-20T12:34:56.789012+00:00
    assert "T" in out["ts"]
    assert out["ts"].endswith("+00:00")


def test_injection_generator_timestamp_unix_ms():
    rule = ToolArgInjection(
        tool_name="x",
        generators={"ts": "timestamp_unix_ms"},
    )
    out = inject_tool_args(
        tool_name="x", args={}, state_data={}, chat_session_id="s", injections=[rule]
    )
    assert isinstance(out["ts"], int)
    # Sanity: ~13 digits for ms since epoch in this era
    assert 1_700_000_000_000 < out["ts"] < 4_000_000_000_000


def test_injection_set_paths_and_generators_coexist_on_one_rule():
    """update_cart needs cart_id from state AND a fresh idempotency_key —
    both in one rule."""
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"id": "state.data.cart_id"},
        generators={"idempotency_key": "uuid_v4"},
    )
    out = inject_tool_args(
        tool_name="update_cart",
        args={"cart": {"line_items": [{"item": {"id": "gid://Shopify/v1"}}]}},
        state_data={"cart_id": "c-123"},
        chat_session_id="sess",
        injections=[rule],
    )
    assert out["id"] == "c-123"
    assert "idempotency_key" in out


# ---------------------------------------------------------------------------
# End-to-end: simulate the reported failure scenario
# ---------------------------------------------------------------------------


def test_full_lifecycle_reproduces_the_fix():
    """The exact failure mode from logs.txt:
    1) update_cart creates a cart, returns cart.id = c1
    2) next turn, LLM omits cart_id (or hallucinates one)
    3) without the engine: server returns "cart does not exist"
    4) with the engine: state.data.cart_id is c1; injection fills it
    """
    reducer = StateReducer(
        tool_name="update_cart",
        set_paths={"cart_id": "cart.id"},
    )
    injection = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
    )

    # Turn 1: LLM calls update_cart(add_items=[snowboard]); MCP creates a cart.
    state: dict = {}
    turn1_args = {"add_items": [{"product_variant_id": "snowboard_v", "quantity": 1}]}
    # No cart_id in args yet, no cart_id in state — injection is no-op.
    injected = inject_tool_args("update_cart", turn1_args, state, "sess", [injection])
    assert "cart_id" not in injected

    # Server returns a fresh cart.
    result = _flow_result(
        {
            "cart": {
                "id": "gid://shopify/Cart/hWNCJg...",
                "checkout_url": "https://shop/c/hWNCJg",
            }
        }
    )
    state = apply_state_reducers(state, "update_cart", result, [reducer])
    assert state["cart_id"] == "gid://shopify/Cart/hWNCJg..."

    # Turn 2: LLM calls update_cart(add_items=[wax]) — forgets cart_id.
    turn2_args = {"add_items": [{"product_variant_id": "wax_v", "quantity": 1}]}
    injected = inject_tool_args("update_cart", turn2_args, state, "sess", [injection])
    # Engine fills it from session state — no more hallucination / orphan carts.
    assert injected["cart_id"] == "gid://shopify/Cart/hWNCJg..."
    assert injected["add_items"] == [{"product_variant_id": "wax_v", "quantity": 1}]


# ---------------------------------------------------------------------------
# Regression: idempotency_hash must hash post-injection args
# ---------------------------------------------------------------------------


def test_idempotency_hash_includes_injected_set_paths():
    """Regression: hash was previously computed from the original LLM args,
    missing any cart_id (or other id) injected by ``set_paths`` in the same
    rule. Two materially different dispatched requests would then collide
    on the same idempotency key and the second would be no-op'd by the
    upstream server.
    """
    rule = ToolArgInjection(
        tool_name="update_cart",
        set_paths={"cart_id": "state.data.cart_id"},
        generators={"idempotency_key": "idempotency_hash"},
    )
    base_args = {"add_items": [{"product_variant_id": "v1", "quantity": 1}]}

    # Same LLM args, different state cart_ids — must produce different hashes.
    out_a = inject_tool_args(
        tool_name="update_cart",
        args=dict(base_args),
        state_data={"cart_id": "cart-A"},
        chat_session_id="sess",
        turn_id="turn-1",
        injections=[rule],
    )
    out_b = inject_tool_args(
        tool_name="update_cart",
        args=dict(base_args),
        state_data={"cart_id": "cart-B"},
        chat_session_id="sess",
        turn_id="turn-1",
        injections=[rule],
    )
    assert out_a["cart_id"] == "cart-A"
    assert out_b["cart_id"] == "cart-B"
    assert out_a["idempotency_key"] != out_b["idempotency_key"], (
        "hash must include the injected cart_id; two distinct carts "
        "produced the same idempotency key"
    )
