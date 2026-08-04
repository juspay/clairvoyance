# pyrefly: ignore-errors
# LLMContextMessage union narrowing + duck-typed monkeypatching — same
# limitation as the sibling agent/block-codec tests.
"""_cycle_loop handling of ``("context_message", …)`` driver events.

The Gemini driver yields thought signatures as context_message events
(it never mutates context itself). The loop must (a) append them to the
in-memory LLMContext in stream order — BEFORE the assistant tool_calls
message it adds after the stream closes — and (b) persist them on the
cycle's assistant row so they survive the stateless-turn DB round-trip.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import (
    LLMContext,
    LLMSpecificMessage,
)

import app.ai.voice.agents.breeze_buddy.chat.agent as agent_module
from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.signatures import (
    GEMINI_THOUGHT_SIGNATURE_BLOCK,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

_NODE: Dict[str, Any] = {"name": "start", "functions": []}


def _make_agent() -> ChatAgent:
    template = TemplateModel.model_construct(
        id="tpl-1", name="t", flow={}, configurations=None
    )
    agent = ChatAgent(
        session_id="sess-1",
        template=template,
        llm=object(),
        template_vars={},
    )
    agent._turn_id = "turn-1"
    return agent


def _sig_msg(bookmark: dict) -> LLMSpecificMessage:
    return LLMSpecificMessage(
        llm="google",
        message={
            "type": "thought_signature",
            "signature": b"\x01sig",
            "bookmark": bookmark,
        },
    )


def _patch_db(monkeypatch) -> List[Dict[str, Any]]:
    """No-op the DB writers; capture insert_chat_message calls."""
    inserted: List[Dict[str, Any]] = []

    async def _insert(**kwargs):
        inserted.append(kwargs)
        return None

    async def _noop(**_kwargs):
        return None

    monkeypatch.setattr(agent_module, "insert_chat_message", _insert)
    monkeypatch.setattr(agent_module, "update_chat_session_after_turn", _noop)
    monkeypatch.setattr(agent_module, "upsert_agent_session_state_merge", _noop)
    return inserted


def _patch_stream(monkeypatch, cycles: List[List[tuple]]) -> None:
    """Fake llm_driver.stream yielding one scripted cycle per call."""
    calls = {"n": 0}

    async def _stream(_llm, _context, **_kwargs):
        script = cycles[calls["n"]]
        calls["n"] += 1
        for event in script:
            yield event

    monkeypatch.setattr(agent_module.llm_driver, "stream", _stream)


async def _drain(agen) -> List[Any]:
    return [event async for event in agen]


_PREP = _PreparedTools(
    flow_config={}, global_funcs=[], tool_retention=None, tool_projection=None
)


async def test_context_message_added_before_assistant_and_persisted(monkeypatch):
    sig = _sig_msg({"function_call": "fc-1"})
    call = FunctionCallFromLLM(
        function_name="search_catalog",
        tool_call_id="fc-1",
        arguments={"query": "bras"},
        context=None,
    )
    _patch_stream(
        monkeypatch,
        [
            [("tool_call", call), ("context_message", sig)],
            [("text", "All done.")],
        ],
    )
    inserted = _patch_db(monkeypatch)

    async def _dispatch(self, _call, _node, _funcs, injected_args=None):
        return {"ok": True}, None

    monkeypatch.setattr(ChatAgent, "_dispatch_tool_call", _dispatch)

    agent = _make_agent()
    context = LLMContext(messages=[{"role": "user", "content": "find bras"}])
    events = await _drain(agent._cycle_loop(context, dict(_NODE), _PREP))
    assert [e.event for e in events][-1] == "turn_end"

    messages = context.get_messages()
    sig_idx = messages.index(sig)
    assistant_idx = next(
        i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("tool_calls")
    )
    # Stream order: signature lands BEFORE the assistant tool_calls
    # message appended after stream close.
    assert sig_idx < assistant_idx
    assert messages[assistant_idx]["tool_calls"][0]["id"] == "fc-1"

    # The gate-cycle assistant row persists the signature as an internal
    # block alongside its tool_use.
    assistant_rows = [
        r for r in inserted if r.get("role") == agent_module.ChatMessageRole.ASSISTANT
    ]
    cycle_row = assistant_rows[0]
    block_types = [b["type"] for b in cycle_row["content_blocks"]]
    assert "tool_use" in block_types
    assert GEMINI_THOUGHT_SIGNATURE_BLOCK in block_types


async def test_final_cycle_text_signature_persists_on_final_row(monkeypatch):
    sig = _sig_msg({"text": "Here you go."})
    _patch_stream(
        monkeypatch,
        [[("text", "Here you go."), ("context_message", sig)]],
    )
    inserted = _patch_db(monkeypatch)

    agent = _make_agent()
    context = LLMContext(messages=[{"role": "user", "content": "hi"}])
    events = await _drain(agent._cycle_loop(context, dict(_NODE), _PREP))
    assert [e.event for e in events][-1] == "turn_end"

    # In-memory context got the signature message.
    assert sig in context.get_messages()

    assistant_rows = [
        r for r in inserted if r.get("role") == agent_module.ChatMessageRole.ASSISTANT
    ]
    assert len(assistant_rows) == 1
    block_types = [b["type"] for b in assistant_rows[0]["content_blocks"]]
    assert block_types == ["text", GEMINI_THOUGHT_SIGNATURE_BLOCK]
