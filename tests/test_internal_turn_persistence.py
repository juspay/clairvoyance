# pyrefly: ignore-errors
# Duck-typed monkeypatching over ChatAgent internals — same limitation as
# tests/test_agent_step_events.py (whose harness this mirrors).
"""Internal AGENT_TURN persistence (IntentPolicy.internal — RFC-001 §3.3).

The enrich_product overlay blurb runs a REAL LLM turn whose exchange must
never replay visibly: the rewritten instruction persists as an internal
user block (no ``user_committed``), the assistant prose persists as an
internal block with ``content=None`` / ``ui_blocks=None``, and — should
the model stray into a tool call — the tool-cycle row demotes its prose
to internal too. The live SSE stream is unchanged (tokens still flow);
only persistence and the user echo differ.
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


import app.ai.voice.agents.breeze_buddy.chat.turn_core as turn_core
from app.ai.voice.agents.breeze_buddy.chat.agent import ChatAgent, _PreparedTools
from app.ai.voice.agents.breeze_buddy.chat.history.block_codec import (
    VISIBILITY_INTERNAL,
)
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


def _record_db(monkeypatch) -> List[Dict[str, Any]]:
    """Capture every insert_chat_message call's kwargs, in order."""
    rows: List[Dict[str, Any]] = []

    async def _insert(**kwargs):
        rows.append(kwargs)
        return None

    async def _noop(**_kwargs):
        return None

    _patch_agent_attr(monkeypatch, "insert_chat_message", _insert)
    _patch_agent_attr(monkeypatch, "update_chat_session_after_turn", _noop)
    _patch_agent_attr(monkeypatch, "upsert_agent_session_state_merge", _noop)
    return rows


def _patch_stream(monkeypatch, cycles: List[List[tuple]]) -> None:
    calls = {"n": 0}

    async def _stream(_llm, _context, **_kwargs):
        script = cycles[calls["n"]]
        calls["n"] += 1
        for event in script:
            yield event

    monkeypatch.setattr(llm_driver, "stream", _stream)


def _internal_blocks(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        b
        for b in (row.get("content_blocks") or [])
        if b.get("visibility") == VISIBILITY_INTERNAL
    ]


def _visible_text_blocks(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        b
        for b in (row.get("content_blocks") or [])
        if b.get("type") == "text" and b.get("visibility") != VISIBILITY_INTERNAL
    ]


# ---------------------------------------------------------------------------
# _cycle_loop — assistant persistence
# ---------------------------------------------------------------------------


async def test_internal_turn_final_row_is_internal_only(monkeypatch):
    """Prose-only internal turn: tokens stream live, but the assistant row
    persists content=None + internal text block + no ui_blocks."""
    _patch_stream(monkeypatch, [[("text", "Great pick — runs true to size.")]])
    rows = _record_db(monkeypatch)
    agent = _make_agent()
    agent._internal_turn = True
    context = LLMContext(messages=[{"role": "user", "content": "enrich"}])
    events = [ev async for ev in agent._cycle_loop(context, dict(_NODE), _PREP)]

    deltas = "".join(ev.data["delta"] for ev in events if ev.event == "assistant_token")
    assert "runs true to size" in deltas  # live stream unchanged

    assert len(rows) == 1
    row = rows[0]
    assert row["content"] is None
    assert row["ui_blocks"] is None
    assert not _visible_text_blocks(row)
    internal = _internal_blocks(row)
    assert len(internal) == 1
    assert "runs true to size" in internal[0]["text"]


async def test_normal_turn_final_row_stays_visible(monkeypatch):
    """Regression guard: the default path is untouched."""
    _patch_stream(monkeypatch, [[("text", "Here you go.")]])
    rows = _record_db(monkeypatch)
    agent = _make_agent()
    context = LLMContext(messages=[{"role": "user", "content": "hi"}])
    [ev async for ev in agent._cycle_loop(context, dict(_NODE), _PREP)]

    assert len(rows) == 1
    assert rows[0]["content"] == "Here you go."
    assert len(_visible_text_blocks(rows[0])) == 1


async def test_internal_turn_tool_cycle_row_demotes_prose(monkeypatch):
    """The enrich prompt forbids tools, but if the model strays the
    tool-cycle row must not leak visible prose into resume replay."""
    call = FunctionCallFromLLM(
        function_name="get_product",
        tool_call_id="fc-1",
        arguments={},
        context=None,
    )
    _patch_stream(
        monkeypatch,
        [
            [("text", "Let me check."), ("tool_call", call)],
            [("text", "It pairs well with the trail shorts.")],
        ],
    )
    rows = _record_db(monkeypatch)

    async def _dispatch(self, _call, _node, _funcs, injected_args=None):
        return {"status": "success", "data": {}}, None

    monkeypatch.setattr(ChatAgent, "_dispatch_tool_call", _dispatch)

    agent = _make_agent()
    agent._internal_turn = True
    context = LLMContext(messages=[{"role": "user", "content": "enrich"}])
    [ev async for ev in agent._cycle_loop(context, dict(_NODE), _PREP)]

    # Row order: assistant(tool_use) gate row, user(tool_result), final.
    from app.schemas.breeze_buddy.chat import ChatMessageRole

    assistant_rows = [r for r in rows if r.get("role") == ChatMessageRole.ASSISTANT]
    assert len(assistant_rows) == 2
    for row in assistant_rows:
        assert row["content"] is None
        assert row["ui_blocks"] is None
        assert not _visible_text_blocks(row)
    # Prose survived — internally.
    gate_internal = _internal_blocks(assistant_rows[0])
    assert any("Let me check." in b["text"] for b in gate_internal)
    final_internal = _internal_blocks(assistant_rows[1])
    assert any("pairs well" in b["text"] for b in final_internal)


# ---------------------------------------------------------------------------
# run_turn — user-row persistence + user_committed suppression
# ---------------------------------------------------------------------------


async def _run_turn_capture(monkeypatch, *, internal: bool):
    rows = _record_db(monkeypatch)

    async def _prepare_tools(self):
        return _PREP

    def _resolve_node(self, _flow, _current):
        return dict(_NODE)

    async def _no_kb(self, _content, _history):
        return None

    def _seed(self, _node, _history, user_content, _funcs, kb_message=None):
        return LLMContext(messages=[{"role": "user", "content": user_content}])

    async def _cycle(self, _context, _node, _prep):
        return
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(ChatAgent, "_prepare_tools", _prepare_tools)
    monkeypatch.setattr(ChatAgent, "_resolve_node", _resolve_node)
    monkeypatch.setattr(ChatAgent, "_prepare_kb_message", _no_kb)
    monkeypatch.setattr(ChatAgent, "_seed_context", _seed)
    monkeypatch.setattr(ChatAgent, "_cycle_loop", _cycle)

    agent = _make_agent()
    events = [
        ev
        async for ev in agent.run_turn(
            user_content="[Background enrichment] tell me more",
            history=[],
            current_node=None,
            internal=internal,
        )
    ]
    return rows, events


async def test_internal_turn_user_row_internal_and_no_user_committed(monkeypatch):
    rows, events = await _run_turn_capture(monkeypatch, internal=True)
    assert all(ev.event != "user_committed" for ev in events)
    assert len(rows) == 1
    row = rows[0]
    assert row["content"] is None
    internal = _internal_blocks(row)
    assert len(internal) == 1
    assert "Background enrichment" in internal[0]["text"]
    assert not _visible_text_blocks(row)


async def test_normal_turn_user_row_visible_and_committed(monkeypatch):
    rows, events = await _run_turn_capture(monkeypatch, internal=False)
    assert any(ev.event == "user_committed" for ev in events)
    assert rows[0]["content"] == "[Background enrichment] tell me more"
    assert len(_visible_text_blocks(rows[0])) == 1


# ---------------------------------------------------------------------------
# run_chat_turn — internal turns must not supersede pending approvals
# ---------------------------------------------------------------------------


async def _drive_run_chat_turn(monkeypatch, *, internal: bool) -> Dict[str, Any]:
    """Run run_chat_turn end-to-end with every collaborator stubbed,
    recording the supersede-call count and the internal flag the agent
    receives."""
    from types import SimpleNamespace

    from app.schemas.breeze_buddy.chat import ChatSessionStatus

    calls: Dict[str, Any] = {"supersede": 0, "agent_internal": None}

    async def _get_session(_sid):
        return SimpleNamespace(
            status=ChatSessionStatus.ACTIVE,
            template_id="tpl-1",
            metadata={},
            current_node=None,
            # None = unmetered — billing gate/deduction no-op in-harness.
            merchant_id=None,
        )

    async def _get_template(_tid):
        return TemplateModel.model_construct(
            id="tpl-1", name="t", flow={}, configurations=None, reseller_id="r1"
        )

    async def _resolve(_sid, only_expired):
        calls["supersede"] += 1
        return []

    async def _history_limit():
        return 50

    async def _list_messages(_sid, limit=None):
        return []

    async def _get_state(_sid):
        return None

    async def _render_vars(_template, _persisted):
        return {}

    async def _get_llm(_config, pooled=True):
        return object()

    async def _run_turn(self, *, user_content, history, current_node, internal=False):
        calls["agent_internal"] = internal
        return
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(turn_core, "get_chat_session_by_id", _get_session)
    monkeypatch.setattr(turn_core, "get_template_by_id_cached", _get_template)
    monkeypatch.setattr(turn_core, "resolve_dangling_approvals", _resolve)
    monkeypatch.setattr(turn_core, "CHAT_HISTORY_REPLAY_LIMIT", _history_limit)
    monkeypatch.setattr(turn_core, "list_chat_messages_for_session", _list_messages)
    monkeypatch.setattr(turn_core, "get_agent_session_state", _get_state)
    monkeypatch.setattr(turn_core, "build_render_template_vars", _render_vars)
    monkeypatch.setattr(turn_core, "get_llm_service", _get_llm)
    monkeypatch.setattr(ChatAgent, "run_turn", _run_turn)

    [
        ev
        async for ev in turn_core.run_chat_turn(
            session_id="s1", user_content="x", internal=internal
        )
    ]
    return calls


async def test_internal_run_chat_turn_skips_approval_supersede(monkeypatch):
    calls = await _drive_run_chat_turn(monkeypatch, internal=True)
    assert calls["supersede"] == 0
    assert calls["agent_internal"] is True


async def test_normal_run_chat_turn_still_supersedes(monkeypatch):
    calls = await _drive_run_chat_turn(monkeypatch, internal=False)
    assert calls["supersede"] == 1
    assert calls["agent_internal"] is False
