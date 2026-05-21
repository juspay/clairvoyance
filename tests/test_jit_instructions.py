"""Tests for JIT (Just-in-Time) UI instruction injection into MCP tool
responses. Covers each ``ToolUiTrigger`` value end-to-end."""

from __future__ import annotations

import json

from app.ai.voice.agents.breeze_buddy.mcp import _maybe_inject_ui_instructions
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
    out = _maybe_inject_ui_instructions(result, hint, "search_catalog")
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
    out = _maybe_inject_ui_instructions(result, hint, "get_product_details")
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
    out = _maybe_inject_ui_instructions(result, hint, "search_shop_policies_and_faqs")
    parsed = json.loads(out)
    assert parsed["_ui_skip"] is True
    assert "_ui_instructions" not in parsed


def test_jit_no_hint_passes_through_unchanged():
    result = _wrap({"products": []})
    out = _maybe_inject_ui_instructions(result, None, "search_catalog")
    assert out == result


def test_jit_inject_no_op_on_non_object_payload():
    # Lists and primitives pass through; the LLM gets the raw structure.
    list_payload = json.dumps([{"id": 1}, {"id": 2}])
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="x")
    assert _maybe_inject_ui_instructions(list_payload, hint, "tool") == list_payload


def test_jit_inject_handles_non_json_string_passthrough():
    # Plain text tool results pass through (no _ui_instructions appended).
    plain = "Sorry, no results."
    hint = ToolUiHint(trigger=ToolUiTrigger.ON_SUCCESS, instructions="x")
    assert _maybe_inject_ui_instructions(plain, hint, "tool") == plain
