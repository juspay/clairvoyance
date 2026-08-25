"""Tests for run_chat_turn — the channel-agnostic chat brain.

run_chat_turn is the generator both POST /message and the widget voice bridge
drive. These tests mock its DB/LLM boundary and assert: terminal error events
on a missing/ended session, the approval-supersede events riding at the start
of the stream, and clean delegation to ChatAgent.run_turn for the happy path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, cast

import pytest

from app.ai.voice.agents.breeze_buddy.chat import turn_core as tc
from app.ai.voice.agents.breeze_buddy.chat.approvals import (
    WIRE_STATUS_SUPERSEDED,
    ApprovalClaim,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.schemas.breeze_buddy.chat import ChatSessionStatus


async def _collect(agen) -> List[SSEEvent]:
    return [ev async for ev in agen]


def _patch_common(monkeypatch, *, superseded=None, agent_events=None):
    """Patch the run_chat_turn boundary for the happy path."""

    async def _limit():
        return 50

    async def _messages(session_id, limit=None):
        return []

    async def _state(session_id):
        return None

    async def _vars(template, persisted):
        return {}

    async def _llm(cfg, pooled=False):
        return object()

    async def _resolve(session_id, only_expired=False):
        return superseded or []

    class _FakeChatAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run_turn(
            self, *, user_content, history, current_node, internal=False
        ):
            for ev in agent_events or []:
                yield ev

    monkeypatch.setattr(tc, "CHAT_HISTORY_REPLAY_LIMIT", _limit)
    monkeypatch.setattr(tc, "list_chat_messages_for_session", _messages)
    monkeypatch.setattr(tc, "get_agent_session_state", _state)
    monkeypatch.setattr(tc, "build_render_template_vars", _vars)
    monkeypatch.setattr(tc, "get_llm_service", _llm)
    monkeypatch.setattr(tc, "resolve_dangling_approvals", _resolve)
    monkeypatch.setattr(tc, "blocks_to_llm_context_messages", lambda rows: [])
    monkeypatch.setattr(tc, "repair_dangling_tool_uses", lambda h: h)
    monkeypatch.setattr(tc, "ChatAgent", _FakeChatAgent)


def _active_session():
    return SimpleNamespace(
        status=ChatSessionStatus.ACTIVE,
        current_node="start",
        template_id="t1",
        metadata={"template_vars": {}},
        # None = unmetered — the billing gate/deduction are no-ops, which
        # is the right posture for these harness tests.
        merchant_id=None,
    )


async def test_missing_session_yields_error_and_turn_end(monkeypatch):
    async def _none(session_id):
        return None

    monkeypatch.setattr(tc, "get_chat_session_by_id", _none)
    events = await _collect(run_then(tc, session_id="missing"))
    assert [e.event for e in events] == ["error", "turn_end"]
    assert events[0].data["code"] == "session_not_found"
    assert events[1].data["session_status"] == "FAILED"


async def test_ended_session_yields_error_and_turn_end(monkeypatch):
    async def _ended(session_id):
        return SimpleNamespace(
            status=ChatSessionStatus.ENDED,
            current_node="start",
            template_id="t1",
            metadata={},
        )

    monkeypatch.setattr(tc, "get_chat_session_by_id", _ended)
    events = await _collect(run_then(tc, session_id="s"))
    assert [e.event for e in events] == ["error", "turn_end"]
    assert events[0].data["code"] == "session_ended"


async def test_missing_template_yields_error(monkeypatch):
    async def _session(session_id):
        return _active_session()

    async def _no_template(template_id):
        return None

    monkeypatch.setattr(tc, "get_chat_session_by_id", _session)
    monkeypatch.setattr(tc, "get_template_by_id_cached", _no_template)
    events = await _collect(run_then(tc, session_id="s"))
    assert [e.event for e in events] == ["error", "turn_end"]
    assert events[0].data["code"] == "template_missing"


async def test_supersede_events_ride_first(monkeypatch):
    async def _session(session_id):
        return _active_session()

    async def _template(template_id):
        return SimpleNamespace(id="t1")

    monkeypatch.setattr(tc, "get_chat_session_by_id", _session)
    monkeypatch.setattr(tc, "get_template_by_id_cached", _template)
    _patch_common(
        monkeypatch,
        superseded=[SimpleNamespace(tool_call_id="tc1", reason="moved on")],
        agent_events=[
            SSEEvent("assistant_token", {"delta": "hi"}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ],
    )
    events = await _collect(run_then(tc, session_id="s"))
    # The superseded approval is resolved first, then the agent's turn.
    assert events[0].event == "function_approval_resolved"
    assert events[0].data["status"] == WIRE_STATUS_SUPERSEDED
    assert events[0].data["tool_call_id"] == "tc1"
    assert [e.event for e in events[1:]] == ["assistant_token", "turn_end"]


async def test_delegates_to_chat_agent_when_no_pending(monkeypatch):
    async def _session(session_id):
        return _active_session()

    async def _template(template_id):
        return SimpleNamespace(id="t1")

    monkeypatch.setattr(tc, "get_chat_session_by_id", _session)
    monkeypatch.setattr(tc, "get_template_by_id_cached", _template)
    _patch_common(
        monkeypatch,
        superseded=[],
        agent_events=[
            SSEEvent("assistant_message", {"idx": 1, "content": "done"}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ],
    )
    events = await _collect(run_then(tc, session_id="s"))
    assert [e.event for e in events] == ["assistant_message", "turn_end"]


def run_then(module, *, session_id):
    """run_chat_turn invocation helper (keeps the await off the call site)."""
    return module.run_chat_turn(session_id=session_id, user_content="hi")


# ---------------------------------------------------------------------------
# run_chat_approval_turn (the voice bridge's HITL entry point)
# ---------------------------------------------------------------------------


async def test_approval_turn_not_found(monkeypatch):
    async def _claim(session_id, tool_call_id, approved, reason):
        return ApprovalClaim(outcome="not_found")

    monkeypatch.setattr(tc, "claim_tool_approval", _claim)
    events = await _collect(
        tc.run_chat_approval_turn(session_id="s", tool_call_id="tc1", approved=True)
    )
    assert [e.event for e in events] == ["function_approval_resolved", "turn_end"]
    assert events[0].data["status"] == "cancelled"


async def test_approval_turn_already_decided(monkeypatch):
    async def _claim(session_id, tool_call_id, approved, reason):
        return ApprovalClaim(outcome="already_decided", winning_status="denied")

    monkeypatch.setattr(tc, "claim_tool_approval", _claim)
    events = await _collect(
        tc.run_chat_approval_turn(session_id="s", tool_call_id="tc1", approved=False)
    )
    assert events[0].event == "function_approval_resolved"
    assert events[0].data["status"] == "denied"


async def test_approval_turn_proceed_delegates(monkeypatch):
    claimed = SimpleNamespace(tool_call_id="tc1", function_name="f")

    async def _claim(session_id, tool_call_id, approved, reason):
        return ApprovalClaim(
            outcome="proceed",
            claimed=cast(Any, claimed),
            effective_approved=True,
            wire_status="approved",
            expired_siblings=cast(
                Any, [SimpleNamespace(tool_call_id="sib1", reason="expired")]
            ),
        )

    async def _continuation(**kwargs):
        yield SSEEvent("assistant_message", {"idx": 1, "content": "ok"})
        yield SSEEvent("turn_end", {"session_status": "ACTIVE"})

    monkeypatch.setattr(tc, "claim_tool_approval", _claim)
    monkeypatch.setattr(tc, "run_chat_approval_continuation", _continuation)
    events = await _collect(
        tc.run_chat_approval_turn(session_id="s", tool_call_id="tc1", approved=True)
    )
    # The expired sibling is resolved first, then the continuation streams.
    assert events[0].event == "function_approval_resolved"
    assert events[0].data["tool_call_id"] == "sib1"
    assert [e.event for e in events[1:]] == ["assistant_message", "turn_end"]
