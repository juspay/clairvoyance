# pyrefly: ignore-errors
# Duck-typed monkeypatching over ChatAgent internals — same limitation as
# tests/test_agent_step_events.py (whose harness this mirrors).
"""Plan-as-emission (Phase 2): the <plan> marker parser + the agent's
plan_started/plan_updated SSE wiring.

The model's plan is UX-advisory: parsed tolerantly (malformed → dropped,
never rendered), stripped from prose/persistence, labels resolved through
the same step-label registry as live step lines.
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


from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
from app.ai.voice.agents.breeze_buddy.chat.steps.plan import PlanExtractor
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

_NODE: Dict[str, Any] = {"name": "start", "functions": []}
_PREP = _PreparedTools(
    flow_config={}, global_funcs=[], tool_retention=None, tool_projection=None
)


# ---------------------------------------------------------------------------
# PlanExtractor
# ---------------------------------------------------------------------------


def test_extractor_strips_complete_plan_and_parses_it():
    ex = PlanExtractor()
    visible, plans = ex.feed(
        'Sure!\n<plan>["search_catalog","update_cart"]</plan>\nOn it.'
    )
    assert visible == "Sure!\n\nOn it."
    assert plans == [["search_catalog", "update_cart"]]


def test_extractor_handles_marker_split_across_chunks():
    ex = PlanExtractor()
    v1, p1 = ex.feed("Working. <pl")
    v2, p2 = ex.feed('an>["get_cart"]</pl')
    v3, p3 = ex.feed("an> done")
    assert (v1 + v2 + v3) == "Working.  done"
    assert p1 == [] and p2 == []
    assert p3 == [["get_cart"]]
    assert ex.flush() == ""


def test_extractor_drops_malformed_plan_silently():
    ex = PlanExtractor()
    visible, plans = ex.feed("<plan>not json</plan>ok")
    assert visible == "ok"
    assert plans == []
    # Non-string entries invalidate the whole declaration.
    visible, plans = ex.feed('<plan>["a", 3]</plan>')
    assert plans == []


def test_extractor_flush_releases_partial_prefix_as_prose():
    ex = PlanExtractor()
    visible, _ = ex.feed("total <pla")
    assert visible == "total "
    assert ex.flush() == "<pla"


def test_extractor_unterminated_block_is_dropped_on_flush():
    ex = PlanExtractor()
    visible, _ = ex.feed('<plan>["search_catalog"')
    assert visible == ""
    assert ex.flush() == ""


def test_extractor_caps_step_count():
    ex = PlanExtractor()
    body = ",".join(f'"t{i}"' for i in range(12))
    _, plans = ex.feed(f"<plan>[{body}]</plan>")
    assert len(plans[0]) == 8


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------


def _make_agent() -> ChatAgent:
    template = TemplateModel.model_construct(
        id="tpl-1", name="t", flow={}, configurations=None
    )
    agent = ChatAgent(
        session_id="sess-1", template=template, llm=object(), template_vars={}
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
        function_name=name, tool_call_id=call_id, arguments={}, context=None
    )


async def test_plan_declaration_emits_plan_started_and_strips_prose(monkeypatch):
    _patch_stream(
        monkeypatch,
        [
            [
                ("text", '<plan>["search_catalog","update_cart"]</plan>'),
                ("tool_call", _tool_call("search_catalog", "fc-1")),
            ],
            [("text", "Found it.")],
        ],
    )
    _patch_db(monkeypatch)
    _patch_dispatch(monkeypatch, {"products": []})
    agent = _make_agent()
    context = LLMContext(messages=[{"role": "user", "content": "find and add"}])
    events = [ev async for ev in agent._cycle_loop(context, dict(_NODE), _PREP)]

    plan = next(ev for ev in events if ev.event == "plan_started")
    assert [s["tool"] for s in plan.data["steps"]] == ["search_catalog", "update_cart"]
    # Labels come from the step-label registry (running form).
    assert plan.data["steps"][0]["label"] == "Searching the catalog"
    assert plan.data["turn_id"] == "turn-1"
    # The marker never leaks into the prose stream.
    tokens = "".join(ev.data["delta"] for ev in events if ev.event == "assistant_token")
    assert "<plan>" not in tokens
    # Plan precedes the first step_started.
    names = [ev.event for ev in events]
    assert names.index("plan_started") < names.index("step_started")


async def test_second_declaration_is_plan_updated(monkeypatch):
    _patch_stream(
        monkeypatch,
        [
            [
                ("text", '<plan>["search_catalog"]</plan>'),
                ("tool_call", _tool_call("search_catalog", "fc-1")),
            ],
            [
                ("text", '<plan>["update_cart"]</plan>'),
                ("tool_call", _tool_call("update_cart", "fc-2")),
            ],
            [("text", "Done.")],
        ],
    )
    _patch_db(monkeypatch)
    _patch_dispatch(monkeypatch, {"ok": True})
    agent = _make_agent()
    context = LLMContext(messages=[{"role": "user", "content": "go"}])
    events = [ev async for ev in agent._cycle_loop(context, dict(_NODE), _PREP)]
    names = [ev.event for ev in events]
    assert names.count("plan_started") == 1
    assert names.count("plan_updated") == 1
