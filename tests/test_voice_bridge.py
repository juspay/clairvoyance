"""Tests for WidgetVoiceBridge — widget voice driven through the chat brain.

The bridge runs the SAME ChatAgent turn as POST /message (via run_chat_turn)
and adapts the SSE event stream into TTSSpeakFrames + RTVI events. These tests
stub ``run_chat_turn`` with a controllable async generator and drive a fake
pipeline task + RTVI emit, so they exercise the bridge's adaptation logic
(sentence aggregation, marker stripping, filler-once, barge-in cancel, greeting
gating) without a live LLM or DB.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import TTSSpeakFrame

from app.ai.voice.agents.breeze_buddy.chat import voice_bridge as vb
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.voice_bridge import (
    _DEFAULT_FILLER_PHRASE,
    WidgetVoiceBridge,
    _SentenceAggregator,
)
from app.schemas.breeze_buddy.chat import ChatMessageRole


class _FakeTask:
    def __init__(self) -> None:
        self.frames: list = []

    async def queue_frame(self, frame) -> None:
        self.frames.append(frame)


class _FakeEmit:
    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, event_type, payload=None) -> None:
        self.calls.append((event_type, payload))


class _FakeLock:
    """No-op RedisLock stand-in for unit tests (no Redis). Tracks balance so a
    test can assert the lock is released even when a turn is cancelled."""

    acquired = 0
    released = 0
    raise_on_acquire = False

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def acquire(self) -> None:
        if _FakeLock.raise_on_acquire:
            raise vb.LockAcquireError("busy")
        _FakeLock.acquired += 1

    async def release(self) -> None:
        _FakeLock.released += 1


@pytest.fixture(autouse=True)
def _patch_redis_lock(monkeypatch):
    _FakeLock.acquired = 0
    _FakeLock.released = 0
    _FakeLock.raise_on_acquire = False
    monkeypatch.setattr(vb, "RedisLock", _FakeLock)
    yield


def _spoken(task: _FakeTask) -> list:
    return [f.text for f in task.frames if isinstance(f, TTSSpeakFrame)]


def _emitted(emit: _FakeEmit, event_type: str) -> list:
    return [payload for (et, payload) in emit.calls if et == event_type]


def _stub_run_chat_turn(*events: SSEEvent):
    """Build a run_chat_turn replacement that yields ``events`` in order."""

    async def _gen(*, session_id, user_content, llm=None, context_placement=None):
        for ev in events:
            yield ev

    return _gen


def _make_bridge() -> tuple[WidgetVoiceBridge, _FakeTask, _FakeEmit]:
    task = _FakeTask()
    emit = _FakeEmit()
    bridge = WidgetVoiceBridge(session_id="sess-1", task=task, emit_rtvi=emit)
    return bridge, task, emit


async def _run_one_turn(bridge: WidgetVoiceBridge, text: str = "hi") -> None:
    await bridge.handle_user_turn(text)
    inflight = bridge._inflight
    assert inflight is not None
    await inflight


# ---------------------------------------------------------------------------
# _SentenceAggregator
# ---------------------------------------------------------------------------


def test_aggregator_flushes_complete_sentences_past_min():
    agg = _SentenceAggregator(min_chars=10)
    # Below min — held.
    assert agg.push("Hi") == []
    # Crosses min and hits a terminal followed by a space → flush.
    out = agg.push(" there, friend. And more")
    assert out == ["Hi there, friend."]
    assert not agg.is_empty  # "And more" still buffered
    assert agg.flush() == ["And more"]
    assert agg.is_empty


def test_aggregator_does_not_split_decimal_midnumber():
    agg = _SentenceAggregator(min_chars=4)
    # The "." in 3.5 is followed by a digit, not whitespace → not a cut point.
    out = agg.push("It costs 3.5 dollars total. Next")
    assert out == ["It costs 3.5 dollars total."]


def test_aggregator_flush_empty_is_noop():
    agg = _SentenceAggregator()
    assert agg.flush() == []
    assert agg.has_emitted is False


# ---------------------------------------------------------------------------
# WidgetVoiceBridge — turn streaming
# ---------------------------------------------------------------------------


async def test_blank_user_turn_is_ignored(monkeypatch):
    monkeypatch.setattr(vb, "run_chat_turn", _stub_run_chat_turn())
    bridge, task, _ = _make_bridge()
    await bridge.handle_user_turn("   ")
    assert bridge._inflight is None
    assert _spoken(task) == []


async def test_speaks_sentences_and_emits_transcript(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("assistant_token", {"delta": "Hello there, friend. "}),
            SSEEvent("assistant_token", {"delta": "How are you today?"}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, task, emit = _make_bridge()
    await _run_one_turn(bridge)

    spoken = _spoken(task)
    assert spoken == ["Hello there, friend.", "How are you today?"]
    # Each spoken chunk also rides an assistant-transcript RTVI event.
    transcripts = [p["content"] for p in _emitted(emit, "assistant-transcript")]
    assert transcripts == ["Hello there, friend.", "How are you today?"]
    # turn-end is emitted once.
    assert len(_emitted(emit, "turn-end")) == 1


async def test_strips_ui_stream_markers_from_tts(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent(
                "assistant_token",
                {"delta": 'Here you go. <ui_stream>{"op":"add"}</ui_stream> Done now.'},
            ),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, task, _ = _make_bridge()
    await _run_one_turn(bridge)
    joined = " ".join(_spoken(task))
    assert "<ui_stream>" not in joined
    assert '"op"' not in joined
    assert "Here you go." in joined
    assert "Done now." in joined


async def test_ui_op_forwarded_as_rtvi(monkeypatch):
    op = {"op": "add", "id": "root", "type": "Carousel"}
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("ui_op", {"op": op}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, _, emit = _make_bridge()
    await _run_one_turn(bridge)
    assert _emitted(emit, "ui-op") == [{"op": op}]


async def test_filler_fires_once_before_prose(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("function_call_started", {"name": "get_cart"}),
            SSEEvent("function_call_started", {"name": "search"}),
            SSEEvent("assistant_token", {"delta": "Your cart has two items total."}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, task, _ = _make_bridge()
    await _run_one_turn(bridge)
    spoken = _spoken(task)
    # Exactly one filler (default phrase), before the prose, despite two tool
    # calls.
    assert spoken[0] == _DEFAULT_FILLER_PHRASE
    assert spoken.count(_DEFAULT_FILLER_PHRASE) == 1
    assert "Your cart has two items total." in spoken


async def test_no_filler_when_prose_came_first(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("assistant_token", {"delta": "Let me look that up for you. "}),
            SSEEvent("function_call_started", {"name": "get_cart"}),
            SSEEvent("assistant_token", {"delta": "You have two items."}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, task, _ = _make_bridge()
    await _run_one_turn(bridge)
    assert _DEFAULT_FILLER_PHRASE not in _spoken(task)


async def test_barge_in_cancel_drops_tail(monkeypatch):
    gate = asyncio.Event()

    async def _gen(*, session_id, user_content, llm=None, context_placement=None):
        yield SSEEvent(
            "assistant_token", {"delta": "This is the first sentence here. "}
        )
        await gate.wait()  # block until the test releases (it won't — cancelled)
        yield SSEEvent("assistant_token", {"delta": "Tail that must be dropped."})
        yield SSEEvent("turn_end", {"session_status": "ACTIVE"})

    monkeypatch.setattr(vb, "run_chat_turn", _gen)
    bridge, task, _ = _make_bridge()
    await bridge.handle_user_turn("hi")
    inflight = bridge._inflight
    assert inflight is not None
    # Let the first sentence flush, then barge in.
    await asyncio.sleep(0.02)
    assert _spoken(task) == ["This is the first sentence here."]
    await bridge.cancel_inflight()
    # _run_turn swallows the CancelledError (uncancel pattern) so the task
    # completes cleanly — the next handle_user_turn drains it.
    await bridge._drain()
    # The tail never spoke — cancel stopped the generator and bumped the
    # generation so any late frame is dropped.
    assert _spoken(task) == ["This is the first sentence here."]
    # The Redis lock acquired for the turn was released despite the cancel.
    assert _FakeLock.acquired == 1 and _FakeLock.released == 1


async def test_lock_busy_skips_turn(monkeypatch):
    _FakeLock.raise_on_acquire = True
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("assistant_token", {"delta": "This should never be spoken."}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, task, emit = _make_bridge()
    await _run_one_turn(bridge)
    # Lock contended → the turn is skipped (no TTS) and a busy error is emitted.
    assert _spoken(task) == []
    busy = [p for p in _emitted(emit, "error") if p.get("code") == "busy"]
    assert busy


async def test_lock_released_after_normal_turn(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("assistant_token", {"delta": "All done here."}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, _, _ = _make_bridge()
    await _run_one_turn(bridge)
    assert _FakeLock.acquired == 1 and _FakeLock.released == 1


async def test_error_event_emits_rtvi_error(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("error", {"code": "boom", "message": "kaboom"}),
            SSEEvent("turn_end", {"session_status": "FAILED"}),
        ),
    )
    bridge, _, emit = _make_bridge()
    await _run_one_turn(bridge)
    errors = _emitted(emit, "error")
    assert errors and errors[0]["message"] == "kaboom"


# ---------------------------------------------------------------------------
# WidgetVoiceBridge — terminal-event guarantee (every turn emits exactly one)
# ---------------------------------------------------------------------------


async def test_interrupted_turn_still_emits_terminal_turn_end(monkeypatch):
    """Root-cause guard: an interrupted turn (barge-in / teardown / a ui-action
    tap that cancels the in-flight turn) must STILL emit exactly one terminal
    ``turn-end`` — even though the brain's own ``turn_end`` never arrives — so
    the client is never left hanging (stuck 'Speaking' / streaming bubble)."""
    gate = asyncio.Event()

    async def _gen(*, session_id, user_content, llm=None, context_placement=None):
        yield SSEEvent("assistant_token", {"delta": "First sentence here. "})
        await gate.wait()  # cancelled before this releases
        yield SSEEvent("turn_end", {"session_status": "ACTIVE"})

    monkeypatch.setattr(vb, "run_chat_turn", _gen)
    bridge, _, emit = _make_bridge()
    await bridge.handle_user_turn("hi")
    await asyncio.sleep(0.02)
    await bridge.cancel_inflight()
    await bridge._drain()
    ends = _emitted(emit, "turn-end")
    assert len(ends) == 1 and ends[0] == {"status": None}


async def test_stream_ending_without_turn_end_synthesizes_one(monkeypatch):
    """A brain stream that completes without a ``turn_end`` event still yields a
    terminal ``turn-end`` for the client."""
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("assistant_token", {"delta": "Done, but no explicit end."}),
        ),
    )
    bridge, _, emit = _make_bridge()
    await _run_one_turn(bridge)
    assert len(_emitted(emit, "turn-end")) == 1


async def test_normal_turn_end_not_doubled(monkeypatch):
    """The brain's own ``turn_end`` must NOT be duplicated by the synthetic
    fallback — exactly one terminal event per turn."""
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("assistant_token", {"delta": "Hello."}),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, _, emit = _make_bridge()
    await _run_one_turn(bridge)
    assert len(_emitted(emit, "turn-end")) == 1


# ---------------------------------------------------------------------------
# WidgetVoiceBridge — greeting gating
# ---------------------------------------------------------------------------


def _msg(role: ChatMessageRole, content):
    return SimpleNamespace(role=role, content=content)


async def test_greeting_spoken_on_first_attach(monkeypatch):
    async def _fake_messages(session_id):
        return [_msg(ChatMessageRole.ASSISTANT, "Hi! How can I help?")]

    monkeypatch.setattr(vb, "list_chat_messages_for_session", _fake_messages)
    bridge, task, _ = _make_bridge()
    await bridge.maybe_speak_greeting()
    assert _spoken(task) == ["Hi! How can I help?"]


async def test_greeting_skipped_when_user_already_spoke(monkeypatch):
    async def _fake_messages(session_id):
        return [
            _msg(ChatMessageRole.ASSISTANT, "Hi! How can I help?"),
            _msg(ChatMessageRole.USER, "show me shoes"),
            _msg(ChatMessageRole.ASSISTANT, "Here are some shoes."),
        ]

    monkeypatch.setattr(vb, "list_chat_messages_for_session", _fake_messages)
    bridge, task, _ = _make_bridge()
    await bridge.maybe_speak_greeting()
    assert _spoken(task) == []  # mid-conversation reconnect — no re-greet


async def test_greeting_fires_despite_tool_result_user_rows(monkeypatch):
    # Tool-result rows are role=USER, content=None — they must NOT count as the
    # user having spoken (the greeting off-by-one guard).
    async def _fake_messages(session_id):
        return [
            _msg(ChatMessageRole.ASSISTANT, "Hi! How can I help?"),
            _msg(ChatMessageRole.USER, None),  # synthetic tool_result row
        ]

    monkeypatch.setattr(vb, "list_chat_messages_for_session", _fake_messages)
    bridge, task, _ = _make_bridge()
    await bridge.maybe_speak_greeting()
    assert _spoken(task) == ["Hi! How can I help?"]


# ---------------------------------------------------------------------------
# WidgetVoiceBridge — HITL (voice approval cards)
# ---------------------------------------------------------------------------


async def test_forwards_approval_request_to_rtvi(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent("assistant_token", {"delta": "Let me get that approved. "}),
            SSEEvent(
                "function_approval_requested",
                {
                    "tool_call_id": "tc1",
                    "name": "request_human_callback",
                    "args": {"phone": "x"},
                    "prompt": "Connect you to a human?",
                },
            ),
            SSEEvent(
                "turn_end", {"session_status": "ACTIVE", "awaiting_approval": True}
            ),
        ),
    )
    bridge, task, emit = _make_bridge()
    await _run_one_turn(bridge)
    spoken = _spoken(task)
    # Prose before the gate is still spoken...
    assert "Let me get that approved." in spoken
    # ...the approval prompt is spoken aloud so voice users hear the question...
    assert "Connect you to a human?" in spoken
    # ...and the approval card is surfaced live with widget-shaped fields.
    reqs = _emitted(emit, "function-approval-request")
    assert reqs and reqs[0] == {
        "approval_id": "tc1",
        "function_name": "request_human_callback",
        "arguments": {"phone": "x"},
        "prompt": "Connect you to a human?",
    }


async def test_forwards_function_call_lifecycle_to_rtvi(monkeypatch):
    monkeypatch.setattr(
        vb,
        "run_chat_turn",
        _stub_run_chat_turn(
            SSEEvent(
                "function_call_started",
                {
                    "name": "search_products",
                    "args": {"q": "bottle"},
                    "tool_call_id": "tc1",
                },
            ),
            SSEEvent(
                "function_call_completed",
                {
                    "name": "search_products",
                    "tool_call_id": "tc1",
                    "result_summary": "ok",
                },
            ),
            SSEEvent("turn_end", {"session_status": "ACTIVE"}),
        ),
    )
    bridge, task, emit = _make_bridge()
    await _run_one_turn(bridge)
    # The tool's start/finish are surfaced to the widget so the orb can show a
    # "executing <tool>" state (start carries name + args; completion clears it).
    started = _emitted(emit, "function-call-started")
    completed = _emitted(emit, "function-call-completed")
    assert started and started[0] == {
        "name": "search_products",
        "args": {"q": "bottle"},
        "tool_call_id": "tc1",
    }
    assert completed and completed[0] == {
        "name": "search_products",
        "tool_call_id": "tc1",
    }


async def test_handle_approval_decision_drives_resume(monkeypatch):
    captured: dict = {}

    async def _stub_approval(
        *, session_id, tool_call_id, approved, reason=None, llm=None
    ):
        captured.update(
            session_id=session_id, tool_call_id=tool_call_id, approved=approved
        )
        yield SSEEvent(
            "function_approval_resolved",
            {"tool_call_id": tool_call_id, "status": "approved", "reason": None},
        )
        yield SSEEvent("assistant_token", {"delta": "Done — connecting you now."})
        yield SSEEvent("turn_end", {"session_status": "ACTIVE"})

    monkeypatch.setattr(vb, "run_chat_approval_turn", _stub_approval)
    bridge, task, emit = _make_bridge()
    await bridge.handle_approval_decision("tc1", True, None)
    inflight = bridge._inflight
    assert inflight is not None
    await inflight

    assert captured == {"session_id": "sess-1", "tool_call_id": "tc1", "approved": True}
    resolved = _emitted(emit, "function-approval-resolved")
    assert resolved and resolved[0]["approval_id"] == "tc1"
    assert resolved[0]["status"] == "approved"
    assert "Done — connecting you now." in _spoken(task)
    # Lock acquired + released for the approval turn too.
    assert _FakeLock.acquired == 1 and _FakeLock.released == 1


async def test_handle_approval_decision_ignores_blank_id(monkeypatch):
    monkeypatch.setattr(vb, "run_chat_approval_turn", _stub_run_chat_turn())
    bridge, _, _ = _make_bridge()
    await bridge.handle_approval_decision(None, True, None)
    assert bridge._inflight is None
