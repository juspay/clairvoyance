#!/usr/bin/env python3
"""Build the DB-format redbus tool_based template for qwen_harness.

Chains eleven_v3/make_tool_based_redbus.py (v4 → tool_based wrapper, kept as
a scratch intermediate), then wraps its flow in the full prod envelope:
rnr / test-shopify, telephony ids, qwen llm_configurations with
tool_choice=required, and the update_outcome_in_database hooks from the prod
redbus template (POSITIVE_FEEDBACK / NEGATIVE_FEEDBACK / BUSY with rating +
feedback sourced from the llm).

Output: qwen_harness/redbus-customer-trip-feedback-toolbased.json — directly
usable as a create-template payload against a local server.
"""

import json
import runpy
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INTERMEDIATE = REPO / "eleven_v3" / "redbus-toolbased.json"
OUT = Path(__file__).resolve().parent / "redbus-customer-trip-feedback-toolbased.json"

QWEN_LLM = {
    "provider": "openai",
    "sdk": None,
    "model": "qwen3.8-27b-4bit",
    "region": None,
    "endpoint": "http://34.126.104.99:8000/v1",
    "api_key_name": "BREEZE_LLM_API_KEY",
    "temperature": 0.2,
    "max_tokens": 500,
    "thinking": {"enabled": False},
    "function_call_timeout_secs": None,
    "realtime": None,
    "tool_choice": "required",
    "prefill_system_prompt": True,
}


def outcome_hook(outcome_value: str, llm_fields: list[str]) -> list[dict]:
    expected = {"outcome": {"value": outcome_value, "source": "static"}}
    for field in llm_fields:
        expected[field] = {"source": "llm"}
    return [{"name": "update_outcome_in_database", "expected_fields": expected}]


runpy.run_path(str(REPO / "eleven_v3" / "make_tool_based_redbus.py"))
src = json.loads(INTERMEDIATE.read_text())

functions = src["flow"]["nodes"][0]["functions"]
for fn in functions:
    if fn["function_name"] == "positive_feedback":
        fn["hooks"] = outcome_hook("POSITIVE_FEEDBACK", ["rating", "feedback"])
    elif fn["function_name"] == "negative_feedback":
        fn["hooks"] = outcome_hook("NEGATIVE_FEEDBACK", ["rating", "feedback"])
    elif fn["function_name"] == "user_busy":
        fn["hooks"] = outcome_hook("BUSY", [])

cfg = src["configurations"]
template = {
    "id": "af4a2d5b-7e6c-4b1d-9c3e-6d7e8f9a0b14",
    "reseller_id": "rnr",
    "merchant_id": "test-shopify",
    "name": "redbus-customer-trip-feedback-toolbased",
    "is_active": True,
    "supported_channels": ["voice"],
    "secrets": None,
    "telephony_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "outbound_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "description": (
        "tool_based redbus trip-feedback template (DB format, qwen): every "
        "script is a say block on a Hindi-only function (name determines "
        "speech), the three outcomes speak their closings, end the call and "
        "record POSITIVE_FEEDBACK / NEGATIVE_FEEDBACK / BUSY with the spoken "
        "rating digit + verbatim feedback."
    ),
    "expected_payload_schema": src["expected_payload_schema"],
    "expected_callback_response_schema": {
        "rating": {"type": "integer", "optional": True},
        "feedback": {"type": "string", "optional": True},
    },
    "configurations": {
        "llm_configurations": QWEN_LLM,
        "tts_configuration": cfg["tts_configuration"],
        "initial_greeting": cfg["initial_greeting"],
        "user_idle_configuration": cfg["user_idle_configuration"],
        "dial_tone": True,
        "enable_background_sound": False,
        "background_sound_volume": 2,
        "enable_text_input": True,
        "interruption": {"mode": "enabled", "min_words": 1},
        "observers": None,
    },
    "flow": src["flow"],
}

OUT.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")
INTERMEDIATE.unlink()  # only the DB-format final ships
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
