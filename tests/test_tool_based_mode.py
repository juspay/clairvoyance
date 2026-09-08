"""Tests for tool_based flow mode — template-owned speech via function say blocks.

Covers the four pieces of the framework:
- ``tool_speech.render_say``: language selection, variant selection,
  placeholder substitution (args over payload vars)
- builder wiring: say passthrough into the function handler,
  ``respond_immediately=False`` on every node, TOOL_BASED_RULES_PROMPT
  injection into the initial node's role messages
- ``transition_handler``: the speech side effect queues a
  ``TTSSpeakFrame(append_to_context=True)`` and (with ``end_call``) runs
  the full end_conversation finalization instead of transitioning
- ``ToolModeProseGuardProcessor``: drops LLM prose, passes tool speech and
  everything else
- ``tool_choice`` plumbing: LLMConfiguration -> Azure/OpenAI settings.extra
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast

import pytest
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    LLMTextFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test

_DOWNSTREAM = FrameDirection.DOWNSTREAM

from app.ai.voice.agents.breeze_buddy.processors.tool_mode_prose_guard import (
    ToolModeProseGuardProcessor,
)
from app.ai.voice.agents.breeze_buddy.template.builder import FlowConfigBuilder
from app.ai.voice.agents.breeze_buddy.template.tool_speech import (
    TOOL_BASED_RULES_PROMPT,
    render_say,
)
from app.ai.voice.agents.breeze_buddy.template.transition import (
    transition_handler,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    FlowMode,
    SayConfig,
    SayPhrasing,
    TemplateModel,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

EXAMPLE_TEMPLATE_PATH = (
    Path(__file__).parent.parent
    / "app"
    / "ai"
    / "voice"
    / "agents"
    / "breeze_buddy"
    / "examples"
    / "templates"
    / "tool-based-example.json"
)


def _say(
    utterances: Optional[Dict[str, List[str]]] = None,
    **kwargs: Any,
) -> SayConfig:
    return SayConfig.model_validate(
        {
            "utterances": utterances
            or {"hi": ["नमस्ते {customer_name}"], "en": ["Hello {customer_name}"]},
            **kwargs,
        }
    )


def _tool_based_template() -> TemplateModel:
    """Two-node tool_based template exercising say / end_call / silent tool."""
    flow = {
        "mode": "tool_based",
        "initial_node": "n1",
        "nodes": [
            {
                "node_name": "n1",
                "task_messages": [{"role": "developer", "content": "Collect the age."}],
                "role_messages": [{"role": "system", "content": "You are Priyanka."}],
                "functions": [
                    {
                        "function_name": "ask_age",
                        "description": "Ask the age",
                        "properties": {
                            "language": {
                                "type": "string",
                                "enum": ["hi", "en"],
                            }
                        },
                        "required": ["language"],
                        "transition_to": "n2",
                        "say": {
                            "utterances": {
                                "hi": ["आपकी उम्र क्या है?"],
                                "en": ["What is your age?"],
                            }
                        },
                    },
                    {
                        # Silent tool on purpose: legal, but flagged by the
                        # builder warning path.
                        "function_name": "mute_and_think",
                        "description": "No speech side effect",
                        "properties": {},
                    },
                ],
            },
            {
                "node_name": "n2",
                "task_messages": [{"role": "developer", "content": "Close the call."}],
                "functions": [
                    {
                        "function_name": "say_goodbye",
                        "description": "Goodbye + hang up",
                        "properties": {
                            "language": {
                                "type": "string",
                                "enum": ["hi", "en"],
                            }
                        },
                        "required": ["language"],
                        "say": {
                            "utterances": {
                                "hi": ["धन्यवाद, नमस्ते!"],
                                "en": ["Thank you, goodbye!"],
                            },
                            "end_call": True,
                        },
                    }
                ],
            },
        ],
    }
    return TemplateModel(id="t1", reseller_id="r1", name="tool-based-test", flow=flow)


class _FakeTask:
    def __init__(self) -> None:
        self.frames: List[Any] = []

    async def queue_frame(self, frame: Any) -> None:
        self.frames.append(frame)


def _fake_context(task: Optional[_FakeTask] = None) -> SimpleNamespace:
    """Duck-typed TemplateContext: bot state + the task the say block needs."""
    bot = SimpleNamespace(
        task=task,
        template_vars={"customer_name": "राहुल"},
        conversation_ended=False,
    )
    return SimpleNamespace(bot=bot, task=task)


# ---------------------------------------------------------------------------
# render_say
# ---------------------------------------------------------------------------


_VARS = {"customer_name": "राहुल"}


class TestRenderSay:
    def test_language_from_args(self):
        text, language = render_say(_say(), {"language": "en"}, _VARS)
        assert language == "en"
        assert text == "Hello राहुल"  # template_vars substitution

    def test_language_falls_back_to_default(self):
        say = _say(default_language="en")
        text, language = render_say(say, {}, _VARS)
        assert language == "en"
        assert text == "Hello राहुल"

    def test_language_falls_back_to_first_key(self):
        text, language = render_say(_say(), {}, _VARS)
        assert language == "hi"
        assert text == "नमस्ते राहुल"

    def test_unknown_language_arg_never_mutes(self):
        text, language = render_say(_say(), {"language": "ta"}, _VARS)
        assert language == "hi"  # first key fallback
        assert text == "नमस्ते राहुल"

    def test_args_override_template_vars(self):
        say = _say({"hi": ["उम्र {value} साल दर्ज है"]})
        text, _ = render_say(say, {"value": "34"}, {"value": "WRONG"})
        assert text == "उम्र 34 साल दर्ज है"

    def test_unresolved_placeholder_kept_verbatim(self):
        say = _say({"hi": ["नमस्ते {missing_token}"]})
        text, _ = render_say(say, {})
        assert text == "नमस्ते {missing_token}"

    def test_first_phrrasing_is_deterministic(self):
        say = _say({"hi": ["एक", "दो", "तीन"]}, phrasing=SayPhrasing.FIRST.value)
        for _ in range(10):
            text, _ = render_say(say, {"language": "hi"})
            assert text == "एक"

    def test_random_phrasing_picks_from_variants(self):
        say = _say({"hi": ["एक", "दो", "तीन"]})
        picks = {render_say(say, {"language": "hi"})[0] for _ in range(50)}
        assert picks == {"एक", "दो", "तीन"}

    def test_non_str_language_arg_ignored(self):
        text, language = render_say(_say(), {"language": 42})
        assert language == "hi"


class TestNestedPlaceholderPaths:
    """Dotted-path substitution into nested payload JSON and tool args."""

    PAYLOAD = {
        "customer": {
            "name": "Rahul",
            "address": {"city": "Bengaluru", "pincode": 560001},
        },
        "items": [
            {"name": "T-Shirt", "price": 499},
            {"name": "Jeans", "price": 1299},
        ],
        "order_json": '{"total": 1798, "currency": "INR"}',
    }

    def test_nested_dict_path(self):
        say = _say(
            {"hi": ["शहर {customer.address.city}, पिन {customer.address.pincode}"]}
        )
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "शहर Bengaluru, पिन 560001"

    def test_list_first_and_last_accessors(self):
        say = _say({"hi": ["पहला {items.first.name}, आखिरी {items.last.name}"]})
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "पहला T-Shirt, आखिरी Jeans"

    def test_numeric_list_index(self):
        say = _say({"hi": ["कीमत {items.1.price} रुपये"]})
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "कीमत 1299 रुपये"

    def test_json_encoded_string_parsed_mid_path(self):
        say = _say({"hi": ["कुल {order_json.total} {order_json.currency}"]})
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "कुल 1798 INR"

    def test_flat_placeholder_still_works(self):
        say = _say({"hi": ["नमस्ते {customer_name}"]})
        text, _ = render_say(say, {}, {"customer_name": "राहुल"})
        assert text == "नमस्ते राहुल"

    def test_missing_leaf_kept_verbatim(self):
        say = _say({"hi": ["{customer.address.country}"]})
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "{customer.address.country}"

    def test_out_of_range_index_kept_verbatim(self):
        say = _say({"hi": ["{items.5.price}"]})
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "{items.5.price}"

    def test_non_scalar_leaf_never_spoken_raw(self):
        say = _say({"hi": ["{items.first}"]})
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "{items.first}"

    def test_args_root_wins_and_shadows_payload_subtree(self):
        say = _say({"hi": ["{items.first.price}"]})
        text, _ = render_say(say, {"items": [{"price": 99}]}, self.PAYLOAD)
        assert text == "99"

    def test_missing_arg_leaf_does_not_fall_through_to_payload(self):
        say = _say({"hi": ["{items.first.price}"]})
        text, _ = render_say(say, {"items": "not-json"}, self.PAYLOAD)
        assert text == "{items.first.price}"

    def test_none_leaf_is_unresolved(self):
        say = _say({"hi": ["{customer.name}"]})
        text, _ = render_say(say, {}, {"customer": {"name": None}})
        assert text == "{customer.name}"

    def test_plain_string_mid_path_is_unresolved(self):
        say = _say({"hi": ["{customer.name.first}"]})
        text, _ = render_say(say, {}, self.PAYLOAD)
        assert text == "{customer.name.first}"


# ---------------------------------------------------------------------------
# SayConfig schema
# ---------------------------------------------------------------------------


class TestSayConfigSchema:
    def test_defaults(self):
        say = _say()
        assert say.phrasing == SayPhrasing.RANDOM
        assert say.default_language is None
        assert say.end_call is False

    def test_empty_utterances_rejected(self):
        with pytest.raises(Exception):
            SayConfig.model_validate({"utterances": {}})

    def test_end_call_flag(self):
        say = SayConfig.model_validate(
            {"utterances": {"hi": ["bye"]}, "end_call": True}
        )
        assert say.end_call is True


# ---------------------------------------------------------------------------
# Builder wiring
# ---------------------------------------------------------------------------


class TestBuilderToolBased:
    def _build(self) -> Dict[str, Any]:
        builder = FlowConfigBuilder()
        return builder.build_flow_config(_tool_based_template())

    def test_mode_in_result(self):
        assert self._build()["mode"] == FlowMode.TOOL_BASED.value

    def test_all_nodes_respond_immediately_false(self):
        config = self._build()
        for name, node in config["nodes"].items():
            assert node.get("respond_immediately") is False, name

    def test_rules_prompt_injected_into_initial_node(self):
        config = self._build()
        role = config["nodes"]["n1"]["role_messages"][-1]
        assert role["role"] == "system"
        assert role["content"].endswith(TOOL_BASED_RULES_PROMPT)
        # authored content preserved ahead of the appended block
        assert role["content"].startswith("You are Priyanka.")

    def test_rules_prompt_added_when_no_role_messages(self):
        template = _tool_based_template()
        template.flow["nodes"][0]["role_messages"] = []
        config = FlowConfigBuilder().build_flow_config(template)
        role = config["nodes"]["n1"]["role_messages"][-1]
        assert role["content"] == TOOL_BASED_RULES_PROMPT

    def test_say_passthrough_to_handler(self):
        captured: Dict[str, Any] = {}

        async def fake_handler(
            llm_args: Dict[str, Any],
            transition_to: Optional[str] = None,
            hooks: Optional[List[Dict[str, Any]]] = None,
            function_name: Optional[str] = None,
            say: Optional[Dict[str, Any]] = None,
        ):
            captured.update(
                say=say,
                transition_to=transition_to,
                function_name=function_name,
            )
            return {}, None

        builder = FlowConfigBuilder()
        cast(Dict[str, Any], builder.handler_map)["transition_handler"] = fake_handler
        config = builder.build_flow_config(_tool_based_template())

        functions = {f.name: f for f in config["nodes"]["n1"]["functions"]}
        import asyncio

        asyncio.run(functions["ask_age"].handler({"language": "hi"}, None))
        assert captured["function_name"] == "ask_age"
        assert captured["transition_to"] == "n2"
        assert captured["say"]["utterances"]["hi"] == ["आपकी उम्र क्या है?"]
        assert captured["say"]["end_call"] is False

        asyncio.run(functions["mute_and_think"].handler({}, None))
        assert captured["say"] is None

    def test_cancel_on_interruption_flag(self):
        """Pure speech functions may be cancelled by a barge-in (their audio
        is flushed anyway) and settle as plain synchronous tool calls; hooked
        or end_call functions must survive interruption to write their
        outcome."""
        config = FlowConfigBuilder().build_flow_config(_tool_based_template())
        functions = {
            f.name: f for node in config["nodes"].values() for f in node["functions"]
        }
        assert functions["ask_age"].cancel_on_interruption is True
        assert functions["say_goodbye"].cancel_on_interruption is False
        assert functions["mute_and_think"].cancel_on_interruption is True

        template = _tool_based_template()
        template.flow["nodes"][0]["functions"][0]["hooks"] = [
            {"name": "update_outcome_in_database"}
        ]
        hooked = {
            f.name: f
            for node in FlowConfigBuilder()
            .build_flow_config(template)["nodes"]
            .values()
            for f in node["functions"]
        }
        assert hooked["ask_age"].cancel_on_interruption is False

    def test_flow_mode_unaffected_by_tool_based_adjustments(self):
        template = _tool_based_template()
        template.flow["mode"] = FlowMode.FLOW.value
        config = FlowConfigBuilder().build_flow_config(template)
        assert "mode" not in config
        # respond_immediately untouched (absent -> flows default True)
        for name, node in config["nodes"].items():
            assert "respond_immediately" not in node, name
        role = config["nodes"]["n1"]["role_messages"][-1]
        assert TOOL_BASED_RULES_PROMPT not in role["content"]

    def test_example_template_builds(self):
        """The shipped example JSON must pass schema validation + build."""
        raw = json.loads(EXAMPLE_TEMPLATE_PATH.read_text())
        flow = raw["flow"]
        template = TemplateModel(
            id="example",
            reseller_id="r1",
            name=raw["template_name"],
            flow=flow,
        )
        config = FlowConfigBuilder().build_flow_config(template)
        assert config["mode"] == FlowMode.TOOL_BASED.value
        say_tools = [
            f.name
            for node in config["nodes"].values()
            for f in node.get("functions", [])
        ]
        assert "say_thankyou" in say_tools
        assert "save_response" in say_tools
        assert "say_goodbye" in say_tools


class TestProdConversionTemplatesBuild:
    """The redbus / flipkart tool_based conversions must keep building."""

    # DB-format finals live in qwen_harness (chained from eleven_v3 sources)
    QWEN_HARNESS = Path(__file__).resolve().parent.parent / "qwen_harness"

    def _build(self, filename: str) -> Dict[str, Any]:
        raw = json.loads((self.QWEN_HARNESS / filename).read_text())
        template = TemplateModel(
            id=raw["id"],
            reseller_id=raw["reseller_id"],
            name=raw["name"],
            flow=raw["flow"],
        )
        return FlowConfigBuilder().build_flow_config(template)

    def test_redbus_toolbased_builds(self):
        config = self._build("redbus-customer-trip-feedback-toolbased.json")
        assert config["mode"] == FlowMode.TOOL_BASED.value
        names = [
            f.name
            for node in config["nodes"].values()
            for f in node.get("functions", [])
        ]
        # speech scripts (name-determines-speech: _hi suffix) + the three
        # outcomes, all say-equipped
        for expected in (
            "rating_hi",
            "reason_hi",
            "more_hi",
            "positive_feedback",
            "negative_feedback",
            "user_busy",
        ):
            assert expected in names

    def test_flipkart_toolbased_builds(self):
        config = self._build("flipkart-recovery-toolbased.json")
        assert config["mode"] == FlowMode.TOOL_BASED.value
        raw = json.loads(
            (self.QWEN_HARNESS / "flipkart-recovery-toolbased.json").read_text()
        )
        funcs = {f["function_name"]: f for f in raw["flow"]["nodes"][0]["functions"]}
        assert len(funcs) == 52  # 32 v5 tools split per language
        # split-language shape: no language arg, single-key say, name-suffixed
        for fn in funcs.values():
            assert "language" not in fn["properties"]
            assert len(fn["say"]["utterances"]) == 1
        # five state hooks exist in all three language variants
        for base in (
            "say_hook_offered",
            "say_hook_eligibility_checked",
            "say_hook_kyc_completed",
            "say_hook_mandate_completed",
            "say_hook_agreement_signed",
        ):
            for suffix in ("hi", "hi_std", "en"):
                fn = funcs[f"{base}_{suffix}"]
                assert set(fn["say"]["utterances"]) == {suffix}
        # all six v5 outcomes split into hi/en, say+end_call, hooked
        for outcome in (
            "app_return_committed",
            "not_interested",
            "customer_stuck",
            "already_completed",
            "user_busy",
            "wrong_person",
        ):
            for suffix in ("hi", "en"):
                fn = funcs[f"{outcome}_{suffix}"]
                assert fn["say"]["end_call"] is True
                assert fn["hooks"][0]["name"] == "update_outcome_in_database"


class TestQwenHarnessTemplatesBuild:
    """The qwen_harness DB-format conversions must keep building.

    Unlike the eleven_v3 wrapper format, these carry the full prod envelope
    (reseller/merchant, telephony ids, eleven_v3 TTS, qwen llm_config with
    tool_choice=required) plus update_outcome_in_database hooks on the
    outcome tools.
    """

    QWEN = Path(__file__).resolve().parent.parent / "qwen_harness"

    def _build(self, filename: str) -> tuple[dict, dict]:
        raw = json.loads((self.QWEN / filename).read_text())
        template = TemplateModel(
            id=raw["id"],
            reseller_id=raw["reseller_id"],
            name=raw["name"],
            flow=raw["flow"],
        )
        return FlowConfigBuilder().build_flow_config(template), raw

    def _assert_envelope(
        self, raw: dict, min_tools: int, tts_model: str = "eleven_v3_conversational"
    ) -> None:
        assert raw["reseller_id"] == "rnr"
        assert raw["merchant_id"] == "test-shopify"
        assert raw["telephony_number_id"] == "6f3c6720-b38f-4b76-ba65-adaf79eec863"
        assert raw["outbound_number_id"] == "6f3c6720-b38f-4b76-ba65-adaf79eec863"
        tts = raw["configurations"]["tts_configuration"]
        assert tts["provider"] == "elevenlabs"
        assert tts["model"] == tts_model
        assert tts["voice_id"] == "iB2rIwm9cQCRGWoKDRtX"
        assert tts["enable_tts_caching"] is True
        llm = raw["configurations"]["llm_configurations"]
        assert llm["model"] == "qwen3.8-27b-4bit"
        assert llm["tool_choice"] == "required"
        funcs = raw["flow"]["nodes"][0]["functions"]
        assert len(funcs) >= min_tools
        # every function speaks — no silent tools in tool_based mode
        assert all(f.get("say") for f in funcs)
        # split-language shape everywhere: no language arg, the function
        # name alone determines the spoken line
        for fn in funcs:
            assert "language" not in fn["properties"]
            assert len(fn["say"]["utterances"]) == 1

    def test_order_confirmation_builds(self):
        config, raw = self._build("order-confirmation-toolbased.json")
        assert config["mode"] == FlowMode.TOOL_BASED.value
        self._assert_envelope(raw, 40)
        funcs = {f["function_name"]: f for f in raw["flow"]["nodes"][0]["functions"]}
        assert len(funcs) == 56  # 28 actions x hi/en
        # the four COD outcomes speak their closings and end the call,
        # in both language variants
        for name in (
            "confirm_order",
            "cancel_order",
            "update_address_and_confirm",
            "user_busy",
        ):
            for suffix in ("hi", "en"):
                fn = funcs[f"{name}_{suffix}"]
                assert fn["say"]["end_call"] is True
                assert fn["hooks"][0]["name"] == "update_outcome_in_database"
                assert set(fn["say"]["utterances"]) == {suffix}
        # address read-back takes the merged address as a verbatim arg
        assert "updated_address" in funcs["say_address_read_back_hi"]["required"]
        # each variant's utterance map is keyed by its own suffix
        for fn_name, fn in funcs.items():
            assert fn_name.endswith(tuple(fn["say"]["utterances"]))

    def test_abandoned_checkout_builds(self):
        config, raw = self._build("abandoned-checkout-toolbased.json")
        assert config["mode"] == FlowMode.TOOL_BASED.value
        self._assert_envelope(raw, 30)
        funcs = {f["function_name"]: f for f in raw["flow"]["nodes"][0]["functions"]}
        assert len(funcs) == 38  # 19 actions x hi/en
        high = funcs["high_intent_recovery_hi"]["properties"]["abandonment_reason"][
            "enum"
        ]
        assert "PAYMENT_FAILED" in high and "PRODUCT_DETAIL_GAP" in high
        low = funcs["low_intent_recovery_hi"]["properties"]["abandonment_reason"][
            "enum"
        ]
        assert "ALREADY_PURCHASED_ELSEWHERE" in low
        for name in (
            "high_intent_recovery",
            "low_intent_recovery",
            "already_purchased",
            "user_busy",
        ):
            for suffix in ("hi", "en"):
                assert funcs[f"{name}_{suffix}"]["say"]["end_call"] is True
        # the ladder probes carry the cart-reference arg
        assert "cart_reference" in funcs["say_after_consent_hi"]["required"]
        assert "cart_reference" in funcs["say_deflection_probe_en"]["required"]
        # edge hardening: off-topic/identity questions get a redirect tool
        assert "say_off_topic_hi" in funcs and "say_off_topic_en" in funcs

    def test_driver_not_active_builds(self):
        config, raw = self._build("driver-not-active-toolbased.json")
        assert config["mode"] == FlowMode.TOOL_BASED.value
        # driver stays on eleven_v3_conversational (not part of the v2 swap)
        self._assert_envelope(raw, 12, tts_model="eleven_v3_conversational")
        funcs = {f["function_name"]: f for f in raw["flow"]["nodes"][0]["functions"]}
        # Kannada-only: single selector, no language arg anywhere
        for fn in funcs.values():
            assert set(fn["say"]["utterances"]) == {"kn"}
            assert "language" not in fn["properties"]
        # driver_says_no SPEAKS the why question instead of ending
        assert funcs["driver_says_no"]["say"].get("end_call", False) is False
        assert (
            "ಯಾಕೆ ready ಇಲ್ಲ" in funcs["driver_says_no"]["say"]["utterances"]["kn"][0]
        )
        # routing-encoded ready_to_start values are static-injected
        expected = funcs["driver_says_yes"]["hooks"][0]["expected_fields"]
        assert expected["ready_to_start"] == {"value": "YES", "source": "static"}
        # only the genuinely varying functions keep the enum arg
        assert "ready_to_start" in funcs["driver_says_no"]["properties"]
        assert "ready_to_start" not in funcs["driver_says_yes"]["properties"]
        # the pitch is the gate: yes/no outcomes mention it
        assert "say_pitch" in funcs["driver_says_yes"]["description"]
        # edge hardening: off-topic questions get a redirect back to ready
        assert "say_off_topic" in funcs
        assert "ready na?" in funcs["say_off_topic"]["say"]["utterances"]["kn"][0]


# ---------------------------------------------------------------------------
# transition_handler speech side effect
# ---------------------------------------------------------------------------


class TestTransitionHandlerSay:
    @pytest.mark.asyncio
    async def test_say_queues_tts_speak_frame(self, monkeypatch):
        # Neutralize VAD/interruption side effects — they touch bot internals
        # the fake bot doesn't have.
        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".reset_vad_to_default",
            lambda context: None,
        )
        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".apply_node_vad_config",
            lambda context, node: None,
        )

        async def noop(*args, **kwargs):
            return None

        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".reset_interruption_to_default",
            noop,
        )
        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".apply_node_interruption_config",
            noop,
        )

        task = _FakeTask()
        context = _fake_context(task)
        say = {
            "utterances": {"hi": ["आपकी उम्र क्या है?"], "en": ["What?"]},
            "phrasing": "first",
        }

        result, next_node = await transition_handler(
            context,
            {"language": "hi"},
            transition_to=None,
            hooks=None,
            function_name="ask_age",
            say=say,
        )

        assert len(task.frames) == 1
        frame = task.frames[0]
        assert isinstance(frame, TTSSpeakFrame)
        assert frame.text == "आपकी उम्र क्या है?"
        assert frame.append_to_context is True
        # no transition_to -> stays on node, result is a plain dict
        assert next_node is None

    @pytest.mark.asyncio
    async def test_say_without_transition_returns_empty_result(self, monkeypatch):
        # Regression: a say-block function used to return a truthy
        # {"result": ..., "status": "success"} dict. Pipecat's response
        # aggregator re-runs inference on any truthy function result, and
        # with tool_choice=required the model must call another tool —
        # the bot repeated itself forever. The say path must return an
        # EMPTY result so the turn ends and the bot waits for the user.
        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".reset_vad_to_default",
            lambda context: None,
        )
        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".apply_node_vad_config",
            lambda context, node: None,
        )

        async def noop(*args, **kwargs):
            return None

        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".reset_interruption_to_default",
            noop,
        )
        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.template.transition"
            ".apply_node_interruption_config",
            noop,
        )

        task = _FakeTask()
        context = _fake_context(task)
        say = {"utterances": {"hi": ["बोलिए"]}, "phrasing": "first"}

        result, next_node = await transition_handler(
            context,
            {"language": "hi"},
            transition_to=None,
            hooks=None,
            function_name="say_rating_request",
            say=say,
        )

        # empty result: falsy, so no LLM re-inference after the speech
        assert result == {}
        assert next_node is None

    @pytest.mark.asyncio
    async def test_end_call_speaks_then_finalizes(self, monkeypatch):
        finalized: List[Dict[str, Any]] = []

        async def fake_end_conversation(ctx, args, transition_to=None):
            finalized.append(args)

        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.handlers.internal" ".end_conversation",
            fake_end_conversation,
        )

        task = _FakeTask()
        context = _fake_context(task)
        say = {
            "utterances": {"hi": ["धन्यवाद, नमस्ते!"]},
            "end_call": True,
        }

        result, next_node = await transition_handler(
            context,
            {"language": "hi"},
            transition_to="some_node",  # must be ignored on the end_call path
            hooks=None,
            function_name="say_goodbye",
            say=say,
        )

        assert len(task.frames) == 1
        assert task.frames[0].text == "धन्यवाद, नमस्ते!"
        assert finalized == [{"language": "hi"}]
        assert next_node is None

    @pytest.mark.asyncio
    async def test_end_call_early_fired_skips_speech_still_finalizes(self, monkeypatch):
        """Outcome closings early-fire at name-decode; at function completion
        the handler must not re-speak, but end_conversation still runs — the
        goodbye audio already queued ahead of the EndFrame drains first
        because TTS processes frames sequentially."""
        finalized: List[Dict[str, Any]] = []

        async def fake_end_conversation(ctx, args, transition_to=None):
            finalized.append(args)

        monkeypatch.setattr(
            "app.ai.voice.agents.breeze_buddy.handlers.internal" ".end_conversation",
            fake_end_conversation,
        )

        class _Router:
            def __init__(self) -> None:
                self.consumed: List[Optional[str]] = []

            def consume_pending(self, function_name):
                self.consumed.append(function_name)
                return function_name == "negative_feedback"

        router = _Router()
        task = _FakeTask()
        context = _fake_context(task)
        context.bot.early_speech_router = router
        say = {
            "utterances": {"hi": ["जी मैं समझता हूँ. धन्यवाद!"]},
            "end_call": True,
        }

        result, next_node = await transition_handler(
            context,
            {"rating": 1, "feedback": "बस लेट थी"},
            transition_to=None,
            hooks=None,
            function_name="negative_feedback",
            say=say,
        )

        assert task.frames == []  # already spoken early — no double-speak
        assert router.consumed == ["negative_feedback"]
        assert finalized == [{"rating": 1, "feedback": "बस लेट थी"}]
        assert next_node is None

    @pytest.mark.asyncio
    async def test_no_task_drops_speech_without_crash(self):
        context = _fake_context(task=None)
        say = {"utterances": {"hi": ["नमस्ते"]}}
        result, next_node = await transition_handler(
            context,
            {},
            transition_to=None,
            hooks=None,
            function_name="ask_age",
            say=say,
        )
        assert next_node is None


# ---------------------------------------------------------------------------
# Prose guard — driven through pipecat's run_test harness so push semantics
# match a live pipeline exactly (same pattern as test_voice_ui_stream.py).
# ---------------------------------------------------------------------------


class TestProseGuard:
    @pytest.mark.asyncio
    async def test_drops_llm_text_frames(self):
        guard = ToolModeProseGuardProcessor()
        down, _ = await run_test(
            guard,
            frames_to_send=[
                LLMTextFrame(text="नमस्ते!"),
                TextFrame(text="raw text"),
            ],
        )
        texts = [f.text for f in down if isinstance(f, TextFrame)]
        assert texts == ["raw text"]
        assert guard.dropped_prose_count == 1

    @pytest.mark.asyncio
    async def test_tool_speech_passes_through(self):
        guard = ToolModeProseGuardProcessor()
        speak = TTSSpeakFrame(text="टूल बोल रहा है", append_to_context=True)
        down, _ = await run_test(guard, frames_to_send=[speak])
        assert speak in down


# ---------------------------------------------------------------------------
# tool_choice plumbing
# ---------------------------------------------------------------------------


class TestToolChoicePlumbing:
    def test_llm_configuration_accepts_tool_choice(self):
        from app.ai.voice.llm.types import LLMConfiguration

        config = LLMConfiguration(tool_choice="required")
        assert config.tool_choice == "required"

    def test_azure_settings_extra_carries_tool_choice(self):
        from app.ai.voice.llm.azure import AzureConfig, build_azure_llm

        service = build_azure_llm(
            AzureConfig(
                api_key="k",
                endpoint="https://example.openai.azure.com",
                model="gpt-4.1",
                tool_choice="required",
            )
        )
        assert service._settings.extra.get("tool_choice") == "required"

    def test_openai_settings_extra_carries_tool_choice(self):
        from app.ai.voice.llm.openai import OpenAIConfig, build_openai_llm

        service = build_openai_llm(
            OpenAIConfig(api_key="k", model="gpt-4o", tool_choice="required")
        )
        assert service._settings.extra.get("tool_choice") == "required"

    def test_gateway_base_url_disables_developer_role(self):
        from app.ai.voice.llm.openai import OpenAIConfig, build_openai_llm

        # breeze/sglang 400s on developer-role messages ("Unexpected message
        # role") — gateway services must convert them to user messages.
        gateway = build_openai_llm(
            OpenAIConfig(
                api_key="k",
                model="qwen3.8-27b-4bit",
                base_url="http://34.126.104.99:8000/v1",
                tool_choice="required",
            )
        )
        assert gateway.supports_developer_role is False

        stock = build_openai_llm(OpenAIConfig(api_key="k", model="gpt-4o"))
        assert stock.supports_developer_role is True

    def test_absent_tool_choice_leaves_extra_clean(self):
        from app.ai.voice.llm.azure import AzureConfig, build_azure_llm

        service = build_azure_llm(
            AzureConfig(
                api_key="k",
                endpoint="https://example.openai.azure.com",
                model="gpt-4.1",
            )
        )
        assert "tool_choice" not in service._settings.extra


# ---------------------------------------------------------------------------
# prepare_initial_node honors tool_based
# ---------------------------------------------------------------------------


class TestPrepareInitialNode:
    def _prepare(self, mode: Optional[str]):
        from app.ai.voice.agents.breeze_buddy.agent.flow import prepare_initial_node

        builder = FlowConfigBuilder()
        template = _tool_based_template()
        if mode is None:
            template.flow.pop("mode")
        else:
            template.flow["mode"] = mode
        flow_config = builder.build_flow_config(template)
        return prepare_initial_node(
            flow_config,
            lead_payload={},
            configurations=None,
            has_greeting_source=False,
        )

    def test_tool_based_never_responds_immediately(self):
        node = self._prepare(FlowMode.TOOL_BASED.value)
        assert node["respond_immediately"] is False

    def test_non_tool_based_without_greeting_responds(self):
        node = self._prepare(FlowMode.FLOW.value)
        assert node["respond_immediately"] is True


# ---------------------------------------------------------------------------
# Early speech: fire TTS on function-name decode
# ---------------------------------------------------------------------------


class TestEarlySpeechRouter:
    def _router(self, functions, task_frames, template_vars=None, tts_frames=None):
        from app.ai.voice.agents.breeze_buddy.template.early_speech import (
            EarlySpeechRouter,
        )

        task = SimpleNamespace(queue_frame=asyncio_mock_collector(task_frames))
        tts = (
            SimpleNamespace(queue_frame=asyncio_mock_collector(tts_frames))
            if tts_frames is not None
            else None
        )
        return EarlySpeechRouter(
            functions=functions,
            task_getter=lambda: task,
            template_vars_getter=lambda: template_vars or {},
            tts_getter=(lambda: tts) if tts_frames is not None else None,
        )

    @pytest.mark.asyncio
    async def test_fires_on_unique_prefix_with_id(self):
        frames = []
        router = self._router(
            [
                {
                    "function_name": "say_rating_request",
                    "say": {
                        "utterances": {"hi": ["रेटिंग दीजिए"]},
                        "phrasing": "first",
                    },
                },
                {
                    "function_name": "say_rating_reask",
                    "say": {"utterances": {"hi": ["फिर बताइए"]}, "phrasing": "first"},
                },
            ],
            frames,
        )
        # ambiguous prefix (both say_rating_* match) — nothing yet
        await router.on_name_fragment("say_rating", "call_1")
        assert frames == []
        # unambiguous + id — fires, even before the name is complete
        await router.on_name_fragment("say_rating_req", "call_1")
        assert len(frames) == 1
        assert isinstance(frames[0], TTSSpeakFrame)
        assert frames[0].text == "रेटिंग दीजिए"
        assert frames[0].append_to_context is True
        # same tool_call_id never fires twice
        await router.on_name_fragment("say_rating_request", "call_1")
        assert len(frames) == 1
        # a repeat call (new id) fires again
        await router.on_name_fragment("say_rating_request", "call_2")
        assert len(frames) == 2

    @pytest.mark.asyncio
    async def test_no_id_requires_full_name(self):
        frames = []
        router = self._router(
            [
                {
                    "function_name": "say_hi_en",
                    "say": {"utterances": {"en": ["Hello"]}, "phrasing": "first"},
                }
            ],
            frames,
        )
        await router.on_name_fragment("say_hi", "")
        assert frames == []
        await router.on_name_fragment("say_hi_en", "")
        assert len(frames) == 1

    @pytest.mark.asyncio
    async def test_arg_placeholder_blocks_early_fire(self):
        frames = []
        router = self._router(
            [
                {
                    "function_name": "say_address",
                    "say": {
                        "utterances": {"hi": ["पता {updated_address} है"]},
                        "phrasing": "first",
                    },
                }
            ],
            frames,
        )
        await router.on_name_fragment("say_address", "call_1")
        assert frames == []
        assert router.consume_pending("say_address") is False

    @pytest.mark.asyncio
    async def test_template_vars_resolve_early(self):
        frames = []
        router = self._router(
            [
                {
                    "function_name": "say_rating_request",
                    "say": {
                        "utterances": {"hi": ["{bus_operator_name} बस ट्रिप"]},
                        "phrasing": "first",
                    },
                }
            ],
            frames,
            template_vars={"bus_operator_name": "ABC travels"},
        )
        await router.on_name_fragment("say_rating_request", "call_1")
        assert len(frames) == 1
        assert frames[0].text == "ABC travels बस ट्रिप"

    @pytest.mark.asyncio
    async def test_consume_pending_is_one_shot(self):
        frames = []
        router = self._router(
            [
                {
                    "function_name": "say_bye",
                    "say": {"utterances": {"hi": ["अलविदा"]}, "phrasing": "first"},
                }
            ],
            frames,
        )
        await router.on_name_fragment("say_bye", "call_1")
        assert router.consume_pending("say_bye") is True
        # consumed — a stale second consume fails
        assert router.consume_pending("say_bye") is False
        # different function never consumes
        await router.on_name_fragment("say_bye", "call_2")
        assert router.consume_pending("say_other") is False
        assert router.consume_pending("say_bye") is True

    @pytest.mark.asyncio
    async def test_end_call_tool_fires_early(self):
        """Outcome tools early-fire too: the closing text is static, so speech
        starts at name-decode while the dynamic arguments (rating / feedback)
        decode for another ~1s. The handler consumes the marker at function
        completion — hooks and end_conversation still run there."""
        frames = []
        router = self._router(
            [
                {
                    "function_name": "negative_feedback",
                    "say": {
                        "utterances": {"hi": ["जी. मैं समझता हूँ."]},
                        "phrasing": "first",
                        "end_call": True,
                    },
                },
                {
                    "function_name": "reason_hi",
                    "say": {
                        "utterances": {"hi": ["कारण क्या था?"]},
                        "phrasing": "first",
                    },
                },
            ],
            frames,
        )
        await router.on_name_fragment("negative_feedback", "call_1")
        assert len(frames) == 1
        assert router.consume_pending("negative_feedback") is True
        # plain speech tools on the same router fire as before
        await router.on_name_fragment("reason_hi", "call_2")
        assert len(frames) == 2

    @pytest.mark.asyncio
    async def test_early_fire_routes_direct_to_tts(self):
        """The early frame must not enter at the pipeline top: a task-queued
        frame waits behind the LLM processor's in-flight stream (measured as
        ttfc ≈ LLM complete + ~2ms on every turn). With a TTS service
        available, the frame queues straight into it."""
        task_frames = []
        tts_frames = []
        router = self._router(
            [
                {
                    "function_name": "reason_hi",
                    "say": {
                        "utterances": {"hi": ["पहला वाक्य. दूसरा वाक्य."]},
                        "phrasing": "first",
                    },
                }
            ],
            task_frames,
            tts_frames=tts_frames,
        )
        await router.on_name_fragment("reason_hi", "call_1")
        await asyncio.sleep(0.2)  # let the sentence tail run
        assert [f.text for f in tts_frames] == ["पहला वाक्य.", "दूसरा वाक्य."]
        assert task_frames == []

    @pytest.mark.asyncio
    async def test_cancel_pending_tails_stops_tail_and_clears_marker(self):
        frames = []
        router = self._router(
            [
                {
                    "function_name": "say_long",
                    "say": {
                        "utterances": {"hi": ["पहला वाक्य. दूसरा वाक्य. तीसरा वाक्य."]},
                        "phrasing": "first",
                    },
                }
            ],
            frames,
        )
        await router.on_name_fragment("say_long", "call_1")
        assert len(frames) == 1  # first sentence queued inline
        # user barges in before the tail finishes
        router.cancel_pending_tails()
        await asyncio.sleep(0.2)  # the tail would have queued 2 more by now
        assert len(frames) == 1
        # marker cleared — a re-call of the same function speaks normally
        assert router.consume_pending("say_long") is False

    def test_attach_no_op_without_say_functions(self):
        from app.ai.voice.agents.breeze_buddy.template.early_speech import (
            attach_early_speech,
        )

        class _Svc:
            pass

        svc = _Svc()
        bot = SimpleNamespace(task=None, template_vars={})
        router = attach_early_speech(
            llm_service=svc,
            bot=bot,
            flow={"mode": "tool_based", "nodes": [{"functions": []}]},
        )
        assert router is not None and router.enabled is False
        assert not hasattr(svc, "on_function_name_fragment")

        router = attach_early_speech(
            llm_service=svc,
            bot=bot,
            flow={"mode": "flow", "nodes": []},
        )
        assert router is None


def asyncio_mock_collector(frames):
    async def _queue(frame):
        frames.append(frame)

    return _queue


# ---------------------------------------------------------------------------
# Say utterances reach TTS sentence-by-sentence (LLM-aggregation pacing)
# ---------------------------------------------------------------------------


class TestSaySentenceSplitting:
    def test_split_say_for_tts(self):
        from app.ai.voice.agents.breeze_buddy.template.tool_speech import (
            split_say_for_tts,
        )

        line = (
            "ठीक है. आपने ABC travels बस से Mumbai से Punjab तक ट्रिप की थी. "
            "कृपया रेटिंग दीजिए."
        )
        parts = split_say_for_tts(line)
        assert parts == [
            "ठीक है.",
            "आपने ABC travels बस से Mumbai से Punjab तक ट्रिप की थी.",
            "कृपया रेटिंग दीजिए.",
        ]
        # Devanagari danda and single-sentence lines
        assert split_say_for_tts("एक वाक्य। दूसरा वाक्य।") == [
            "एक वाक्य।",
            "दूसरा वाक्य।",
        ]
        assert split_say_for_tts("कोई विराम चिह्न नहीं") == ["कोई विराम चिह्न नहीं"]

    @pytest.mark.asyncio
    async def test_multi_sentence_say_queues_per_sentence(self, monkeypatch):
        transition_mod = __import__(
            "app.ai.voice.agents.breeze_buddy.template.transition",
            fromlist=["transition"],
        )
        monkeypatch.setattr(
            transition_mod, "reset_vad_to_default", lambda context: None
        )
        monkeypatch.setattr(
            transition_mod, "apply_node_vad_config", lambda context, node: None
        )

        async def noop(*args, **kwargs):
            return None

        monkeypatch.setattr(transition_mod, "reset_interruption_to_default", noop)
        monkeypatch.setattr(transition_mod, "apply_node_interruption_config", noop)

        task = _FakeTask()
        context = _fake_context(task)
        say = {
            "utterances": {"hi": ["पहला वाक्य. दूसरा वाक्य. तीसरा वाक्य."]},
            "phrasing": "first",
        }

        await transition_mod.transition_handler(
            context,
            {"language": "hi"},
            transition_to=None,
            hooks=None,
            function_name="say_long_line",
            say=say,
        )

        texts = [f.text for f in task.frames]
        assert texts == ["पहला वाक्य.", "दूसरा वाक्य.", "तीसरा वाक्य."]
        assert all(f.append_to_context for f in task.frames)

    @pytest.mark.asyncio
    async def test_early_speech_multi_sentence_first_inline(self):
        from app.ai.voice.agents.breeze_buddy.template.early_speech import (
            EarlySpeechRouter,
        )

        frames = []

        async def _queue(frame):
            frames.append(frame)

        router = EarlySpeechRouter(
            functions=[
                {
                    "function_name": "say_rating_request",
                    "say": {
                        "utterances": {"hi": ["एक. दो. तीन."]},
                        "phrasing": "first",
                    },
                }
            ],
            task_getter=lambda: SimpleNamespace(queue_frame=_queue),
            template_vars_getter=lambda: {},
        )
        await router.on_name_fragment("say_rating_request", "call_1")
        # first sentence queued synchronously inside the hook
        assert [f.text for f in frames] == ["एक."]
        # the tail lands after the gapped background task runs
        await asyncio.gather(*list(router._fire_tasks))
        assert [f.text for f in frames] == ["एक.", "दो.", "तीन."]
        # dedup marker still one-shot
        assert router.consume_pending("say_rating_request") is True


# ---------------------------------------------------------------------------
# STT timing tap + collector finalize measurement
# ---------------------------------------------------------------------------


class TestSTTTimingTap:
    @pytest.mark.asyncio
    async def test_tap_notes_and_passes_through(self):
        from app.ai.voice.agents.breeze_buddy.processors.stt_timing_tap import (
            STTTimingTapProcessor,
        )

        noted: List[str] = []
        collector = SimpleNamespace(
            note_stt_interim=lambda: noted.append("interim"),
            note_stt_final=lambda: noted.append("final"),
        )
        tap = STTTimingTapProcessor(collector)
        interim = InterimTranscriptionFrame(text="हाँ", user_id="", timestamp="")
        final = TranscriptionFrame(text="हाँ, बोलो।", user_id="", timestamp="")
        down, _ = await run_test(tap, frames_to_send=[interim, final])
        assert down == [interim, final]
        assert noted == ["interim", "final"]

    def test_collector_measures_endpointer_tail(self):
        import time as _time

        from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (  # noqa: E501
            MetricsCollectorProcessor,
        )

        collector = MetricsCollectorProcessor()
        collector.note_stt_interim()
        _time.sleep(0.05)
        # a later interim moves the anchor — the tail is last-interim → final
        collector.note_stt_interim()
        _time.sleep(0.03)
        collector.note_stt_final()
        assert collector._stt_finalize_ms is not None
        assert 20 <= collector._stt_finalize_ms <= 500
        # a final with no open interim window measures nothing
        measured = collector._stt_finalize_ms
        collector.note_stt_final()
        assert collector._stt_finalize_ms == measured
        # a new utterance opens a fresh window
        collector.note_stt_interim()
        assert collector._stt_finalize_ms is None


class TestTurnLatencyBreakdown:
    """The two per-turn anchors the conversation view shows beyond ttfc:
    speech-stop → finalized transcript (the recognizer tail the caller waits
    through) and speech-stop → early-fire say queued at name-decode."""

    @pytest.mark.asyncio
    async def test_vad_stop_to_final_lands_in_turn_payload(self):
        import time as _time

        from pipecat.frames.frames import UserStoppedSpeakingFrame

        from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (  # noqa: E501
            MetricsCollectorProcessor,
        )

        collector = MetricsCollectorProcessor()
        collector.note_stt_interim()
        await collector.process_frame(UserStoppedSpeakingFrame(), _DOWNSTREAM)
        _time.sleep(0.04)
        collector.note_stt_final()

        turns = collector.get_metrics()
        assert len(turns) == 1
        assert turns[0]["stt_vad_final_ms"] is not None
        assert 20 <= turns[0]["stt_vad_final_ms"] <= 500

    @pytest.mark.asyncio
    async def test_early_say_lands_in_turn_payload(self):
        import time as _time

        from pipecat.frames.frames import UserStoppedSpeakingFrame

        from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (  # noqa: E501
            MetricsCollectorProcessor,
        )

        collector = MetricsCollectorProcessor()
        await collector.process_frame(UserStoppedSpeakingFrame(), _DOWNSTREAM)
        _time.sleep(0.03)
        collector.note_early_say()

        turns = collector.get_metrics()
        assert len(turns) == 1
        assert turns[0]["early_say_ms"] is not None
        assert 10 <= turns[0]["early_say_ms"] <= 500
        # an early-fire with no open speech-stop anchor is not measured
        collector.note_early_say()
        assert turns[0]["early_say_ms"] is not None  # unchanged payload

    @pytest.mark.asyncio
    async def test_new_fields_reset_between_turns(self):
        from pipecat.frames.frames import UserStoppedSpeakingFrame

        from app.ai.voice.agents.breeze_buddy.processors.metrics_collector_processor import (  # noqa: E501
            MetricsCollectorProcessor,
        )

        collector = MetricsCollectorProcessor()
        await collector.process_frame(UserStoppedSpeakingFrame(), _DOWNSTREAM)
        collector.note_early_say()
        first = collector.get_metrics()
        assert first[0]["early_say_ms"] is not None

        # Next turn: speech stop but no early-fire / no final. A turn whose
        # only event is the speech stop is dropped entirely (empty-turn
        # guard); the stale values must not reach any LATER turn's payload —
        # prove it with a second turn that does have an LLM measurement.
        await collector.process_frame(UserStoppedSpeakingFrame(), _DOWNSTREAM)
        collector._record("OpenAILLMService", "ttfb_ms", 0.2)
        second = collector.get_metrics()
        assert len(second) == 2
        assert second[1]["processors"]["OpenAILLMService"]["ttfb_ms"] == [200.0]
        assert "early_say_ms" not in second[1]
        assert "stt_vad_final_ms" not in second[1]


class TestInterruptionAudioGuard:
    """Stale TTS chunks racing past the interruption flush must be dropped —
    otherwise the carrier replays them after its clear event ("echo" of the
    interrupted line, 2026-09-06 15:45 redbus call). Driven by direct
    process_frame calls: pipecat's run_test worker hangs on real
    InterruptionFrames (its interruption machinery never settles in the
    minimal test pipeline)."""

    async def _guard(self):
        from pipecat.utils.asyncio.task_manager import TaskManager

        from app.ai.voice.agents.breeze_buddy.processors.interruption_audio_guard import (  # noqa: E501
            InterruptionAudioGuardProcessor,
        )

        guard = InterruptionAudioGuardProcessor()
        # The base-class lifecycle handlers (StartFrame creates the process
        # task, InterruptionFrame flushes) need a task manager — in
        # production FrameProcessorSetup wires one during pipeline setup.
        guard._task_manager = TaskManager()
        pushed: List[Any] = []

        async def _collect(frame, direction=_DOWNSTREAM):
            pushed.append(frame)

        guard.push_frame = _collect
        return guard, pushed

    @pytest.mark.asyncio
    async def test_drops_stale_audio_until_new_utterance(self):
        from pipecat.frames.frames import (
            InterruptionFrame,
            TTSAudioRawFrame,
            TTSStartedFrame,
        )

        guard, pushed = await self._guard()
        before = TTSAudioRawFrame(audio=b"\x01\x02", sample_rate=8000, num_channels=1)
        stale = TTSAudioRawFrame(audio=b"\x03\x04", sample_rate=8000, num_channels=1)
        fresh = TTSAudioRawFrame(audio=b"\x05\x06", sample_rate=8000, num_channels=1)
        interrupt = InterruptionFrame()
        started = TTSStartedFrame()

        for frame in (before, interrupt, stale, started, fresh):
            await guard.process_frame(frame, _DOWNSTREAM)

        assert pushed == [before, interrupt, started, fresh]
        assert (
            stale not in pushed
        ), "chunk racing the flush must never reach the carrier"
        assert guard.armed is False

    @pytest.mark.asyncio
    async def test_passes_audio_when_never_interrupted(self):
        from pipecat.frames.frames import TTSAudioRawFrame

        guard, pushed = await self._guard()
        a = TTSAudioRawFrame(audio=b"\x01\x02", sample_rate=8000, num_channels=1)
        b = TTSAudioRawFrame(audio=b"\x03\x04", sample_rate=8000, num_channels=1)
        await guard.process_frame(a, _DOWNSTREAM)
        await guard.process_frame(b, _DOWNSTREAM)
        assert pushed == [a, b]
        assert guard.armed is False

    @pytest.mark.asyncio
    async def test_max_armed_timeout_releases_the_guard(self):
        import time as _time

        from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame

        guard, pushed = await self._guard()
        await guard.process_frame(InterruptionFrame(), _DOWNSTREAM)
        audio = TTSAudioRawFrame(audio=b"\x07\x08", sample_rate=8000, num_channels=1)

        # Armed long past the failsafe ceiling: audio passes and the guard
        # rearms off instead of muting the line forever.
        guard._armed_at = _time.monotonic() - 5.0
        await guard.process_frame(audio, _DOWNSTREAM)
        assert pushed[-1] is audio and audio not in pushed[:-1]
        assert guard.armed is False

    @pytest.mark.asyncio
    async def test_start_frame_creates_frame_processing_task(self):
        """Regression for the 2026-09-06 16:16 silent live call.

        In pipecat 1.8.1 the base FrameProcessor.process_frame owns the
        lifecycle frames: StartFrame creates this processor's non-system
        frame task, which is the ONLY consumer of its process queue. A
        processor that overrides process_frame without calling super()
        never gets that task, so every audio frame enqueues into the guard
        and is never forwarded — total silence downstream while everything
        upstream (LLM, TTS synthesis, service metrics) looks healthy.
        Driving this through run_test just deadlocks the pipeline, so the
        test asserts the mechanism directly: after StartFrame flows through
        the guard's own process_frame, the process task must exist.
        """
        from pipecat.frames.frames import StartFrame
        from pipecat.utils.asyncio.task_manager import TaskManager

        from app.ai.voice.agents.breeze_buddy.processors.interruption_audio_guard import (  # noqa: E501
            InterruptionAudioGuardProcessor,
        )

        guard = InterruptionAudioGuardProcessor()
        guard._task_manager = TaskManager()
        await guard.process_frame(StartFrame(), _DOWNSTREAM)

        # Name-mangled private by design: this is the exact task whose
        # absence muted the live call, and there is no public accessor.
        task = getattr(guard, "_FrameProcessor__process_frame_task")
        assert task is not None, (
            "StartFrame must create the guard's non-system frame task — "
            "without it (missing super().process_frame) audio frames queue "
            "forever and the caller hears silence"
        )
        assert not task.done()


class TestFlipkartV2ToolBased:
    """The bilingual flipkart-emi-dropoff-recovery-toolbased conversion:
    per-language split functions, one dialogue each, unique-first-letter
    stems (early-fire gates on the name decode), bilingual outcomes."""

    PATH = (
        Path(__file__).resolve().parent.parent
        / "qwen_harness"
        / "flipkart-emi-dropoff-recovery-toolbased.json"
    )
    OUTCOMES = {
        "commit": "APP_NUDGE_ACCEPTED",
        "decline": "NOT_INTERESTED",
        "stuck": "ISSUE_REPORTED",
        "done": "ALREADY_DONE",
        "busy": "BUSY",
        "wrong": "WRONG_PERSON",
    }

    def _template(self) -> Dict[str, Any]:
        return json.loads(self.PATH.read_text())

    def _functions(self) -> List[Dict[str, Any]]:
        return [f for n in self._template()["flow"]["nodes"] for f in n["functions"]]

    def test_builds(self):
        template = TemplateModel.model_validate(self._template())
        config = FlowConfigBuilder().build_flow_config(template)
        assert config["mode"] == FlowMode.TOOL_BASED.value

    def test_speech_tools_one_dialogue_no_language_arg(self):
        for f in self._functions():
            if f["function_name"] in self.OUTCOMES:
                continue
            for lang, lines in f["say"]["utterances"].items():
                assert len(lines) == 1, f"{f['function_name']}.{lang}"
            assert "language" not in f["properties"], f["function_name"]
            assert f["hooks"] == []
            assert not f["say"].get("end_call")

    def test_speech_stems_paired_with_unique_first_letters(self):
        speech = [
            f["function_name"]
            for f in self._functions()
            if f["function_name"] not in self.OUTCOMES
        ]
        stems: Dict[str, set] = {}
        for name in speech:
            stem, lang = name.rsplit("_", 1)
            stems.setdefault(stem, set()).add(lang)
        assert speech and all(langs == {"hi", "en"} for langs in stems.values())
        firsts = [stem[0] for stem in stems]
        assert len(firsts) == len(set(firsts)), "stems need unique first letters"

    def test_outcomes_bilingual_end_call_with_hooks(self):
        by_name = {f["function_name"]: f for f in self._functions()}
        for name, value in self.OUTCOMES.items():
            f = by_name[name]
            assert "language" in f["required"], name
            assert set(f["say"]["utterances"]) == {"hi", "en"}, name
            assert f["say"]["end_call"] is True, name
            hook = f["hooks"][0]
            assert hook["name"] == "update_outcome_in_database"
            assert hook["expected_fields"]["outcome"]["value"] == value

    def test_every_placeholder_resolves_from_payload(self):
        """Early-fire safety: no speech line carries an LLM-argument
        placeholder — everything resolves from lead payload values."""
        from app.ai.voice.agents.breeze_buddy.template.tool_speech import (
            substitute_placeholders,
        )

        payload = {
            "customer_name": "Rohit Kumar",
            "product_name": "Samsung Galaxy S24, 8GB RAM",
            "lender": "Fibe",
            "approved_loan_amount": "seventy five thousand rupees",
            "credit_limit": "one hundred thousand rupees",
            "applicable_tenures": "3, 6, 9, 12 months",
            "no_cost_emi_tenures": "3, 6 months",
            "no_cost_emi_applicable": "yes",
            "downpayment_amount": "four thousand nine hundred ninety nine rupees",
            "pending_steps": "auto-pay setup and agreement signing",
        }
        template = self._template()
        node = template["flow"]["nodes"][0]
        texts = [template["configurations"]["initial_greeting"]]
        texts += [m["content"] for m in node["role_messages"]]
        texts += [m["content"] for m in node["task_messages"]]
        for f in self._functions():
            texts += [
                line for lines in f["say"]["utterances"].values() for line in lines
            ]
        for text in texts:
            _rendered, unresolved = substitute_placeholders(text, {}, payload)
            assert not unresolved, f"unresolved {unresolved} in {text[:60]!r}"


class TestGatewayThinkingSwitch:
    """Gateway thinking opt-out: sglang/Qwen3 thinks by default; a template
    with "thinking": {"enabled": false} must send the chat-template kwarg —
    gateway only, never to real OpenAI."""

    def test_disable_thinking_extra_body_on_gateway(self):
        from app.ai.voice.llm.openai import OpenAIConfig, build_openai_llm

        service = build_openai_llm(
            OpenAIConfig(
                api_key="k",
                base_url="http://gateway:8000/v1",
                model="qwen3.8-27b-4bit",
                disable_thinking=True,
            )
        )
        extra = service._settings.extra
        assert extra["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    def test_extra_kwargs_are_real_sdk_params(self):
        """pipecat spreads Settings.extra into chat.completions.create(**params).
        The OpenAI SDK raises TypeError for any kwarg that is not a real
        create() parameter (live incident, 2026-09-06 14:21 call:
        'chat_template_kwargs' killed every LLM request). Whatever we put in
        extra must be in the SDK signature — extra_body is the sanctioned
        carrier for non-standard body fields."""
        import inspect

        from openai.resources.chat.completions import AsyncCompletions

        from app.ai.voice.llm.openai import OpenAIConfig, build_openai_llm

        service = build_openai_llm(
            OpenAIConfig(
                api_key="k",
                base_url="http://gateway:8000/v1",
                model="qwen3.8-27b-4bit",
                disable_thinking=True,
                reasoning_effort="low",
                tool_choice="required",
            )
        )
        sdk_params = set(inspect.signature(AsyncCompletions.create).parameters)
        for key in service._settings.extra:
            assert key in sdk_params, f"{key!r} is not a create() kwarg"

    def test_disable_thinking_never_sent_to_real_openai(self):
        from app.ai.voice.llm.openai import OpenAIConfig, build_openai_llm

        service = build_openai_llm(
            OpenAIConfig(api_key="k", model="gpt-4o", disable_thinking=True)
        )
        assert "extra_body" not in service._settings.extra
        assert "chat_template_kwargs" not in service._settings.extra

    def test_qwen_templates_opt_out_of_thinking(self):
        for name in (
            "redbus-customer-trip-feedback-toolbased.json",
            "flipkart-emi-dropoff-recovery-toolbased.json",
            "flipkart-recovery-toolbased.json",
        ):
            path = Path(__file__).resolve().parent.parent / "qwen_harness" / name
            llm = json.loads(path.read_text())["configurations"]["llm_configurations"]
            assert llm["endpoint"], name  # gateway templates only
            # DB round-trips serialize the full ThinkingConfiguration (extra
            # null fields) — what matters is thinking being explicitly OFF.
            assert llm["thinking"]["enabled"] is False, name
