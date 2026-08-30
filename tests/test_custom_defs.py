"""CHAMELEON custom-component registry: guards + hydration + LLM surface.

Covers the pure halves of the feature (no DB, no agent):
- registration guards (name, schema, flags, render_def lint)
- resolve_custom_show_op (pointer walk, object lift, selection, caps,
  JSON-Schema validation with structural-only errors)
- render_ui enum joining + execute routing (two-merchant isolation lives
  here too: a def not passed in is simply unknown)
"""

from typing import Any, Dict

import pytest

from app.ai.voice.agents.breeze_buddy.chat.ui.binding import BindingStore
from app.ai.voice.agents.breeze_buddy.chat.ui.custom_defs import (
    lint_render_def,
    resolve_custom_show_op,
    summarize_custom_render,
    validate_registration,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.render_ui_tool import (
    execute_render_ui,
    render_ui_components,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    CustomComponentDef,
    CustomComponentFlags,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

JOURNEY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "journeys": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "summary": {"type": "string"},
                    "duration_label": {"type": "string"},
                    "min_fare_label": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    "required": ["journeys"],
}


def journey_def(**flag_overrides) -> CustomComponentDef:
    flags = {
        "data_bound": True,
        "selection_field": "items",
        "list_props": ["journeys"],
        "max_items_default": 4,
        "max_items_limit": 8,
        **flag_overrides,
    }
    return CustomComponentDef(
        name="JourneyOptions",
        version=1,
        props_schema=JOURNEY_SCHEMA,
        flags=CustomComponentFlags.model_validate(flags),
        render_def={"type": "col", "children": []},
        prompt_hint="Bind journeys to $tool:search_journeys#/journeys",
    )


def store_with(tool: str, payload: Any) -> BindingStore:
    store = BindingStore()
    assert store.record(tool, "t1", payload)
    return store


JOURNEYS = [
    {"id": "j1", "summary": "Metro via Alandur", "duration_label": "51 min"},
    {"id": "j2", "summary": "Bus 570", "duration_label": "64 min"},
    {"id": "j3", "summary": "Metro + walk", "duration_label": "58 min"},
]


# ---------------------------------------------------------------------------
# Registration guards
# ---------------------------------------------------------------------------


class TestRegistrationGuards:
    def test_clean_registration_passes(self):
        assert (
            validate_registration(
                name="JourneyOptions",
                props_schema=JOURNEY_SCHEMA,
                flags={"data_bound": True},
                render_def={"type": "col", "children": [{"type": "text"}]},
            )
            == []
        )

    def test_overlay_only_requires_render_def(self):
        errors = validate_registration(
            name="JourneyDetail",
            props_schema=JOURNEY_SCHEMA,
            flags={"data_bound": True, "overlay_only": True},
            render_def=None,
        )
        assert any("overlay_only requires a render_def" in e for e in errors)

    def test_model_renderable_filters_overlay_only(self):
        from app.ai.voice.agents.breeze_buddy.chat.custom_components import (
            model_renderable,
        )
        from app.ai.voice.agents.breeze_buddy.template.types import (
            CustomComponentDef,
            CustomComponentFlags,
        )

        defs = {
            "JourneyOptions": CustomComponentDef(
                name="JourneyOptions", props_schema={}, render_def={"type": "col"}
            ),
            "JourneyDetail": CustomComponentDef(
                name="JourneyDetail",
                props_schema={},
                render_def={"type": "col"},
                flags=CustomComponentFlags(overlay_only=True),
            ),
        }
        assert set(model_renderable(defs)) == {"JourneyOptions"}

    @pytest.mark.parametrize("bad", ["journeyOptions", "J", "Has Spaces", "x" * 70])
    def test_name_must_be_pascal_case(self, bad):
        errors = validate_registration(
            name=bad, props_schema={}, flags={}, render_def=None
        )
        assert any("PascalCase" in e for e in errors)

    def test_builtin_collision_rejected(self):
        for taken in ("Card", "ProductGrid", "QuickReplies"):
            errors = validate_registration(
                name=taken, props_schema={}, flags={}, render_def=None
            )
            assert any("collides" in e for e in errors), taken

    def test_invalid_json_schema_rejected(self):
        errors = validate_registration(
            name="GoodName",
            props_schema={"type": "not-a-type"},
            flags={},
            render_def=None,
        )
        assert any("JSON Schema" in e for e in errors)

    def test_literal_fields_rejected(self):
        errors = validate_registration(
            name="GoodName",
            props_schema={},
            flags={"literal_fields": ["eta"]},
            render_def=None,
        )
        assert any("literal_fields" in e for e in errors)

    def test_data_bound_false_rejected(self):
        errors = validate_registration(
            name="GoodName",
            props_schema={},
            flags={"data_bound": False},
            render_def=None,
        )
        assert any("data_bound" in e for e in errors)


class TestRenderDefLint:
    def test_unknown_node_type(self):
        assert any(
            "unknown node type" in e
            for e in lint_render_def({"type": "script", "children": []})
        )

    def test_depth_cap(self):
        node: Dict[str, Any] = {"type": "text"}
        for _ in range(10):
            node = {"type": "box", "children": [node]}
        assert any("depth" in e for e in lint_render_def(node))

    def test_static_open_url_must_be_https(self):
        bad = {
            "type": "button",
            "props": {
                "label": "Go",
                "action": {"type": "open_url", "url": "http://x.example/y"},
            },
        }
        assert any("https" in e for e in lint_render_def(bad))
        good = dict(
            bad,
            props={
                "label": "Go",
                "action": {"type": "open_url", "url": "https://x.example/y"},
            },
        )
        assert lint_render_def(good) == []

    def test_bound_open_url_allowed(self):
        node = {
            "type": "button",
            "props": {
                "label": "Ticket",
                "action": {"type": "open_url", "url": "{$props.ticket_url}"},
            },
        }
        assert lint_render_def(node) == []

    def test_open_detail_component_must_be_static_pascal(self):
        bad = {
            "type": "button",
            "props": {
                "label": "View details",
                "action": {"type": "open_detail", "component": "{$props.c}"},
            },
        }
        assert any("PascalCase" in e for e in lint_render_def(bad))
        good = {
            "type": "button",
            "props": {
                "label": "View details",
                "action": {
                    "type": "open_detail",
                    "component": "JourneyDetail",
                    "title": "Your journey",
                    "props": {"journey_id": "{$j.id}", "legs": "$j.legs"},
                },
            },
        }
        assert lint_render_def(good) == []

    def test_open_detail_props_must_be_object(self):
        bad = {
            "type": "button",
            "props": {
                "label": "x",
                "action": {
                    "type": "open_detail",
                    "component": "JourneyDetail",
                    "props": ["not", "an", "object"],
                },
            },
        }
        assert any("props must be an object" in e for e in lint_render_def(bad))

    def test_repeat_binding_syntax(self):
        bad = {"type": "box", "repeat": {"in": "journeys", "as": "j"}}
        assert any("repeat.in" in e for e in lint_render_def(bad))
        good = {"type": "box", "repeat": {"in": "$props.journeys", "as": "j"}}
        assert lint_render_def(good) == []

    def test_to_assistant_needs_msg(self):
        node = {
            "type": "button",
            "props": {"label": "Pick", "action": {"type": "to_assistant"}},
        }
        assert any("msg" in e for e in lint_render_def(node))


# ---------------------------------------------------------------------------
# Hydration
# ---------------------------------------------------------------------------


def _ok(result) -> Dict[str, Any]:
    assert result.error is None, result.error
    assert result.op is not None
    return result.op


def _err(result) -> str:
    assert result.op is None
    assert result.error is not None
    return result.error


class TestResolveCustomShowOp:
    def _op(self, **overrides) -> Dict[str, Any]:
        op = {
            "op": "show",
            "id": "root",
            "component": "JourneyOptions",
            "bind": {"journeys": "$tool:search_journeys#/journeys"},
            "props": {},
        }
        op.update(overrides)
        return op

    def test_happy_path_hydrates(self):
        store = store_with("search_journeys", {"journeys": JOURNEYS})
        op = _ok(resolve_custom_show_op(self._op(), store, journey_def()))
        assert op["type"] == "JourneyOptions"
        assert op["v"] == 2
        assert [j["id"] for j in op["props"]["journeys"]] == ["j1", "j2", "j3"]

    def test_missing_tool_drops(self):
        error = _err(resolve_custom_show_op(self._op(), BindingStore(), journey_def()))
        assert error.startswith("bind_unresolved:")

    def test_selection_order_respected(self):
        store = store_with("search_journeys", {"journeys": JOURNEYS})
        show = self._op(props={"items": [{"id": "j3"}, {"id": "j1"}]})
        op = _ok(resolve_custom_show_op(show, store, journey_def()))
        assert [j["id"] for j in op["props"]["journeys"]] == ["j3", "j1"]
        assert "items" not in op["props"]

    def test_mangled_selection_fails_open(self):
        store = store_with("search_journeys", {"journeys": JOURNEYS})
        show = self._op(props={"items": [{"id": "nope"}]})
        op = _ok(resolve_custom_show_op(show, store, journey_def()))
        assert len(op["props"]["journeys"]) == 3

    def test_caps_apply(self):
        many = [{"id": f"j{i}", "summary": "s"} for i in range(12)]
        store = store_with("search_journeys", {"journeys": many})
        op = _ok(resolve_custom_show_op(self._op(), store, journey_def()))
        assert len(op["props"]["journeys"]) == 4  # max_items_default
        show = self._op(props={"max_items": 20})
        op = _ok(resolve_custom_show_op(show, store, journey_def()))
        assert len(op["props"]["journeys"]) == 8  # max_items_limit
        assert "max_items" not in op["props"]

    def test_single_object_lifts_to_list(self):
        store = store_with("search_journeys", {"journeys": JOURNEYS[0]})
        op = _ok(resolve_custom_show_op(self._op(), store, journey_def()))
        assert [j["id"] for j in op["props"]["journeys"]] == ["j1"]

    def test_schema_validation_structural_only(self):
        store = store_with("search_journeys", {"journeys": [{"summary": 42}]})
        error = _err(resolve_custom_show_op(self._op(), store, journey_def()))
        assert error.startswith("bind_validation_failed:JourneyOptions:")
        assert "42" not in error  # never echo values

    def test_summary_echoes_referents(self):
        store = store_with("search_journeys", {"journeys": JOURNEYS})
        op = _ok(resolve_custom_show_op(self._op(), store, journey_def()))
        summary = summarize_custom_render(journey_def(), op["props"])
        assert summary["rendered"] == "JourneyOptions"
        assert summary["count"] == 3
        assert summary["journeys"][0]["id"] == "j1"


# ---------------------------------------------------------------------------
# LLM surface: enum joining + execute routing + isolation
# ---------------------------------------------------------------------------


class TestRenderUiSurface:
    def test_custom_names_join_enum_v2_only(self):
        allow = {"QuickReplies", "JourneyOptions"}
        assert "JourneyOptions" in render_ui_components(
            allow, True, custom_components={"JourneyOptions"}
        )
        # v1 session: customs pruned along with every data-bound component
        assert "JourneyOptions" not in render_ui_components(
            allow, False, custom_components={"JourneyOptions"}
        )

    def test_execute_routes_custom(self):
        store = store_with("search_journeys", {"journeys": JOURNEYS})
        defs = {"JourneyOptions": journey_def()}
        outcome = execute_render_ui(
            {
                "component": "JourneyOptions",
                "bind": [
                    {"prop": "journeys", "ref": "$tool:search_journeys#/journeys"}
                ],
            },
            store=store,
            allowlist={"JourneyOptions"},
            components=["JourneyOptions"],
            op_id="root",
            custom_defs=defs,
        )
        assert outcome.decision == "rendered"
        assert outcome.ops[0]["type"] == "JourneyOptions"
        assert outcome.fn_result["count"] == 3

    def test_execute_requires_bind_for_custom(self):
        outcome = execute_render_ui(
            {"component": "JourneyOptions"},
            store=BindingStore(),
            allowlist={"JourneyOptions"},
            components=["JourneyOptions"],
            op_id="root",
            custom_defs={"JourneyOptions": journey_def()},
        )
        assert outcome.fn_result["status"] == "error"
        assert "data-bound" in outcome.fn_result["error"]

    def test_two_merchant_isolation(self):
        """A session without the def treats the component as unknown even
        when another session on the same worker carries it."""
        store = store_with("search_journeys", {"journeys": JOURNEYS})
        outcome = execute_render_ui(
            {
                "component": "JourneyOptions",
                "bind": [
                    {"prop": "journeys", "ref": "$tool:search_journeys#/journeys"}
                ],
            },
            store=store,
            allowlist={"QuickReplies"},
            components=["QuickReplies"],  # other merchant's enum
            op_id="root",
            custom_defs={},  # no overlay on THIS session
        )
        assert outcome.fn_result["status"] == "error"
        assert "unknown component" in outcome.fn_result["error"]
