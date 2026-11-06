"""Tests for AdjacentMergeGeminiAdapter (the Vertex 400 pairing fix).

Upstream pipecat's ``_merge_parallel_tool_calls_for_thinking`` hoists any
UNSIGNED tool-call-only model message into the previous SIGNED parallel
group "regardless of what messages appear in between". Breeze Buddy
histories legitimately contain such messages — widget direct intents are
INJECTED function calls with no thought signature — and the hoisting
unbalances the call/response pairing, producing Gemini's

    400 INVALID_ARGUMENT "Please ensure that the number of function
    response parts is equal to the number of function call parts of the
    function call turn."

(Live repro 2026-07-28: "Add AeroShield Shorts (M)" via the cart widget,
then any typed follow-up — the first agent turn after the direct intent
always crashed once an earlier signed tool call existed.)
"""

import json
from typing import List, cast

from google.genai.types import Content, FunctionCall, Part
from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter
from pipecat.processors.aggregators.llm_context import (
    LLMContext,
    LLMContextMessage,
    LLMSpecificMessage,
)

from app.ai.voice.agents.breeze_buddy.chat.llm.gemini import (
    adapter_patch as adapter_patch_module,
)
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.adapter_patch import (
    GEMINI_PLACEHOLDER_SIGNATURE_WIRE,
    PLACEHOLDER_THOUGHT_SIGNATURE,
    AdjacentMergeGeminiAdapter,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def fc_part(name: str, call_id: str, signed: bool = False) -> Part:
    part = Part(function_call=FunctionCall(id=call_id, name=name, args={}))
    if signed:
        part.thought_signature = b"sig-bytes"
    return part


def model_fc(name: str, call_id: str, signed: bool = False) -> Content:
    return Content(role="model", parts=[fc_part(name, call_id, signed)])


def model_text(text: str) -> Content:
    return Content(role="model", parts=[Part(text=text)])


def user_text(text: str) -> Content:
    return Content(role="user", parts=[Part(text=text)])


def user_fn_response(name: str, call_id: str) -> Content:
    from google.genai.types import FunctionResponse

    return Content(
        role="user",
        parts=[
            Part(
                function_response=FunctionResponse(
                    id=call_id, name=name, response={"ok": True}
                )
            )
        ],
    )


SIG_DICTS = [{"signature": b"sig-bytes", "bookmark": {"function_call": "call_1"}}]


def assert_call_response_pairing(contents: list) -> None:
    """Gemini contract: every model turn with N functionCall parts must be
    immediately followed by a turn carrying exactly N functionResponse
    parts."""
    for i, content in enumerate(contents):
        n_calls = sum(
            1 for p in (content.parts or []) if getattr(p, "function_call", None)
        )
        if not n_calls:
            continue
        assert i + 1 < len(contents), "dangling function call turn"
        n_resp = sum(
            1
            for p in (contents[i + 1].parts or [])
            if getattr(p, "function_response", None)
        )
        assert (
            n_resp == n_calls
        ), f"turn {i}: {n_calls} function call(s) vs {n_resp} response(s)"


def bug_history() -> list:
    """The exact live layout that crashed: signed search call, its
    response, prose, a user turn, then the INJECTED (unsigned) direct
    intent call and its response."""
    return [
        model_text("greeting"),
        user_text("Do you have any panties"),
        model_fc("search_catalog", "call_1", signed=True),
        user_fn_response("search_catalog", "call_1"),
        model_text("We don't sell standalone panties, but..."),
        user_text('Add AeroShield Shorts (M)\n{"ui_intent": {}}'),
        model_fc("create_cart", "intent_abc"),  # injected — no signature
        user_fn_response("create_cart", "intent_abc"),
        model_text("[ui rendered: 1 CartView]"),
        user_text("Can you just give me checkout link"),
    ]


# ---------------------------------------------------------------------------
# Merge unit tests
# ---------------------------------------------------------------------------


def test_injected_call_stays_in_place():
    adapter = AdjacentMergeGeminiAdapter()
    merged = adapter._merge_parallel_tool_calls_for_thinking(SIG_DICTS, bug_history())
    assert len(merged) == len(bug_history())
    # The injected call is still turn 6, right before its response.
    parts = merged[6].parts
    assert parts and parts[0].function_call
    assert parts[0].function_call.name == "create_cart"
    assert_call_response_pairing(merged)


def test_upstream_adapter_still_hoists():
    """Documents the upstream bug the subclass exists for — if this test
    ever fails, pipecat fixed the merge and the patch can be dropped."""
    adapter = GeminiLLMAdapter()
    merged = adapter._merge_parallel_tool_calls_for_thinking(SIG_DICTS, bug_history())
    hoisted = [
        c
        for c in merged
        if sum(1 for p in (c.parts or []) if getattr(p, "function_call", None)) == 2
    ]
    assert hoisted, "upstream merge no longer hoists — patch may be removable"


def test_adjacent_split_parallel_batch_still_merges():
    """Pipecat's legitimate case: a parallel batch split across ADJACENT
    messages (signature on the first only) must still merge into one
    model turn."""
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        model_fc("search_catalog", "call_1", signed=True),
        model_fc("get_cart", "call_2"),
        user_fn_response("search_catalog", "call_1"),
        user_fn_response("get_cart", "call_2"),
    ]
    merged = adapter._merge_parallel_tool_calls_for_thinking(SIG_DICTS, messages)
    assert len(merged) == 3
    assert [
        p.function_call.name for p in (merged[0].parts or []) if p.function_call
    ] == [
        "search_catalog",
        "get_cart",
    ]


def test_no_signatures_is_identity():
    adapter = AdjacentMergeGeminiAdapter()
    messages = bug_history()
    assert adapter._merge_parallel_tool_calls_for_thinking([], messages) is messages


# ---------------------------------------------------------------------------
# Integration through get_llm_invocation_params (universal messages in,
# adapted contents out — the exact path gemini/stream.stream_gemini runs)
# ---------------------------------------------------------------------------


def test_invocation_params_balanced_after_injected_intent():
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        {"role": "user", "content": "Do you have any panties"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search_catalog",
                        "arguments": json.dumps({"query": "panties"}),
                    },
                }
            ],
        },
        LLMSpecificMessage(
            llm="google",
            message={
                "type": "thought_signature",
                "signature": b"sig-bytes",
                "bookmark": {"function_call": "call_1"},
            },
        ),
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"products": []}),
        },
        {"role": "assistant", "content": "We don't sell those, but..."},
        {"role": "user", "content": 'Add AeroShield Shorts (M)\n{"ui_intent": {}}'},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "intent_abc",
                    "type": "function",
                    "function": {
                        "name": "create_cart",
                        "arguments": json.dumps({"items": []}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "intent_abc",
            "content": json.dumps({"id": "cart-1", "line_items": []}),
        },
        {"role": "assistant", "content": "[ui rendered: 1 CartView]"},
        {"role": "user", "content": "Can you just give me checkout link"},
    ]
    context = LLMContext(messages=cast(List[LLMContextMessage], messages))
    params = adapter.get_llm_invocation_params(context)
    contents = params["messages"]
    assert_call_response_pairing(contents)
    # The search call kept its REAL signature; the injected call cannot
    # have one, so it now carries Google's documented placeholder for
    # client-injected calls (Gemini 3 strict validation would otherwise
    # 400 it — see the stamp tests below; the stamp is silent for
    # injected intents).
    sig_by_name = {
        p.function_call.name: p.thought_signature
        for c in contents
        for p in (c.parts or [])
        if getattr(p, "function_call", None)
    }
    assert sig_by_name["search_catalog"] == b"sig-bytes"
    assert sig_by_name["create_cart"] == PLACEHOLDER_THOUGHT_SIGNATURE


def test_parallel_call_responses_coalesce_into_one_user_turn():
    """Live 2026-07-31 crash #2 (the mirror-image bug): a model turn with
    TWO parallel render_ui calls persists one role=tool message per call;
    the stock adaptation emitted them as two single-response user turns —
    2 calls vs 1 response in the following turn → the same Vertex 400.
    The patched adapter coalesces consecutive response-only user contents."""
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        {"role": "user", "content": "show me leggings"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "grid_1",
                    "type": "function",
                    "function": {"name": "render_ui", "arguments": "{}"},
                },
                {
                    "id": "chips_1",
                    "type": "function",
                    "function": {"name": "render_ui", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "grid_1",
            "content": json.dumps({"status": "rendered"}),
        },
        {
            "role": "tool",
            "tool_call_id": "chips_1",
            "content": json.dumps({"status": "error", "soft": True}),
        },
        {"role": "user", "content": "hey"},
    ]
    context = LLMContext(messages=cast(List[LLMContextMessage], messages))
    params = adapter.get_llm_invocation_params(context)
    assert_call_response_pairing(params["messages"])


def test_coalesce_leaves_sequential_exchanges_and_mixed_turns_alone():
    """Direct unit pin: only ADJACENT response-only user contents merge;
    sequential exchanges (model turn between responses) and user turns
    mixing text with a response are untouched."""
    adapter = AdjacentMergeGeminiAdapter()
    from google.genai.types import FunctionResponse

    mixed_user = Content(
        role="user",
        parts=[
            Part(text="context note"),
            Part(function_response=FunctionResponse(id="c1", name="t", response={})),
        ],
    )
    sequential = [
        model_fc("t", "a1"),
        user_fn_response("t", "a1"),
        model_fc("t", "a2"),
        user_fn_response("t", "a2"),
        mixed_user,
    ]
    out = adapter._coalesce_function_responses(sequential)
    assert [len(c.parts or []) for c in out] == [1, 1, 1, 1, 2]

    parallel = [
        Content(role="model", parts=[fc_part("t", "p1"), fc_part("t", "p2")]),
        user_fn_response("t", "p1"),
        user_fn_response("t", "p2"),
        user_text("hey"),
    ]
    out = adapter._coalesce_function_responses(parallel)
    assert len(out) == 3
    assert (
        sum(1 for p in (out[1].parts or []) if getattr(p, "function_response", None))
        == 2
    )


def test_chat_adapter_swap_is_instance_local_and_voice_stays_stock():
    """The patched adapter is wired ONLY through the chat driver's
    per-instance swap — the shared voice service classes must keep the
    stock pipecat adapter (chat-driven patches must never leak into the
    voice pipeline)."""
    from pipecat.services.google.llm import GoogleLLMService
    from pipecat.services.google.vertex.llm import GoogleVertexLLMService

    from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.adapter_patch import (
        ensure_chat_gemini_adapter,
    )

    # Voice-side pin: stock classes are unpatched.
    assert GoogleLLMService.adapter_class is GeminiLLMAdapter
    assert GoogleVertexLLMService.adapter_class is GeminiLLMAdapter

    # Chat-side swap: instance-local, idempotent.
    svc = object.__new__(GoogleVertexLLMService)  # bypass network __init__
    svc._adapter = GeminiLLMAdapter()
    out = ensure_chat_gemini_adapter(svc)
    assert out is svc
    assert isinstance(svc.get_llm_adapter(), AdjacentMergeGeminiAdapter)
    patched = svc.get_llm_adapter()
    ensure_chat_gemini_adapter(svc)
    assert svc.get_llm_adapter() is patched

    # Non-Gemini services pass through untouched.
    sentinel = object()
    assert ensure_chat_gemini_adapter(sentinel) is sentinel


# ---------------------------------------------------------------------------
# Thought-signature re-attachment + placeholder stamp (live 2026-08-26 crash:
# 400 "Function call is missing a thought_signature in functionCall parts")
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Minimal stand-in for the module logger — records warning messages."""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warning(self, message: str, *args, **kwargs) -> None:
        self.warnings.append(message)

    def debug(self, message: str, *args, **kwargs) -> None:
        pass


def sig_message(call_id: str, raw: bytes) -> LLMSpecificMessage:
    return LLMSpecificMessage(
        llm="google",
        message={
            "type": "thought_signature",
            "signature": raw,
            "bookmark": {"function_call": call_id},
        },
    )


def assistant_calls(*ids: str, name: str = "render_ui") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
            for call_id in ids
        ],
    }


def tool_result(call_id: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"status": "ok"}),
    }


def signatures_by_call_id(contents: list) -> dict:
    return {
        p.function_call.id: p.thought_signature
        for c in contents
        for p in (c.parts or [])
        if getattr(p, "function_call", None)
    }


def test_parallel_batch_signature_reattaches_to_signed_part(monkeypatch):
    """THE confirmed live crash (2026-08-26): a cycle emits parallel calls,
    Gemini signs only the FIRST functionCall part, the cycle loop appends
    ONE assistant message with all the calls — and upstream's re-attachment
    probes only ``parts[-1]``, silently dropping the captured signature
    ("Thought signatures to apply: 2 / Applied 1" in the crash log). The
    patched adapter attaches by call id on ANY part, restoring the exact
    shape Gemini emitted (first part signed, siblings bare — live-validated
    against Vertex as accepted)."""
    recorder = _LogRecorder()
    monkeypatch.setattr(adapter_patch_module, "logger", recorder)
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        {"role": "user", "content": "where is my order"},
        assistant_calls("call_1", name="get_order_status"),
        sig_message("call_1", b"real-sig-1"),
        tool_result("call_1"),
        assistant_calls("call_2", "call_3", "call_4"),
        sig_message("call_2", b"real-sig-2"),
        tool_result("call_2"),
        tool_result("call_3"),
        tool_result("call_4"),
    ]
    context = LLMContext(messages=cast(List[LLMContextMessage], messages))
    contents = adapter.get_llm_invocation_params(context)["messages"]
    assert_call_response_pairing(contents)
    sigs = signatures_by_call_id(contents)
    assert sigs == {
        "call_1": b"real-sig-1",
        "call_2": b"real-sig-2",  # first part of the batch — where Gemini put it
        "call_3": None,
        "call_4": None,
    }
    assert recorder.warnings == []


def test_upstream_last_part_probe_misses_parallel_batch():
    """Documents the upstream bug the re-attachment override exists for —
    if this ever fails, pipecat probes all parts and the override can be
    dropped."""
    adapter = GeminiLLMAdapter()
    batch = Content(role="model", parts=[fc_part("t", "c1"), fc_part("t", "c2")])
    adapter._apply_thought_signatures_to_messages(
        [{"signature": b"sig", "bookmark": {"function_call": "c1"}}], [batch]
    )
    assert batch.parts is not None
    assert not any(
        p.thought_signature for p in batch.parts
    ), "upstream now attaches to non-last parts — override may be removable"


def test_unsigned_batch_stamped_with_placeholder_and_warns(monkeypatch):
    """No signature was captured for a cycle at all (Gemini emitted none /
    the bookmark never made it into context): after re-attachment + merge
    the batch is stamped with the documented placeholder and ONE structured
    warning fires."""
    recorder = _LogRecorder()
    monkeypatch.setattr(adapter_patch_module, "logger", recorder)
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        {"role": "user", "content": "hi"},
        assistant_calls("call_1", name="get_order_status"),
        sig_message("call_1", b"real-sig-1"),
        tool_result("call_1"),
        assistant_calls("call_9", "call_10"),  # signature never captured
        tool_result("call_9"),
        tool_result("call_10"),
    ]
    context = LLMContext(messages=cast(List[LLMContextMessage], messages))
    contents = adapter.get_llm_invocation_params(context)["messages"]
    sigs = signatures_by_call_id(contents)
    assert sigs["call_1"] == b"real-sig-1"  # real signature untouched
    assert sigs["call_9"] == PLACEHOLDER_THOUGHT_SIGNATURE
    assert sigs["call_10"] == PLACEHOLDER_THOUGHT_SIGNATURE
    assert len(recorder.warnings) == 1
    assert "no_signature_captured" in recorder.warnings[0]
    assert "render_ui" in recorder.warnings[0]


def test_captured_not_reattached_tripwire(monkeypatch):
    """A bookmark referencing a call INSIDE a still-unsigned message means
    the re-attachment pass regressed — the stamp still saves the request
    and the warning names the tripwire case. (Forced here via an empty
    signature, which re-attachment skips.)"""
    recorder = _LogRecorder()
    monkeypatch.setattr(adapter_patch_module, "logger", recorder)
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        {"role": "user", "content": "hi"},
        assistant_calls("call_1", "call_2"),
        sig_message("call_1", b""),  # captured but unattachable
        sig_message("call_x", b"real"),  # keeps the fc-signature fast-path on
        tool_result("call_1"),
        tool_result("call_2"),
    ]
    context = LLMContext(messages=cast(List[LLMContextMessage], messages))
    contents = adapter.get_llm_invocation_params(context)["messages"]
    sigs = signatures_by_call_id(contents)
    assert sigs["call_1"] == PLACEHOLDER_THOUGHT_SIGNATURE
    assert sigs["call_2"] == PLACEHOLDER_THOUGHT_SIGNATURE
    assert len(recorder.warnings) == 1
    assert "captured_not_reattached" in recorder.warnings[0]


def test_injected_only_stamp_is_silent(monkeypatch):
    """Widget direct-intent injections (``intent_*`` call ids) are the
    placeholder's documented use case — stamped, but never warned about."""
    recorder = _LogRecorder()
    monkeypatch.setattr(adapter_patch_module, "logger", recorder)
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        {"role": "user", "content": "hi"},
        assistant_calls("call_1", name="search_catalog"),
        sig_message("call_1", b"real-sig-1"),
        tool_result("call_1"),
        assistant_calls("intent_abc", name="create_cart"),  # injected
        tool_result("intent_abc"),
    ]
    context = LLMContext(messages=cast(List[LLMContextMessage], messages))
    contents = adapter.get_llm_invocation_params(context)["messages"]
    sigs = signatures_by_call_id(contents)
    assert sigs["call_1"] == b"real-sig-1"
    assert sigs["intent_abc"] == PLACEHOLDER_THOUGHT_SIGNATURE
    assert recorder.warnings == []


def test_no_signature_regime_means_no_stamp(monkeypatch):
    """With no captured signatures at all (thinking off / non-signing
    model), functionCall messages stay exactly as adapted — the stamp
    never changes behavior outside an active signature regime."""
    recorder = _LogRecorder()
    monkeypatch.setattr(adapter_patch_module, "logger", recorder)
    adapter = AdjacentMergeGeminiAdapter()
    messages = [
        {"role": "user", "content": "hi"},
        assistant_calls("call_1", "call_2"),
        tool_result("call_1"),
        tool_result("call_2"),
    ]
    context = LLMContext(messages=cast(List[LLMContextMessage], messages))
    contents = adapter.get_llm_invocation_params(context)["messages"]
    sigs = signatures_by_call_id(contents)
    assert sigs == {"call_1": None, "call_2": None}
    assert recorder.warnings == []


def test_placeholder_wire_form_is_documented_literal():
    """Google documents the dummy signature as the LITERAL string in the
    request JSON; the SDK serializes ``Part.thought_signature`` bytes as
    unpadded url-safe base64 — this pins the round-trip against SDK
    upgrades (live-validated: the bare history 400s, this stamp passes)."""
    part = Part(thought_signature=PLACEHOLDER_THOUGHT_SIGNATURE)
    wire = part.model_dump(mode="json", exclude_none=True)["thought_signature"]
    assert wire == GEMINI_PLACEHOLDER_SIGNATURE_WIRE
    assert wire == "context_engineering_is_the_way_to_go"
