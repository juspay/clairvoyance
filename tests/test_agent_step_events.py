# pyrefly: ignore-errors
# Duck-typed monkeypatching over ChatAgent internals — same limitation as
# tests/test_agent_context_messages.py (whose harness this mirrors).
"""_cycle_loop step-progress emission (multi-step UX slice 1).

Each ungated tool execution must be bracketed by ``step_started`` (before
dispatch, running label) and ``step_completed`` (after dispatch, done label
+ status + best-effort summary/count), keyed on tool_call_id, WITHOUT
touching the existing function_call_started/completed tool-level events.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext

import app.ai.voice.agents.breeze_buddy.chat.agent as agent_module
from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

_NODE: Dict[str, Any] = {"name": "start", "functions": []}

_PREP = _PreparedTools(
    flow_config={}, global_funcs=[], tool_retention=None, tool_projection=None
)


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


def _patch_db(monkeypatch) -> None:
    async def _noop(**_kwargs):
        return None

    monkeypatch.setattr(agent_module, "insert_chat_message", _noop)
    monkeypatch.setattr(agent_module, "update_chat_session_after_turn", _noop)
    monkeypatch.setattr(agent_module, "upsert_agent_session_state_merge", _noop)


def _patch_stream(monkeypatch, cycles: List[List[tuple]]) -> None:
    calls = {"n": 0}

    async def _stream(_llm, _context, **_kwargs):
        script = cycles[calls["n"]]
        calls["n"] += 1
        for event in script:
            yield event

    monkeypatch.setattr(agent_module.llm_driver, "stream", _stream)


def _patch_dispatch(monkeypatch, result: Any) -> None:
    async def _dispatch(self, _call, _node, _funcs, injected_args=None):
        return result, None

    monkeypatch.setattr(ChatAgent, "_dispatch_tool_call", _dispatch)


def _tool_call(name: str, call_id: str) -> FunctionCallFromLLM:
    return FunctionCallFromLLM(
        function_name=name,
        tool_call_id=call_id,
        arguments={"query": "bras"},
        context=None,
    )


async def _run_turn(monkeypatch, tool_name: str, result: Any) -> List[Any]:
    _patch_stream(
        monkeypatch,
        [
            [("tool_call", _tool_call(tool_name, "fc-1"))],
            [("text", "All done.")],
        ],
    )
    _patch_db(monkeypatch)
    _patch_dispatch(monkeypatch, result)
    agent = _make_agent()
    context = LLMContext(messages=[{"role": "user", "content": "find bras"}])
    return [ev async for ev in agent._cycle_loop(context, dict(_NODE), _PREP)]


async def test_step_events_bracket_the_tool_execution(monkeypatch):
    events = await _run_turn(
        monkeypatch, "search_catalog", {"products": [{}, {}, {}, {}, {}, {}]}
    )
    names = [ev.event for ev in events]
    assert names.index("step_started") < names.index("step_completed")
    # Tool-level events unchanged and still present.
    assert "function_call_started" in names
    assert "function_call_completed" in names
    # step_started precedes the dispatch's completion pair.
    assert names.index("step_started") < names.index("function_call_completed")

    started = next(ev for ev in events if ev.event == "step_started")
    assert started.data == {
        "step_id": "fc-1",
        "label": "Searching the catalog",
        "turn_id": "turn-1",
    }

    completed = next(ev for ev in events if ev.event == "step_completed")
    assert completed.data == {
        "step_id": "fc-1",
        "status": "ok",
        "label": "Searched the catalog",
        "summary": "6 results",
        "count": 6,
    }


async def test_step_completed_error_status_and_no_summary(monkeypatch):
    events = await _run_turn(
        monkeypatch, "search_catalog", {"status": "error", "error": "boom"}
    )
    completed = next(ev for ev in events if ev.event == "step_completed")
    assert completed.data["status"] == "error"
    assert "summary" not in completed.data
    assert "count" not in completed.data


async def test_step_completed_line_items_summary(monkeypatch):
    events = await _run_turn(
        monkeypatch, "update_cart", {"id": "cart-1", "line_items": [{}, {}]}
    )
    completed = next(ev for ev in events if ev.event == "step_completed")
    assert completed.data["summary"] == "cart updated · 2 items"
    assert completed.data["count"] == 2
