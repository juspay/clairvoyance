#!/usr/bin/env python3
"""Build the DB-format flipkart tool_based template for qwen_harness.

Chains eleven_v3/make_tool_based_flipkart.py (v5 → tool_based wrapper with
hi/hi_std/en selector variants, kept as a scratch intermediate), then:

- splits every selector function into per-language functions
  (say_x_hi / say_x_hi_std / say_x_en) — the name alone determines the
  spoken line, no ``language`` arg anywhere;
- rewrites the role's language instructions for variant picking;
- restores the v5 update_outcome_in_database hooks on the six outcomes;
- wraps everything in the full prod envelope (rnr / test-shopify, telephony
  ids, qwen llm_configurations with tool_choice=required).

Output: qwen_harness/flipkart-recovery-toolbased.json — directly usable as
a create-template payload against a local server.
"""

import json
import runpy
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INTERMEDIATE = REPO / "eleven_v3" / "flipkart-recovery-toolbased.json"
OUT = Path(__file__).resolve().parent / "flipkart-recovery-toolbased.json"

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

# v5 outcome contracts (mirrored from flipkart-recovery-v5.json)
OUTCOME_HOOKS = {
    "app_return_committed": (
        "APP_NUDGE_ACCEPTED",
        ["committed_step", "completion_timeline"],
    ),
    "not_interested": ("NOT_INTERESTED", ["drop_off_reason"]),
    "customer_stuck": ("ISSUE_REPORTED", ["issue_description"]),
    "already_completed": ("ALREADY_DONE", ["claimed_step"]),
    "user_busy": ("BUSY", ["callback_note"]),
    "wrong_person": ("WRONG_PERSON", []),
}


def split_function(fn: dict) -> list[dict]:
    """One function per language key; the name alone determines speech."""
    say = fn["say"]
    variants = []
    for lang, lines in say["utterances"].items():
        variant = {
            **fn,
            "function_name": f"{fn['function_name']}_{lang}",
            "properties": {
                k: v for k, v in fn.get("properties", {}).items() if k != "language"
            },
            "required": [r for r in fn.get("required", []) if r != "language"],
            "say": {
                "utterances": {lang: lines},
                "phrasing": say.get("phrasing", "first"),
                "default_language": lang,
                **({"end_call": True} if say.get("end_call") else {}),
            },
        }
        variants.append(variant)
    return variants


def outcome_hook(outcome_value: str, llm_fields: list[str]) -> list[dict]:
    expected = {"outcome": {"value": outcome_value, "source": "static"}}
    for field in llm_fields:
        expected[field] = {"source": "llm"}
    expected["unsupported_language_code"] = {"source": "llm"}
    return [{"name": "update_outcome_in_database", "expected_fields": expected}]


runpy.run_path(str(REPO / "eleven_v3" / "make_tool_based_flipkart.py"))
src = json.loads(INTERMEDIATE.read_text())

node = src["flow"]["nodes"][0]
functions: list[dict] = []
for fn in node["functions"]:
    base = fn["function_name"]
    hook_spec = OUTCOME_HOOKS.get(base)
    if hook_spec:
        fn["hooks"] = outcome_hook(*hook_spec)
    functions.extend(split_function(fn))
node["functions"] = functions

# Split-mode description patches: the hi/hi_std pick and the guidance-entry
# gate must live ON the tools, where the model reads them.
_SELECTOR_BASES = [
    "say_hook_offered",
    "say_hook_eligibility_checked",
    "say_hook_kyc_completed",
    "say_hook_mandate_completed",
    "say_hook_agreement_signed",
    "say_card_no_cost",
    "say_card_monthly_emi",
]
_SELECTOR_HINT = (
    " Call the _hi variant only when {no_cost_emi_applicable} is yes; when "
    "it is no, call _hi_std (the standard-EMI wording) instead."
)
_STEP_A_NOTE = (
    " Guidance starts ONLY after the customer commits to doing it now or "
    "asks how — after a card followed by a mere 'ok/ठीक है', the move is "
    "say_return_to_app, never this."
)
for fn in functions:
    base = fn["function_name"].rsplit("_", 1)[0]
    variant = fn["function_name"].rsplit("_", 1)[1]
    if base in _SELECTOR_BASES and variant in ("hi", "hi_std"):
        fn["description"] += _SELECTOR_HINT
    elif base == "say_step_open_app":
        fn["description"] += _STEP_A_NOTE
    elif base == "user_busy":
        fn["description"] += (
            " NOT for a commitment to do it later ('बाद में कर लूँगा शाम को', "
            "'कल कर दूँगा') — that is app_return_committed with "
            "completion_timeline; this tool is only for cannot-talk-now / "
            "angry / stop-calling."
        )
    elif base == "not_interested":
        fn["description"] += (
            " A refusal to give any reason, or 'कोई खास वजह नहीं / nothing "
            "specific', completes the reason step with no matching card — "
            "come straight here instead of re-asking or looping on cards."
        )
    elif base.startswith("say_hook_"):
        fn["description"] += (
            " Call ONCE only — never replay it: a hesitation or no in reply "
            "(सोचता हूँ, मंगा लूँगा बाद में, nahi) goes to the matching card, "
            "not this hook again."
        )

role = node["role_messages"][0]["content"]
old = "Select hi vs hi_std by {no_cost_emi_applicable}."
new = (
    "Pick the _hi vs _hi_std VARIANT of the hook/card tool by "
    "{no_cost_emi_applicable} (both are Hindi; hi_std is the standard-EMI "
    "wording)."
)
assert old in role, "flipkart role drifted: hi/hi_std selector line missing"
role = role.replace(old, new)
old = "say_screening_intro (language=en)"
assert old in role, "flipkart role drifted: screener language line missing"
role = role.replace(old, "say_screening_intro_en")
first_para, rest = role.split("\n\n", 1)
role = (
    first_para + "\n\nLANGUAGE PICK: every tool name ends in its language — _hi and "
    "_hi_std are Hindi, _en is English. Call the variant matching the "
    "language the customer is speaking right now; English replies → _en, "
    "Hindi → _hi (or _hi_std where that variant exists). Never mix "
    "variants in one turn." + "\n\n" + rest
)
node["role_messages"][0]["content"] = role

cfg = src["configurations"]
template = {
    "id": "b05b3e6c-8f7d-4c2e-ad4f-7e8f9a0b1c25",
    "reseller_id": "rnr",
    "merchant_id": "test-shopify",
    "name": "flipkart-recovery-toolbased",
    "is_active": True,
    "supported_channels": ["voice"],
    "secrets": None,
    "telephony_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "outbound_number_id": "6f3c6720-b38f-4b76-ba65-adaf79eec863",
    "description": (
        "tool_based flipkart EMI-recovery template (DB format, qwen, split "
        "per language): every spoken line is a say block on a language-"
        "suffixed function (_hi/_hi_std/_en — no language arg, the name "
        "alone determines speech); the state hooks, card scripts, guidance "
        "ladder and six v5 outcomes all speak their own lines, and the "
        "outcomes end the call and record their v5 outcome contracts."
    ),
    "expected_payload_schema": src["expected_payload_schema"],
    "expected_callback_response_schema": {
        "committed_step": {"type": "string", "optional": True},
        "completion_timeline": {"type": "string", "optional": True},
        "drop_off_reason": {"type": "string", "optional": True},
        "issue_description": {"type": "string", "optional": True},
        "claimed_step": {"type": "string", "optional": True},
        "callback_note": {"type": "string", "optional": True},
        "unsupported_language_code": {"type": "string", "optional": True},
    },
    "configurations": {
        "llm_configurations": QWEN_LLM,
        # Bilingual hi/en callers (the _en hooks exist for them): both hints
        # so Soniox doesn't force one script on code-mixed speech. timeout
        # turn detection keeps the turn open across mid-sentence finals —
        # stt_native answered fragments (observed live 2026-09-06).
        "stt_configuration": {
            "provider": "soniox",
            "language": "hi,en",
            "turn_detection": "timeout",
            "user_speech_timeout": 3.0,
        },
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
print(f"wrote {OUT} ({OUT.stat().st_size} bytes, " f"{len(functions)} split functions)")
