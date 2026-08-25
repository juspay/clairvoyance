"""WISMO v2 — OrderStatus component, projection, derivation, field gate.

Covers the Phase 0 contract end to end:

- ``OrderP`` lifts the LIVE nautilus samples (2026-08-25: the unfulfilled
  and the fulfilled ``#110145`` captures) — offset timestamps, derived
  display fields, nullable tracking block.
- ``derive_status`` — the v1 prompt's status table as code.
- The literal-fields gate — pure mechanics (caps, list bounds, the
  same-turn-page-read requirement); NO content judgment — the LLM
  generates the results (product call 2026-08-26).
- The courier-blind page-read annotator wrapper.
- ``execute_render_ui`` literal-fields flow: shaped transcriptions
  merge; malformed shapes drop field-wise with reasons; fail-closed
  without a registered gate.
- Text-channel bypass: a ``show`` op naming a literal field drops at
  parse.
"""

import json
from pathlib import Path

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.wismo import (
    LITERAL_CAPS,
    PAGE_TEXT_CAP,
    OrderP,
    OrderStatus,
    derive_status,
    verify_wismo_literals,
    wrap_page_read_result,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.binding import BindingStore
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    build_render_ui_schema,
    execute_render_ui,
    render_ui_components,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import parse_op_line
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import resolve_allowlist

FIXTURE = (
    Path(__file__).parent / "assist" / "fixtures" / "shiprocket_tracking_page.jina.txt"
)

# Live capture 2026-08-25 — order #110145 pre-fulfilment.
UNFULFILLED = {
    "order_name": "#110145",
    "order_number": 110145,
    "created_at": "2026-08-24T18:03:17+05:30",
    "fulfillment_status": "unfulfilled",
    "financial_status": "pending",
    "line_items": ["Rawdha Attar – Madina-Inspired Signature Blend, 12ml"],
    "tracking_company": None,
    "tracking_number": None,
    "tracking_url": None,
    "shipment_status": None,
    "order_status_url": "https://amirandsons.com/x/orders/tok/authenticate?key=k",
}

# Same order after fulfilment (live capture, same day).
FULFILLED = {
    **UNFULFILLED,
    "fulfillment_status": "fulfilled",
    "tracking_company": "Shadowfax DS 1kg",
    "tracking_number": "SF1876236403KAD",
    "tracking_url": "https://shiprocket.co/tracking/SF1876236403KAD",
    "shipment_status": "confirmed",
}

# A checkpoint row transcribed near-verbatim from the fixture page.
GOOD_UPDATE = "25 Aug, 12:32 PM — Item out for pickup"


@pytest.fixture(scope="module")
def page_text() -> str:
    return FIXTURE.read_text()


@pytest.fixture(scope="module")
def commerce_allowlist():
    return resolve_allowlist(enabled_groups=["core", "commerce"])


# ---------------------------------------------------------------------------
# OrderP projection
# ---------------------------------------------------------------------------


class TestOrderP:
    def test_lifts_live_unfulfilled_sample(self):
        p = OrderP.model_validate(UNFULFILLED)
        assert p.order_name == "#110145"
        assert p.order_number == 110145
        assert p.placed_display == "placed 24 Aug"  # offset timestamp parsed
        assert p.items_display == UNFULFILLED["line_items"][0]
        assert p.tracking_url is None

    def test_financial_status_never_projected(self):
        p = OrderP.model_validate(FULFILLED)
        assert "financial_status" not in p.model_dump()

    def test_order_name_falls_back_to_number(self):
        p = OrderP.model_validate({"order_number": 42})
        assert p.order_name == "#42"

    def test_items_display_truncates_past_two(self):
        p = OrderP.model_validate(
            {"order_name": "#1", "line_items": ["A", "B", "C", "D"]}
        )
        assert p.items_display == "A · B …and 2 more"

    def test_bad_timestamp_degrades_to_no_placed_display(self):
        p = OrderP.model_validate({"order_name": "#1", "created_at": "not-a-date"})
        assert p.placed_display is None


# ---------------------------------------------------------------------------
# Status derivation — the v1 table as code
# ---------------------------------------------------------------------------


class TestDeriveStatus:
    @pytest.mark.parametrize(
        "shipment,fulfillment,has_tracking,key,tone",
        [
            ("delivered", "fulfilled", True, "delivered", "positive"),
            ("out_for_delivery", "fulfilled", True, "out_for_delivery", "info"),
            ("in_transit", "fulfilled", True, "in_transit", "info"),
            ("confirmed", "fulfilled", True, "in_transit", "info"),  # live vocab
            ("label_printed", "fulfilled", True, "in_transit", "info"),
            ("attempted_delivery", "fulfilled", True, "attempted", "warning"),
            ("failure", "fulfilled", True, "issue", "warning"),
            (None, "partial", False, "partial", "info"),
            (None, "unfulfilled", False, "preparing", "neutral"),  # live sample
            (None, None, False, "preparing", "neutral"),
            (None, "fulfilled", True, "in_transit", "info"),  # tracking, no signal
            (None, "fulfilled", False, "unknown", "neutral"),
        ],
    )
    def test_table(self, shipment, fulfillment, has_tracking, key, tone):
        got_key, headline, got_tone = derive_status(shipment, fulfillment, has_tracking)
        assert (got_key, got_tone) == (key, tone)
        assert headline  # every key has a headline

    def test_eta_leads_headline_only_for_moving_states(self):
        moving = OrderStatus.model_validate(
            {"order": FULFILLED, "eta_display": "Friday, 10 July"}
        )
        assert moving.headline == "Arriving Friday, 10 July"
        delivered = OrderStatus.model_validate(
            {
                "order": {**FULFILLED, "shipment_status": "delivered"},
                "eta_display": "Friday, 10 July",  # stale ETA must not lead
            }
        )
        assert delivered.headline == "Delivered"

    def test_derived_fields_serialize_for_persistence(self):
        dumped = OrderStatus.model_validate({"order": UNFULFILLED}).model_dump(
            exclude_none=True, mode="json"
        )
        assert dumped["status_key"] == "preparing"
        assert dumped["headline"] == "Being prepared"
        assert dumped["tone"] == "neutral"


# ---------------------------------------------------------------------------
# Page-read annotator (courier-blind wrapper)
# ---------------------------------------------------------------------------


class TestPageReadWrapper:
    def test_wraps_and_caps_success_text(self):
        result = wrap_page_read_result(
            {}, {"status": "success", "status_code": 200, "data": "x" * 20_000}
        )
        assert result["data"] == {"page_text": "x" * PAGE_TEXT_CAP}

    def test_error_envelope_untouched(self):
        env = {"status": "error", "error": "timeout"}
        assert wrap_page_read_result({}, env) is env

    def test_dict_payload_untouched(self):
        env = {"status": "success", "data": {"already": "structured"}}
        assert wrap_page_read_result({}, env) is env


# ---------------------------------------------------------------------------
# verify_wismo_literals
# ---------------------------------------------------------------------------


def _store_with_page(page_text: str) -> BindingStore:
    store = BindingStore()
    store.record(
        "read_page_content",
        "t1",
        wrap_page_read_result(
            {}, {"status": "success", "status_code": 200, "data": page_text}
        ),
    )
    return store


class TestVerifyLiterals:
    def test_no_page_read_drops_everything(self):
        accepted, dropped = verify_wismo_literals(
            "OrderStatus",
            OrderStatus,
            {"eta_display": "Friday, 10 July"},
            store=BindingStore(),
            template=None,
        )
        assert accepted == {}
        assert dropped == {"eta_display": "no_page_read_this_turn"}

    def test_mixed_accept_and_drop(self, page_text):
        accepted, dropped = verify_wismo_literals(
            "OrderStatus",
            OrderStatus,
            {
                "eta_display": "Friday, 10 July",
                "latest_update": GOOD_UPDATE,
                "updates": [GOOD_UPDATE, "second row", 42, "  "],
            },
            store=_store_with_page(page_text),
            template=None,
        )
        assert accepted["eta_display"] == "Friday, 10 July"
        assert accepted["latest_update"] == GOOD_UPDATE
        # Non-string entries coerced away; strings pass through as-is.
        assert accepted["updates"] == [GOOD_UPDATE, "second row"]
        assert dropped == {}

    def test_truncation_to_display_caps(self, page_text):
        long_update = GOOD_UPDATE + " " + "at BOM_ReayRoad_FM " * 20
        accepted, _ = verify_wismo_literals(
            "OrderStatus",
            OrderStatus,
            {"latest_update": long_update},
            store=_store_with_page(page_text),
            template=None,
        )
        assert len(accepted["latest_update"]) <= LITERAL_CAPS["latest_update"]

    def test_eta_is_trusted_like_any_literal(self, page_text):
        # Product call 2026-08-26: whether a date is an ETA vs a
        # checkpoint is the LLM's judgment (steered by the fields
        # description) — the server only holds the numeric floor.
        accepted, dropped = verify_wismo_literals(
            "OrderStatus",
            OrderStatus,
            {
                "eta_display": "25 Aug",
                "updates": ["25 Aug, 12:32 PM — Item out for pickup"],
            },
            store=_store_with_page(page_text),
            template=None,
        )
        assert accepted["eta_display"] == "25 Aug"
        assert accepted["updates"]
        assert dropped == {}

    def test_non_string_shapes_dropped(self, page_text):
        accepted, dropped = verify_wismo_literals(
            "OrderStatus",
            OrderStatus,
            {"eta_display": 42, "updates": "not-a-list"},
            store=_store_with_page(page_text),
            template=None,
        )
        assert accepted == {}
        assert dropped == {"eta_display": "not_a_string", "updates": "not_a_list"}


# ---------------------------------------------------------------------------
# execute_render_ui — the full function path
# ---------------------------------------------------------------------------


def _full_store(page_text: str) -> BindingStore:
    store = _store_with_page(page_text)
    store.record(
        "get_order_status",
        "t0",
        {
            "status": "success",
            "status_code": 200,
            "data": {"found": True, "orders": [FULFILLED]},
        },
    )
    return store


class TestRenderUiFlow:
    def test_verified_transcriptions_render_and_drops_are_named(
        self, page_text, commerce_allowlist
    ):
        outcome = execute_render_ui(
            {
                "component": "OrderStatus",
                "bind": [{"prop": "order", "ref": "$tool:get_order_status#/orders/0"}],
                "fields": [
                    {"name": "eta_display", "value": "Friday, 10 July"},
                    {"name": "latest_update", "value": GOOD_UPDATE},
                    {"name": "updates", "values": [GOOD_UPDATE]},
                ],
            },
            store=_full_store(page_text),
            allowlist=commerce_allowlist,
            components=render_ui_components(commerce_allowlist, True),
            op_id="root",
            flavor_groups=["commerce"],
        )
        assert outcome.decision == "rendered"
        props = outcome.ops[0]["props"]
        assert props["latest_update"] == GOOD_UPDATE
        assert props["updates"] == [GOOD_UPDATE]
        assert props["eta_display"] == "Friday, 10 July"
        assert props["headline"] == "Arriving Friday, 10 July"
        assert outcome.fn_result.get("dropped_fields", {}) == {}
        # Summary carries the WISMO memory shape.
        assert outcome.fn_result["order"] == "#110145"
        assert outcome.fn_result["state"] == "in_transit"

    def test_top_level_literal_args_tolerated(self, page_text, commerce_allowlist):
        outcome = execute_render_ui(
            {
                "component": "OrderStatus",
                "bind": [{"prop": "order", "ref": "$tool:get_order_status#/orders/0"}],
                "latest_update": GOOD_UPDATE,  # natural form, no fields[] wrapper
            },
            store=_full_store(page_text),
            allowlist=commerce_allowlist,
            components=render_ui_components(commerce_allowlist, True),
            op_id="root",
            flavor_groups=["commerce"],
        )
        assert outcome.decision == "rendered"
        assert outcome.ops[0]["props"]["latest_update"] == GOOD_UPDATE

    def test_bind_cannot_target_literal_fields(self, page_text, commerce_allowlist):
        """Review hardening (2026-08-25): binding eta_display/latest_update
        to raw payload pointers must be REJECTED — a bind walks the raw
        tool payload, skipping the anchoring verifier and reaching fields
        the projection never renders (live repro: financial_status
        rendered as latest_update with NO page read at all)."""
        store = BindingStore()
        store.record(
            "get_order_status",
            "t0",
            {
                "status": "success",
                "status_code": 200,
                "data": {
                    "found": True,
                    "orders": [
                        {
                            "order_name": "#1",
                            "financial_status": "refunded",
                            "note": "Arriving Friday, 10 July",
                        }
                    ],
                },
            },
        )
        outcome = execute_render_ui(
            {
                "component": "OrderStatus",
                "bind": [
                    {"prop": "order", "ref": "$tool:get_order_status#/orders/0"},
                    {
                        "prop": "eta_display",
                        "ref": "$tool:get_order_status#/orders/0/note",
                    },
                    {
                        "prop": "latest_update",
                        "ref": "$tool:get_order_status#/orders/0/financial_status",
                    },
                ],
            },
            store=store,
            allowlist=commerce_allowlist,
            components=render_ui_components(commerce_allowlist, True),
            op_id="root",
            flavor_groups=["commerce"],
        )
        assert outcome.decision != "rendered"
        assert "literal_field_not_bindable" in str(outcome.fn_result.get("error"))

    def test_bind_cannot_overwrite_verified_literal(
        self, page_text, commerce_allowlist
    ):
        """A bind on a literal field must fail even when a verified value
        for the same field rides `fields` — no silent overwrite path."""
        outcome = execute_render_ui(
            {
                "component": "OrderStatus",
                "bind": [
                    {"prop": "order", "ref": "$tool:get_order_status#/orders/0"},
                    {
                        "prop": "latest_update",
                        "ref": "$tool:get_order_status#/orders/0/financial_status",
                    },
                ],
                "fields": [{"name": "latest_update", "value": GOOD_UPDATE}],
            },
            store=_full_store(page_text),
            allowlist=commerce_allowlist,
            components=render_ui_components(commerce_allowlist, True),
            op_id="root",
            flavor_groups=["commerce"],
        )
        assert outcome.decision != "rendered"
        assert "literal_field_not_bindable" in str(outcome.fn_result.get("error"))

    def test_no_flavor_pack_fails_closed(self, page_text, commerce_allowlist):
        outcome = execute_render_ui(
            {
                "component": "OrderStatus",
                "bind": [{"prop": "order", "ref": "$tool:get_order_status#/orders/0"}],
                "fields": [{"name": "latest_update", "value": GOOD_UPDATE}],
            },
            store=_full_store(page_text),
            allowlist=commerce_allowlist,
            components=["OrderStatus"],
            op_id="root",
            flavor_groups=[],  # no pack registered for this scope
        )
        assert outcome.decision == "rendered"
        assert "latest_update" not in outcome.ops[0]["props"]
        assert outcome.fn_result["dropped_fields"] == {"latest_update": "no_verifier"}

    def test_fields_arg_advertised_only_with_wismo_component(self, commerce_allowlist):
        with_wismo = build_render_ui_schema(
            render_ui_components(commerce_allowlist, True),
            None,
            flavor_groups=["commerce"],
        )
        assert "fields" in with_wismo.properties
        names = with_wismo.properties["fields"]["items"]["properties"]["name"]["enum"]
        assert set(names) == {"eta_display", "latest_update", "updates"}
        without = build_render_ui_schema(
            ["ProductGrid", "CartView"], None, flavor_groups=["commerce"]
        )
        assert "fields" not in without.properties

    def test_disabled_component_leaves_no_trace_in_schema(self):
        """disabled_primitives:["OrderStatus"] must scrub the component from
        EVERY LLM-facing schema surface — enum, `fields` arg, bind coaching."""
        schema = build_render_ui_schema(
            ["ProductGrid", "CartView"], None, flavor_groups=["commerce"]
        )
        assert "OrderStatus" not in json.dumps(schema.properties)

    def test_offered_component_bind_coaching_present(self, commerce_allowlist):
        schema = build_render_ui_schema(
            render_ui_components(commerce_allowlist, True),
            None,
            flavor_groups=["commerce"],
        )
        bind_desc = schema.properties["bind"]["description"]
        assert "OrderStatus binds" in bind_desc
        assert "$tool:get_order_status#/orders/0" in bind_desc
        assert "CartView binds" in bind_desc
        assert "ProductGrid binds" in bind_desc

    def test_bind_coaching_gates_every_component_not_just_wismo(self):
        schema = build_render_ui_schema(
            ["ProductGrid", "OrderStatus"], None, flavor_groups=["commerce"]
        )
        bind_desc = schema.properties["bind"]["description"]
        assert "CartView" not in bind_desc
        assert "OrderStatus binds" in bind_desc


# ---------------------------------------------------------------------------
# Text-channel bypass — literal fields are render_ui-only
# ---------------------------------------------------------------------------


class TestRepeatRenderMerge:
    """The live Gemini pattern: card painted right after get_order_status
    (forced think-step), re-rendered after read_page_content with the
    transcribed fields. The second render must REPLACE the first — one
    card per turn (the user saw two stacked cards before this policy)."""

    def _props(self, **extra):
        base = OrderStatus.model_validate({"order": FULFILLED, **extra})
        return base.model_dump(exclude_none=True)

    def test_second_render_replaces_and_eta_lifts_headline(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.render_ui import (
            _merge_repeat_commerce,
        )

        first = self._props()
        second = self._props(eta_display="Saturday, 29 August")
        result = _merge_repeat_commerce("OrderStatus", first, second)
        assert result is not None
        merged, note = result
        assert merged["eta_display"] == "Saturday, 29 August"
        assert merged["headline"] == "Arriving Saturday, 29 August"
        assert merged["status_key"] == "in_transit"
        assert "one card per turn" in note

    def test_prior_enrichment_survives_a_field_less_rerender(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.render_ui import (
            _merge_repeat_commerce,
        )

        first = self._props(latest_update=GOOD_UPDATE)
        second = self._props()
        result = _merge_repeat_commerce("OrderStatus", first, second)
        assert result is not None
        merged, _ = result
        assert merged["latest_update"] == GOOD_UPDATE

    def test_non_commerce_components_still_stack(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.render_ui import (
            _merge_repeat_commerce,
        )

        assert _merge_repeat_commerce("LinkButton", {}, {}) is None


class TestTextChannelGuard:
    def test_show_op_with_literal_prop_drops(self, commerce_allowlist):
        line = (
            '{"op":"show","id":"root","component":"OrderStatus",'
            '"bind":{"order":"$tool:get_order_status#/orders/0"},'
            '"props":{"eta_display":"hacked date"}}'
        )
        result = parse_op_line(line, allowlist=commerce_allowlist)
        assert result.error == "literal_field_requires_render_ui:eta_display"

    def test_show_op_without_literal_props_still_parses(self, commerce_allowlist):
        line = (
            '{"op":"show","id":"root","component":"OrderStatus",'
            '"bind":{"order":"$tool:get_order_status#/orders/0"}}'
        )
        result = parse_op_line(line, allowlist=commerce_allowlist)
        assert result.error is None
        assert result.op is not None and result.op["op"] == "show"

    def test_show_op_bind_to_literal_field_drops_at_resolve(self, commerce_allowlist):
        from app.ai.voice.agents.breeze_buddy.chat.ui.binding import resolve_show_op

        op = {
            "op": "show",
            "id": "root",
            "component": "OrderStatus",
            "bind": {
                "order": "$tool:get_order_status#/orders/0",
                "eta_display": "$tool:get_order_status#/orders/0/note",
            },
        }
        result = resolve_show_op(op, BindingStore(), commerce_allowlist, ["commerce"])
        assert result.error == "literal_field_not_bindable:eta_display"

    def test_disabled_primitives_removes_order_status(self):
        allow = resolve_allowlist(
            enabled_groups=["core", "commerce"],
            disabled_primitives=["OrderStatus"],
        )
        assert "OrderStatus" not in allow
        assert "OrderStatus" not in render_ui_components(allow, True)
