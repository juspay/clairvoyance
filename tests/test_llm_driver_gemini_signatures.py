# pyrefly: ignore-errors
# SimpleNamespace stand-ins for genai chunk/part objects — attribute-shaped
# on purpose; typing them as the real SDK models adds nothing here.
"""stream_gemini thought-signature capture → ``context_message`` events.

Gemini 2.5/3 thinking attaches ``thought_signature`` bytes to response
Parts; Vertex rejects a follow-up request whose replayed functionCall
parts lack them ("Function call is missing a thought_signature"). The
driver must capture each signature with a bookmark (mirroring pipecat's
GoogleLLMService._process_context) and yield it as a
``("context_message", LLMSpecificMessage)`` event WITHOUT mutating the
context — the caller owns context mutation.

Covers the two placement styles the reference handles:
- signature on the function_call Part itself (Gemini 2.5 + both);
- signature on a trailing EMPTY-text chunk (Gemini 3 streaming), where
  the bookmark must carry all non-thought text accumulated so far.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter
from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import (
    LLMContext,
    LLMSpecificMessage,
)

from app.ai.voice.agents.breeze_buddy.chat.llm.gemini import stream as gemini_stream


def _part(
    *,
    text: Optional[str] = None,
    thought: bool = False,
    function_call: Optional[Any] = None,
    inline_data: Optional[Any] = None,
    thought_signature: Optional[bytes] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        thought=thought,
        function_call=function_call,
        inline_data=inline_data,
        thought_signature=thought_signature,
    )


def _fc(name: str, args: Optional[dict] = None, id: Optional[str] = None):
    return SimpleNamespace(name=name, args=args or {}, id=id)


def _chunk(parts: List[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))]
    )


def _service(chunks: List[SimpleNamespace]) -> SimpleNamespace:
    """Duck-typed GoogleLLMService exposing exactly what stream_gemini reads."""

    async def _gen():
        for chunk in chunks:
            yield chunk

    async def _generate_content_stream(**_kwargs):
        return _gen()

    return SimpleNamespace(
        get_llm_adapter=lambda: GeminiLLMAdapter(),
        _settings=SimpleNamespace(system_instruction=None, model="gemini-3.6-flash"),
        _build_generation_params=lambda **_kwargs: {},
        _maybe_unset_thinking_budget=lambda _params: None,
        _tool_config=None,
        _client=SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content_stream=_generate_content_stream)
            )
        ),
    )


async def _drive(chunks: List[SimpleNamespace]) -> List[tuple]:
    context = LLMContext(messages=[{"role": "user", "content": "hi"}])
    events = []
    async for event in gemini_stream.stream_gemini(_service(chunks), context, "test"):
        events.append(event)
    return events


async def test_function_call_signature_yields_context_message():
    chunks = [
        _chunk(
            [
                _part(
                    function_call=_fc("search_catalog", {"query": "bras"}, id="fc-1"),
                    thought_signature=b"\x01sig",
                )
            ]
        )
    ]
    events = await _drive(chunks)
    kinds = [k for k, _ in events]
    assert kinds == ["tool_call", "context_message"]

    call = events[0][1]
    assert isinstance(call, FunctionCallFromLLM)
    assert call.tool_call_id == "fc-1"

    msg = events[1][1]
    assert isinstance(msg, LLMSpecificMessage)
    assert msg.llm == "google"
    assert msg.message == {
        "type": "thought_signature",
        "signature": b"\x01sig",
        "bookmark": {"function_call": "fc-1"},
    }


async def test_generated_id_flows_into_both_call_and_bookmark():
    # fc.id is None (Vertex often omits it) — the driver must generate the
    # fallback uuid ONCE and use it for the tool_call AND the bookmark,
    # or the adapter can never re-attach the signature.
    chunks = [
        _chunk(
            [
                _part(
                    function_call=_fc("get_cart", {}, id=None),
                    thought_signature=b"\x02sig",
                )
            ]
        )
    ]
    events = await _drive(chunks)
    assert [k for k, _ in events] == ["tool_call", "context_message"]
    call = events[0][1]
    msg = events[1][1]
    assert call.tool_call_id  # generated
    assert msg.message["bookmark"] == {"function_call": call.tool_call_id}


async def test_gemini3_trailing_empty_text_chunk_bookmarks_accumulated_text():
    # Gemini 3 streaming splits text across chunks and delivers the
    # signature on a trailing EMPTY-text chunk. The bookmark must carry
    # all the non-thought text seen so far in the response.
    chunks = [
        _chunk([_part(text="Here are ", thought=False)]),
        _chunk([_part(text="our sports bras.", thought=False)]),
        _chunk([_part(text="", thought_signature=b"\x03sig")]),
    ]
    events = await _drive(chunks)
    assert [k for k, _ in events] == ["text", "text", "context_message"]
    msg = events[2][1]
    assert msg.message == {
        "type": "thought_signature",
        "signature": b"\x03sig",
        "bookmark": {"text": "Here are our sports bras."},
    }


async def test_thought_text_excluded_from_stream_and_bookmark():
    chunks = [
        _chunk([_part(text="pondering...", thought=True)]),
        _chunk([_part(text="Visible answer.")]),
        _chunk([_part(text="", thought_signature=b"\x04sig")]),
    ]
    events = await _drive(chunks)
    texts = [payload for kind, payload in events if kind == "text"]
    assert texts == ["Visible answer."]
    msg = events[-1][1]
    assert msg.message["bookmark"] == {"text": "Visible answer."}


async def test_mixed_text_then_function_call_signature_on_call():
    # Common commerce shape: prose + one tool call, signature riding the
    # function_call part (the response's last Part).
    chunks = [
        _chunk([_part(text="Let me look.")]),
        _chunk(
            [
                _part(
                    function_call=_fc("search_catalog", {"query": "x"}, id="fc-9"),
                    thought_signature=b"\x05sig",
                )
            ]
        ),
    ]
    events = await _drive(chunks)
    assert [k for k, _ in events] == ["text", "tool_call", "context_message"]
    assert events[2][1].message["bookmark"] == {"function_call": "fc-9"}


async def test_no_signature_yields_no_context_message():
    chunks = [
        _chunk([_part(text="Plain reply.")]),
        _chunk([_part(function_call=_fc("get_cart", {}, id="fc-2"))]),
    ]
    events = await _drive(chunks)
    assert [k for k, _ in events] == ["text", "tool_call"]


def _capturing_service(generation_params: dict, captured: dict) -> SimpleNamespace:
    """Like _service, but with a template-shaped thinking config and a hook
    capturing the GenerateContentConfig actually sent to the SDK."""

    async def _gen():
        return
        yield  # pragma: no cover — empty stream

    async def _generate_content_stream(**kwargs):
        captured["config"] = kwargs["config"]
        return _gen()

    return SimpleNamespace(
        get_llm_adapter=lambda: GeminiLLMAdapter(),
        _settings=SimpleNamespace(system_instruction=None, model="gemini-2.5-flash"),
        _build_generation_params=lambda **_kwargs: dict(generation_params),
        _maybe_unset_thinking_budget=lambda _params: None,
        _tool_config=None,
        _client=SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content_stream=_generate_content_stream)
            )
        ),
    )


async def test_thinking_override_skipped_for_budget_dialect_models():
    """Gemini 2.5-era templates speak thinking_budget; thinking_level 400s
    there. The cycle-1 'minimal' override fires on EVERY turn, so it must
    leave budget-dialect configs untouched or every 2.5 template bricks."""
    captured: dict = {}
    svc = _capturing_service({"thinking_config": {"thinking_budget": 0}}, captured)
    context = LLMContext(messages=[{"role": "user", "content": "hi"}])
    async for _ in gemini_stream.stream_gemini(
        svc, context, "test", thinking_level_override="minimal"
    ):
        pass
    thinking = captured["config"].thinking_config
    assert getattr(thinking, "thinking_budget", None) == 0
    assert getattr(thinking, "thinking_level", None) is None


async def test_thinking_override_applies_for_level_dialect_models():
    captured: dict = {}
    svc = _capturing_service({"thinking_config": {"thinking_level": "high"}}, captured)
    context = LLMContext(messages=[{"role": "user", "content": "hi"}])
    async for _ in gemini_stream.stream_gemini(
        svc, context, "test", thinking_level_override="minimal"
    ):
        pass
    thinking = captured["config"].thinking_config
    assert str(getattr(thinking, "thinking_level", "")).lower().endswith("minimal")
