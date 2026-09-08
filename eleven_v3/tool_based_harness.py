#!/usr/bin/env python3
"""Live simulation harness for tool_based flow mode.

Drives the example tool_based template
(app/ai/voice/agents/breeze_buddy/examples/templates/tool-based-example.json)
against a REAL LLM using the same protocol pipecat-flows uses:
  system role message + assistant greeting + per-node developer task messages
  + node tools, one tool call per user turn, APPEND context across nodes.

What it proves end-to-end (the whole point of the mode):
  1. Every assistant turn is EXACTLY ONE tool call — `content` is empty
     (tool_choice="required" + the injected rules section).
  2. The right tool is called for each persona step.
  3. `language` tracks the language the customer is speaking (incl. switches).
  4. Extracted `value` args carry what the customer spoke (digits).
  5. render_say produces the correct spoken line (real renderer, real vars).

Usage:
  ./.venv/bin/python eleven_v3/tool_based_harness.py [--llm azure|grid] [--repeat N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.ai.voice.agents.breeze_buddy.template.tool_speech import (  # noqa: E402
    TOOL_BASED_RULES_PROMPT,
    render_say,
    substitute_placeholders,
)
from app.ai.voice.agents.breeze_buddy.template.types import SayConfig  # noqa: E402
from eleven_v3.warm_harness import LLMClient  # noqa: E402

ENV = dotenv_values(REPO / ".env")

TEMPLATE_PATH = (
    REPO
    / "app"
    / "ai"
    / "voice"
    / "agents"
    / "breeze_buddy"
    / "examples"
    / "templates"
    / "tool-based-example.json"
)


class ToolBasedRunner:
    """Mirrors the pipecat-flows protocol for one simulated call."""

    def __init__(self, template: dict, llm: LLMClient, payload: dict):
        self.template_json = template
        self.flow = template["flow"]
        self.nodes = {n["node_name"]: n for n in self.flow["nodes"]}
        self.llm = llm
        self.payload = payload
        self.messages: list[dict] = []
        self.spoken: list[str] = []
        self.violations: list[str] = []
        self.tool_log: list[dict] = []
        self.latencies: list[float] = []
        self.call_ended = False

    def _render(self, text: str) -> str:
        """Substitute {payload_var} paths into prompt text — mirrors the real
        loader's template_vars rendering (the harness feeds the model the
        same prompt a live call would see). Uses the shared say-renderer
        grammar so nested payload JSON resolves here too
        ({items.first.name})."""
        rendered, _ = substitute_placeholders(text, {}, self.payload)
        return rendered

    def _enter_node(self, node_name: str) -> None:
        node = self.nodes[node_name]
        if node.get("role_messages"):
            # Mirrors the builder: authored role message + appended rules.
            role = self._render(node["role_messages"][-1]["content"])
            self.messages.append(
                {
                    "role": "system",
                    "content": role + "\n\n" + TOOL_BASED_RULES_PROMPT,
                }
            )
        for tm in node.get("task_messages", []):
            # breeze/qwen chat template has no developer role — mirror
            # pipecat's convert_developer_to_user for OpenAI-compat backends
            role = "user" if tm["role"] == "developer" else tm["role"]
            self.messages.append({"role": role, "content": self._render(tm["content"])})

    def _greet(self, language: str | None = None) -> None:
        greeting_cfg = self.template_json["configurations"]["initial_greeting"]
        if isinstance(greeting_cfg, dict):
            greeting = greeting_cfg.get(language or "hi") or next(
                iter(greeting_cfg.values())
            )
        else:
            greeting = greeting_cfg
            language = language or "hi"
        self.messages.append({"role": "assistant", "content": greeting})
        self.spoken.append(f"[greeting/{language}] {greeting}")

    async def _user_turn(
        self,
        text: str,
        expect_tool: str | None = None,
        expect_language: str | None = None,
        expect_value: str | None = None,
        speech_contains: str | None = None,
        ends_call: bool | None = None,
        expect_arg_contains: dict | None = None,
        expect_tool_any: list[str] | None = None,
        expect_arg_equals: dict | None = None,
    ) -> dict | None:
        self.messages.append({"role": "user", "content": text})
        node = self.nodes[self.current_node]
        tools = node.get("functions", [])
        t0 = time.perf_counter()
        resp = await self.llm.chat(
            self.llm_kind_client, self.messages, tools, tool_choice="required"
        )
        self.latencies.append(time.perf_counter() - t0)
        choice = resp["choices"][0]
        msg = choice["message"]

        prose = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []

        if prose:
            self.violations.append(f"MODEL PROSE (must never happen): {prose[:120]!r}")
        if len(calls) != 1:
            self.violations.append(
                f"expected exactly 1 tool call, got {len(calls)} "
                f"({[c['function']['name'] for c in calls]})"
            )
            return None

        call = calls[0]
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = None
            self.violations.append(f"unparseable tool args for {name}")

        fn = next((f for f in tools if f["function_name"] == name), None)
        self.tool_log.append(
            {"turn": len(self.tool_log) + 1, "name": name, "args": args}
        )

        # Split-language templates name the language into the function
        # (say_x_hi / say_x_en, no language arg). Personas stay authored
        # against the base name + expect_language; resolve accordingly.
        known = {f["function_name"] for f in tools}
        has_lang_arg = any("language" in f.get("properties", {}) for f in tools)

        def resolve(base: str) -> set[str]:
            if base in known:
                return {base}
            variants = {
                f"{base}_{suffix}"
                for suffix in ("hi", "hi_std", "en", "kn")
                if f"{base}_{suffix}" in known
            }
            if expect_language and f"{base}_{expect_language}" in variants:
                return {f"{base}_{expect_language}"}
            return variants or {base}

        expected_names = resolve(expect_tool) if expect_tool else set()
        expected_any = (
            {v for base in expect_tool_any for v in resolve(base)}
            if expect_tool_any
            else set()
        )

        if (
            expect_tool is not None
            and name not in expected_names
            and not (expected_any and name in expected_any)
        ):
            expected = f"one of {sorted(expected_any)}" if expected_any else expect_tool
            self.violations.append(
                f"after {text[:40]!r}: expected {expected}, got {name}"
            )
        # Language-arg check applies only when the CALLED function declares
        # it — split-language tools (_hi/_en twins) carry the language in
        # their name and must not be flagged for omitting the arg.
        fn_declares_language = fn is not None and "language" in fn.get("properties", {})
        if (
            expect_language is not None
            and fn_declares_language
            and isinstance(args, dict)
            and args.get("language") != expect_language
        ):
            self.violations.append(
                f"{name}: language={args.get('language')!r}, "
                f"expected {expect_language!r}"
            )
        if (
            expect_value is not None
            and isinstance(args, dict)
            and str(args.get("value", "")).strip() != expect_value
        ):
            self.violations.append(
                f"{name}: value={args.get('value')!r}, expected {expect_value!r}"
            )
        if expect_arg_equals and isinstance(args, dict):
            for arg_name, wanted in expect_arg_equals.items():
                if str(args.get(arg_name)).strip() != str(wanted):
                    self.violations.append(
                        f"{name}: {arg_name}={args.get(arg_name)!r}, "
                        f"expected {wanted!r}"
                    )
        if expect_arg_contains and isinstance(args, dict):
            for arg_name, needle in expect_arg_contains.items():
                got = str(args.get(arg_name) or "")
                if needle.lower() not in got.lower():
                    self.violations.append(
                        f"{name}: {arg_name}={got[:80]!r} missing {needle!r}"
                    )

        # Render the speech exactly like the handler does.
        if fn and fn.get("say"):
            say = SayConfig.model_validate(fn["say"])
            line, lang = render_say(say, args or {}, self.payload)
            self.spoken.append(f"[{name}/{lang}] {line}")
            if speech_contains and speech_contains not in line:
                self.violations.append(
                    f"{name}: speech {line!r} missing {speech_contains!r}"
                )
            if ends_call is not None and say.end_call != ends_call:
                self.violations.append(f"{name}: end_call={say.end_call}")
            if say.end_call:
                self.call_ended = True
        elif fn and expect_tool == name:
            self.violations.append(f"{name}: expected a say block, found none")

        # Append the assistant tool-call + acknowledgement result to context.
        self.messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call.get("id") or "call_1",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": call["function"].get("arguments") or "{}",
                        },
                    }
                ],
            }
        )
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id") or "call_1",
                "content": '{"status": "success"}',
            }
        )

        if fn and fn.get("transition_to"):
            self.current_node = fn["transition_to"]
            self._enter_node(self.current_node)
        return {"name": name, "args": args}

    @property
    def current_node(self) -> str:
        return self._current_node

    @current_node.setter
    def current_node(self, value: str) -> None:
        self._current_node = value

    # assigned by run()
    llm_kind_client: httpx.AsyncClient


async def run_persona(
    client: httpx.AsyncClient,
    llm: LLMClient,
    persona: dict,
    template: dict,
    default_payload: dict | None = None,
    runner_cls=ToolBasedRunner,
) -> dict:
    lang0 = persona.get("language", "hi")
    payload = persona.get("payload") or default_payload or {"customer_name": "राहुल"}
    runner = runner_cls(template, llm, payload)
    # breeze: one stable session id per call (radix-cache affinity)
    if hasattr(llm, "session_id"):
        llm.session_id = f"tbh-{persona['name']}-{uuid.uuid4().hex[:8]}"
    runner.llm_kind_client = client
    runner.current_node = template["flow"]["initial_node"]
    runner._enter_node(runner.current_node)
    if persona.get("greeting", True):
        runner._greet(lang0)

    for step in persona["steps"]:
        if runner.call_ended:
            if step is not persona["steps"][-1]:
                runner.violations.append(
                    f"call ended early; leftover step: {step.get('say', '')[:40]!r}"
                )
            break
        await runner._user_turn(step["say"], **step.get("expect", {}))

    if persona.get("expect_end") == "any":
        pass  # both endings legal for this persona
    elif persona.get("expect_end") and not runner.call_ended:
        runner.violations.append("expected the call to end, it did not")
    elif not persona.get("expect_end") and runner.call_ended:
        runner.violations.append("call ended unexpectedly")

    return {
        "persona": persona["name"],
        "ok": not runner.violations,
        "violations": runner.violations,
        "tools": [t["name"] for t in runner.tool_log],
        "spoken": runner.spoken,
        "latencies": [round(x, 3) for x in runner.latencies],
    }


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------

PERSONAS = [
    {
        "name": "hi_happy_path",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "नमस्ते जी",
                "expect": {"expect_tool": "say_thankyou", "expect_language": "hi"},
            },
            {
                "say": "मैं 34 साल का हूँ",
                "expect": {
                    "expect_tool": "save_response",
                    "expect_language": "hi",
                    "expect_value": "34",
                    "speech_contains": "34",
                },
            },
            {
                "say": "हाँ बिल्कुल सही है",
                "expect": {
                    "expect_tool": "confirm_details",
                    "expect_language": "hi",
                    "expect_value": "34",
                },
            },
            {
                "say": "ठीक है, धन्यवाद",
                "expect": {
                    "expect_tool": "say_goodbye",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "en_happy_path",
        "language": "en",
        "payload": {"customer_name": "Rahul"},
        "expect_end": True,
        "steps": [
            {
                "say": "hello",
                "expect": {"expect_tool": "say_thankyou", "expect_language": "en"},
            },
            {
                "say": "I am 42 years old",
                "expect": {
                    "expect_tool": "save_response",
                    "expect_language": "en",
                    "expect_value": "42",
                    "speech_contains": "42",
                },
            },
            {
                "say": "yes that's correct",
                "expect": {
                    "expect_tool": "confirm_details",
                    "expect_language": "en",
                    "expect_value": "42",
                },
            },
            {
                "say": "great, thanks",
                "expect": {
                    "expect_tool": "say_goodbye",
                    "expect_language": "en",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_age_in_words",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "हाँजी बोलिए",
                "expect": {"expect_tool": "say_thankyou", "expect_language": "hi"},
            },
            {
                "say": "पचपन साल",
                "expect": {
                    "expect_tool": "save_response",
                    "expect_value": "पचपन",
                    "speech_contains": "पचपन",
                },
            },
            {"say": "जी सही", "expect": {"expect_tool": "confirm_details"}},
            {"say": "ओके", "expect": {"expect_tool": "say_goodbye", "ends_call": True}},
        ],
    },
    {
        "name": "hi_correction_loop",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {"say": "नमस्ते", "expect": {"expect_tool": "say_thankyou"}},
            {
                "say": "48 साल",
                "expect": {"expect_tool": "save_response", "expect_value": "48"},
            },
            {
                "say": "नहीं नहीं, गलती हुई, मैं उनतालीस का हूँ",
                "expect": {
                    "expect_tool": "edit_response",
                    "expect_value": "उनतालीस",
                    "speech_contains": "उनतालीस",
                },
            },
            {"say": "हाँ अब सही है", "expect": {"expect_tool": "confirm_details"}},
            {
                "say": "धन्यवाद जी",
                "expect": {"expect_tool": "say_goodbye", "ends_call": True},
            },
        ],
    },
    {
        "name": "hi_busy_refusal",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "अभी ड्राइव कर रहा हूँ, बाद में कॉल करो",
                "expect": {
                    "expect_tool": "user_busy",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "en_busy_meeting",
        "language": "en",
        "payload": {"customer_name": "Rahul"},
        "expect_end": True,
        "steps": [
            {
                "say": "I'm in a meeting right now, call later",
                "expect": {
                    "expect_tool": "user_busy",
                    "expect_language": "en",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_didnt_understand",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {"say": "जी", "expect": {"expect_tool": "say_thankyou"}},
            {
                "say": "क्या? समझ नहीं आया, दोबारा बताइए",
                "expect": {"expect_tool": "repeat_question", "expect_language": "hi"},
            },
            {
                "say": "29 साल",
                "expect": {"expect_tool": "save_response", "expect_value": "29"},
            },
            {"say": "हाँ", "expect": {"expect_tool": "confirm_details"}},
            {
                "say": "ओके नमस्ते",
                "expect": {"expect_tool": "say_goodbye", "ends_call": True},
            },
        ],
    },
    {
        "name": "hi_switches_to_en_mid_call",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {
                "say": "नमस्ते",
                "expect": {"expect_tool": "say_thankyou", "expect_language": "hi"},
            },
            {
                "say": "actually let's talk in English. I am 31.",
                "expect": {
                    "expect_tool": "save_response",
                    "expect_language": "en",
                    "expect_value": "31",
                },
            },
            {
                "say": "yes correct",
                "expect": {"expect_tool": "confirm_details", "expect_language": "en"},
            },
            {
                "say": "thanks, bye",
                "expect": {
                    "expect_tool": "say_goodbye",
                    "expect_language": "en",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "en_switches_to_hi_mid_call",
        "language": "en",
        "payload": {"customer_name": "Rahul"},
        "expect_end": True,
        "steps": [
            {
                "say": "hi there",
                "expect": {"expect_tool": "say_thankyou", "expect_language": "en"},
            },
            {
                "say": "हिंदी में बात करें? मैं 41 साल का हूँ",
                "expect": {
                    "expect_tool": "save_response",
                    "expect_language": "hi",
                    "expect_value": "41",
                },
            },
            {
                "say": "हाँ सही",
                "expect": {"expect_tool": "confirm_details", "expect_language": "hi"},
            },
            {
                "say": "ठीक है",
                "expect": {
                    "expect_tool": "say_goodbye",
                    "expect_language": "hi",
                    "ends_call": True,
                },
            },
        ],
    },
    {
        "name": "hi_reopen_at_goodbye",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {"say": "नमस्ते जी", "expect": {"expect_tool": "say_thankyou"}},
            {
                "say": "47 साल",
                "expect": {"expect_tool": "save_response", "expect_value": "47"},
            },
            {"say": "हाँ दर्ज कर दीजिए", "expect": {"expect_tool": "confirm_details"}},
            {
                "say": "रुकिए! गलत बताया, असल में 48 है",
                "expect": {
                    "expect_tool": "reopen_conversation",
                    "expect_language": "hi",
                },
            },
            {
                "say": "48",
                "expect": {
                    "expect_tool": "edit_response",
                    "expect_value": "48",
                    "speech_contains": "48",
                },
            },
            {"say": "हाँ अब ठीक है", "expect": {"expect_tool": "confirm_details"}},
            {
                "say": "नमस्ते",
                "expect": {"expect_tool": "say_goodbye", "ends_call": True},
            },
        ],
    },
    {
        "name": "hi_silent_ish_non_answer",
        "language": "hi",
        "expect_end": True,
        "steps": [
            {"say": "हाँ", "expect": {"expect_tool": "say_thankyou"}},
            {"say": "अरे मैं... एक मिनट", "expect": {"expect_tool": "repeat_question"}},
            {
                "say": "पैंतालीस साल",
                "expect": {"expect_tool": "save_response", "expect_value": "पैंतालीस"},
            },
            {"say": "जी", "expect": {"expect_tool": "confirm_details"}},
            {"say": "हुँ", "expect": {"expect_tool": "say_goodbye", "ends_call": True}},
        ],
    },
    {
        "name": "en_jokey_deflection",
        "language": "en",
        "payload": {"customer_name": "Rahul"},
        "expect_end": True,
        "steps": [
            {
                "say": "hey",
                "expect": {"expect_tool": "say_thankyou", "expect_language": "en"},
            },
            {
                "say": "why do you want to know, haha",
                "expect": {"expect_tool": "repeat_question", "expect_language": "en"},
            },
            {
                "say": "alright, 36",
                "expect": {"expect_tool": "save_response", "expect_value": "36"},
            },
            {"say": "yep", "expect": {"expect_tool": "confirm_details"}},
            {
                "say": "bye now",
                "expect": {"expect_tool": "say_goodbye", "ends_call": True},
            },
        ],
    },
]


SUITES = {
    "example": ("eleven_v3", "tool_based_harness"),
    "redbus": ("eleven_v3", "tool_based_personas_redbus"),
    "flipkart": ("eleven_v3", "tool_based_personas_flipkart"),
    "flipkart_v2": ("qwen_harness", "tool_based_personas_flipkart_v2"),
    "order": ("qwen_harness", "tool_based_personas_order"),
    "abandoned": ("qwen_harness", "tool_based_personas_abandoned"),
    "driver": ("qwen_harness", "tool_based_personas_driver"),
    "order_edge": ("qwen_harness", "tool_based_edge_personas_order"),
    "abandoned_edge": ("qwen_harness", "tool_based_edge_personas_abandoned"),
    "driver_edge": ("qwen_harness", "tool_based_edge_personas_driver"),
}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", default="azure", choices=["azure", "grid", "breeze"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--suite", default="example", choices=list(SUITES.keys()))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    import importlib

    suite_pkg, suite_mod_name = SUITES[args.suite]
    suite_mod = importlib.import_module(f"{suite_pkg}.{suite_mod_name}")
    suite_template = json.loads(suite_mod.TEMPLATE_PATH.read_text())
    personas: list[dict] = suite_mod.PERSONAS
    default_payload: dict | None = getattr(suite_mod, "PAYLOAD", None)

    out_path = args.out or f"/tmp/tool_based_{args.suite}.json"
    llm_cfg = suite_template["configurations"].get("llm_configurations") or {}
    if args.llm == "azure":
        # The qwen_harness templates point llm_configurations at breeze/qwen
        # — azure comparison runs use the env-configured deployment instead.
        llm = LLMClient("azure", {"temperature": 0.1, "max_tokens": 500})
    elif args.llm == "breeze":
        llm = LLMClient(
            "breeze",
            {
                "endpoint": llm_cfg.get("endpoint"),
                "model": llm_cfg.get("model"),
                "max_tokens": llm_cfg.get("max_tokens"),
            },
        )
    else:
        llm = LLMClient(
            "grid",
            {
                "endpoint": llm_cfg.get("endpoint") or "https://grid.ai.juspay.net",
                "api_key_name": llm_cfg.get("api_key_name") or "GRID_API_KEY",
                "temperature": llm_cfg.get("temperature"),
                "max_tokens": llm_cfg.get("max_tokens"),
            },
        )

    all_results = []
    async with httpx.AsyncClient() as client:
        for rep in range(args.repeat):
            for persona in personas:
                result = await run_persona(
                    client, llm, persona, suite_template, default_payload
                )
                all_results.append(result)
                status = "PASS" if result["ok"] else "FAIL"
                print(f"[{status}] {result['persona']} -> {'/'.join(result['tools'])}")
                for v in result["violations"]:
                    print(f"       !! {v}")

    passed = sum(1 for r in all_results if r["ok"])
    latencies = sorted(x for r in all_results for x in r["latencies"])
    if latencies:
        avg = sum(latencies) / len(latencies)
        p95 = latencies[int(len(latencies) * 0.95) - 1]
        print(
            f"\n{passed}/{len(all_results)} personas clean ({args.llm}, "
            f"suite={args.suite}) | turn latency avg {avg:.2f}s p95 {p95:.2f}s "
            f"over {len(latencies)} turns"
        )

    Path(out_path).write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    thinking_hits = getattr(llm, "thinking_hits", None)
    if thinking_hits is not None:
        state = "OFF" if os.environ.get("BREEZE_DISABLE_THINKING") == "1" else "default"
        print(f"thinking tokens observed: {thinking_hits} (request thinking: {state})")
    print(f"details: {out_path}")
    return 0 if passed == len(all_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
