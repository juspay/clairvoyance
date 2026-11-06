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
from app.ai.voice.agents.breeze_buddy.chat.agent import (
    approval as agent_approval,
    context as agent_context,
    core as agent_core,
    cycle as agent_cycle,
    render_ui as agent_render_ui,
    tooling as agent_tooling,
)
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver

# agent.py is a package of subsystem modules now — a patched seam must
# land on every submodule that calls it (autoflake prunes unused
# imports per module, hence the hasattr guard).
_AGENT_MODULES = (
    agent_core,
    agent_cycle,
    agent_approval,
    agent_render_ui,
    agent_context,
    agent_tooling,
)


def _patch_agent_attr(monkeypatch, name, value):
    for _mod in _AGENT_MODULES:
        if hasattr(_mod, name):
            monkeypatch.setattr(_mod, name, value)


from types import SimpleNamespace  # isort: skip

from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

_NODE: Dict[str, Any] = {"name": "start", "functions": []}

_PREP = _PreparedTools(
    flow_config={}, global_funcs=[], tool_retention=None, tool_projection=None
)


def _make_agent() -> ChatAgent:
    # Enables the "probe" group so the summarizer registered in
    # test_registered_flavor_summarizer_feeds_step_completed is in scope.
    template = TemplateModel.model_construct(
        id="tpl-1",
        name="t",
        flow={},
        configurations=SimpleNamespace(
            state_reducers=[],
            tool_arg_injection=[],
            client_context=None,
            ui_catalog=SimpleNamespace(
                enabled_groups=["core", "probe"],
                enabled_primitives=None,
                disabled_primitives=None,
            ),
            ui_intents=None,
            tool_execution=None,
        ),
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

    _patch_agent_attr(monkeypatch, "insert_chat_message", _noop)
    _patch_agent_attr(monkeypatch, "update_chat_session_after_turn", _noop)
    _patch_agent_attr(monkeypatch, "upsert_agent_session_state_merge", _noop)


def _patch_stream(monkeypatch, cycles: List[List[tuple]]) -> None:
    calls = {"n": 0}

    async def _stream(_llm, _context, **_kwargs):
        script = cycles[calls["n"]]
        calls["n"] += 1
        for event in script:
            yield event

    monkeypatch.setattr(llm_driver, "stream", _stream)


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
    # Flavor-neutral tool + result shape on purpose: flavor summarizer
    # registries are process-global, so a commerce-shaped payload here
    # would pick up a summary once any flavor test has loaded its group.
    events = await _run_turn(
        monkeypatch, "search_records", {"records": [{}, {}, {}, {}, {}, {}]}
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
        "label": "Searching the records",
        "turn_id": "turn-1",
    }

    completed = next(ev for ev in events if ev.event == "step_completed")
    # No flavor summarizer recognizes this shape → the event omits
    # summary/count entirely (flavors register their own — see
    # register_step_summarizer).
    assert completed.data == {
        "step_id": "fc-1",
        "status": "ok",
        "label": "Searched the records",
    }


async def test_step_completed_error_status_and_no_summary(monkeypatch):
    events = await _run_turn(
        monkeypatch, "search_catalog", {"status": "error", "error": "boom"}
    )
    completed = next(ev for ev in events if ev.event == "step_completed")
    assert completed.data["status"] == "error"
    assert "summary" not in completed.data
    assert "count" not in completed.data


async def test_registered_flavor_summarizer_feeds_step_completed(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.chat.steps import labels as step_labels

    def _rows_summarizer(result):
        rows = result.get("rows")
        if isinstance(rows, list):
            return f"{len(rows)} rows", len(rows)
        return None, None

    # Registered under the group this test's template enables — a
    # summarizer is only ever consulted for sessions in its own flavor.
    step_labels.register_step_summarizer("probe", _rows_summarizer)
    try:
        events = await _run_turn(monkeypatch, "fetch_report", {"rows": [{}, {}]})
        completed = next(ev for ev in events if ev.event == "step_completed")
        assert completed.data["summary"] == "2 rows"
        assert completed.data["count"] == 2
    finally:
        step_labels._STEP_SUMMARIZERS["probe"].remove(_rows_summarizer)
