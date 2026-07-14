"""Tests for JIT (Just-in-Time) UI instruction injection into MCP tool
responses. Covers each ``ToolUiTrigger`` value end-to-end.

The injection logic now lives in the shared result pipeline
(``handlers/transport/utils/tool_pipeline.py``) and is reached, for the
MCP-shaped JSON-string payload, via ``apply_result_pipeline_json_str`` — the
same call the MCP tool handlers make. These tests exercise that path."""

from __future__ import annotations

import json

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.tool_pipeline import (
    apply_result_pipeline_json_str,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ToolUiExample,
    ToolUiHint,
    ToolUiTrigger,
)


def _wrap(payload: dict) -> str:
    return json.dumps(payload)


def test_jit_inject_on_success_appends_instructions():
    hint = ToolUiHint(
        trigger=ToolUiTrigger.ON_SUCCESS,
        instructions="Render as a Carousel of Cards.",
    )
    result = _wrap({"products": [{"id": "p1", "title": "Snowboard"}]})
    out = apply_result_pipeline_json_str(
        result, tool_name="search_catalog", ui_hint=hint
    )
    parsed = json.loads(out)
    assert parsed["_ui_instructions"] == "Render as a Carousel of Cards."
    assert parsed["products"][0]["id"] == "p1"


def test_jit_inject_appends_examples_when_present():
    hint = ToolUiHint(
        trigger=ToolUiTrigger.ON_SUCCESS,
        instructions="Render a Card.",
        examples=[
            ToolUiExample(
                scenario="single product",
                input_sketch="{product: {id, title}}",
                expected_jsonl=[
                    {"op": "add", "id": "root", "type": "Card"},
                ],
            )
        ],
    )
    result = _wrap({"product": {"id": "p1"}})
    out = apply_result_pipeline_json_str(
        result, tool_name="get_product_details", ui_hint=hint
    )
    parsed = json.loads(out)
    assert isinstance(parsed["_ui_examples"], list)
    assert parsed["_ui_examples"][0]["scenario"] == "single product"
    assert parsed["_ui_examples"][0]["expected_jsonl"][0]["type"] == "Card"


def test_jit_skip_ui_sets_flag_and_omits_instructions():
    hint = ToolUiHint(
        trigger=ToolUiTrigger.SKIP_UI,
        instructions="(unused)",
    )
    result = _wrap({"answer": "Our return policy is 30 days."})
    out = apply_result_pipeline_json_str(
        result, tool_name="search_shop_policies_and_faqs", ui_hint=hint
    )
    parsed = json.loads(out)
    assert parsed["_ui_skip"] is True
    assert "_ui_instructions" not in parsed


def test_jit_no_hint_passes_through_unchanged():
    result = _wrap({"products": []})
    out = apply_result_pipeline_json_str(
        result, tool_name="search_catalog", ui_hint=None
    )
    assert out == result


def test_jit_inject_no_op_on_non_object_payload():
    # Lists and primitives don't receive a UI hint (only dicts do); the LLM
    # gets the structure back semantically unchanged.
    list_payload = json.dumps([{"id": 1}, {"id": 2}])
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="x")
    out = apply_result_pipeline_json_str(list_payload, tool_name="tool", ui_hint=hint)
    assert json.loads(out) == [{"id": 1}, {"id": 2}]
    assert "_ui_instructions" not in out


def test_jit_inject_handles_non_json_string_passthrough():
    # Plain text tool results pass through (no _ui_instructions appended).
    plain = "Sorry, no results."
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="x")
    assert (
        apply_result_pipeline_json_str(plain, tool_name="tool", ui_hint=hint) == plain
    )
