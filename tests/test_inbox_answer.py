# pyrefly: ignore-errors
# Duck-typed fakes over the answer's seams and ChatAgent internals — the
# same harness shape as tests/test_internal_turn_persistence.py.
"""Buddy answering inbox threads (inbox PR 3) — on every channel that
carries a conversation; WhatsApp is the one registered today.

Buddy's answer (run on the API pods, D41) turns a burst of her messages into
one turn and sends every assistant message in order as the thread channel's
text, re-checking before each part; out of credits it stays silent; one
answer per thread at a time, with one more turn when she wrote meanwhile.
The idle sweeper ends the sessions their threads let go of, with the honest
reason. The engine, on a thread-bound
session: no UI, a handoff_to_human tool only when the binding allows it,
and that tool ends the turn with the waiting message.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext

from app.ai.voice.agents.breeze_buddy.chat import cleanup
from app.ai.voice.agents.breeze_buddy.chat.agent import (
    ChatAgent,
    _PreparedTools,
    context as agent_context,
    core as agent_core,
    cycle as agent_cycle,
    handoff as agent_handoff,
    tooling as agent_tooling,
)
from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import TURN_END_REPLY_KEY
from app.ai.voice.agents.breeze_buddy.chat.inbox import (
    answer as inbox_answer,
    burst as burst_module,
    sessions,
)
from app.ai.voice.agents.breeze_buddy.chat.inbox.format import render, split
from app.ai.voice.agents.breeze_buddy.chat.inbox.formats.whatsapp import to_whatsapp
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    RenderUiConfig,
    TemplateModel,
)
from app.crm.connectivity.contracts import conversation_profile
from app.crm.conversations.contracts import (
    RESUME_CLAIM_TIMEOUT,
    RESUME_HANDED_BACK,
    BotWork,
    Thread,
    TimelineRow,
)
from app.schemas.breeze_buddy.chat import ChatEndedReason, ChatSessionStatus
from app.services.redis.locks import LockAcquireError

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
THREAD = "22222222-2222-4222-8222-222222222222"
AGENT = "7c1e0f4a-2b3c-4d5e-8f90-a1b2c3d4e5f6"
BINDING = "11111111-1111-4111-8111-111111111111"
SESSION = "55555555-5555-4555-8555-555555555555"
#: WhatsApp's text ceiling, from the channel registry.
TEXT_MAX = conversation_profile("whatsapp").text_max


def _written(n: int) -> datetime:
    """Row n's insert time — Buddy's cursor counts in it, never in the
    provider's occurred_at (a whole second, and a letter can land late)."""
    return NOW + timedelta(seconds=n, milliseconds=250)


def _row(n: int, kind: str = "text", **body: Any) -> TimelineRow:
    return TimelineRow(
        id=f"row-{n}",
        conversation_id=THREAD,
        kind=body.pop("row_kind", "inbound"),
        author_kind="customer",
        body={"type": kind, **body},
        occurred_at=NOW + timedelta(seconds=n),
        created_at=_written(n),
    )


def _work(**over: Any) -> BotWork:
    fields: Dict[str, Any] = dict(
        thread_id=THREAD,
        merchant_id="shop",
        channel="whatsapp",
        customer_id="33333333-3333-4333-8333-333333333333",
        address="+919876543210",
        binding_id=BINDING,
        agent_id=AGENT,
        session_id=SESSION,
    )
    fields.update(over)
    return BotWork(**fields)


def _thread(**over: Any) -> Thread:
    fields: Dict[str, Any] = dict(
        id=THREAD,
        merchant_id="shop",
        channel="whatsapp",
        contact_key="c",
        binding_id=BINDING,
        last_inbound_at=NOW - timedelta(hours=1),
        last_message_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
    )
    fields.update(over)
    return Thread(**fields)


# --- the channel's text -----------------------------------------------------------


def test_markdown_becomes_whatsapp_markup() -> None:
    text = to_whatsapp(
        "## Your order\n\n**Shipped** today, ~~late~~.\n\n"
        "* one\n+ two\n\nTrack it: [here](https://t.example/1)\n"
        "[https://t.example/2](https://t.example/2)"
    )
    assert text == (
        "*Your order*\n\n*Shipped* today, ~late~.\n\n- one\n- two\n\n"
        "Track it: here (https://t.example/1)\nhttps://t.example/2"
    )


def test_a_long_reply_splits_on_breaks_inside_the_ceiling() -> None:
    paragraph = " ".join(["word"] * 300)  # ~1500 chars
    text = "\n\n".join([paragraph] * 5)
    parts = split(text, TEXT_MAX)
    assert len(parts) > 1 and all(len(p) <= TEXT_MAX for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(text.split())
    assert split("x" * (TEXT_MAX + 10), TEXT_MAX) == ["x" * TEXT_MAX, "x" * 10]
    assert split("  ", TEXT_MAX) == []


def test_each_channel_gets_its_own_markup_or_the_words_as_written() -> None:
    assert render("whatsapp", "**Shipped**") == "*Shipped*"
    assert render("instagram", "**Shipped** ") == "**Shipped**"


# --- a burst is one turn -----------------------------------------------------


def test_words_make_one_turn_and_the_rest_rides_along() -> None:
    burst = burst_module.plan_burst(
        [
            _row(1, text="hi"),
            _row(2, "image", caption="this one"),
            _row(3, "sticker"),
            _row(4, "reaction", text="👍"),
            _row(5, "button", text="Track order"),
        ]
    )
    assert burst is not None and not burst.non_text_only
    assert burst.text == (
        "hi\n[sent an image] this one\n[sent a sticker]\n[sent a button] Track order"
    )
    assert burst.upto == _written(5) and burst.last_row_id == "row-5"


def test_only_unreadable_gets_the_non_text_reply_and_reactions_nothing() -> None:
    unreadable = burst_module.plan_burst([_row(1, "audio"), _row(2, "sticker")])
    assert unreadable is not None
    assert unreadable.text is None and unreadable.non_text_only
    quiet = burst_module.plan_burst([_row(1, "reaction", text="❤️"), _row(2, "system")])
    assert quiet is not None and quiet.text is None and not quiet.non_text_only
    assert burst_module.plan_burst([]) is None


def test_a_new_session_hears_what_was_said_before_it() -> None:
    earlier = [
        _row(1, text="where is my order"),
        _row(2, text="it ships today", row_kind="outbound"),
        _row(3, text="thanks, and returns?"),
    ]
    text = burst_module.turn_message("thanks, and returns?", earlier, {"row-3"})
    assert text.startswith("[Earlier in this conversation, before you joined]")
    assert "Customer: where is my order\nTeam: it ships today" in text
    assert text.endswith("[New message from the customer]\nthanks, and returns?")
    assert burst_module.turn_message("hi", [_row(1, text="hi")], {"row-1"}) == "hi"


def test_buddy_is_told_why_it_has_the_thread_back() -> None:
    """Facts only — what to say to her, if anything, is Buddy's call."""
    back = burst_module.resume_note(RESUME_HANDED_BACK, 10)
    lapsed = burst_module.resume_note(RESUME_CLAIM_TIMEOUT, 10)
    assert back == (
        "[A teammate handled this conversation and handed it back to you."
        " What the customer asked before this has been dealt with.]"
    )
    assert lapsed == (
        "[Nobody took this conversation within 10 minutes of your request for a"
        " teammate. It is back with you.]"
    )
    # told alone, or before her words, or after what a new session must hear
    assert burst_module.turn_message(None, [], set(), back) == back
    assert burst_module.turn_message("hi", [], set(), lapsed) == (
        f"{lapsed}\n\n[New message from the customer]\nhi"
    )
    earlier = [
        _row(1, text="where is it"),
        _row(2, text="ships today", row_kind="outbound"),
    ]
    told = burst_module.turn_message(None, earlier, set(), back)
    assert told.startswith("[Earlier in this conversation, before you joined]")
    assert told.endswith(f"Team: ships today\n\n{back}")


# --- how a session ended -----------------------------------------------------


@pytest.mark.parametrize(
    ("thread", "reason"),
    [
        (_thread(bot_session_id=SESSION), None),
        (_thread(binding_id="other", resolved_at=NOW), ChatEndedReason.BINDING_CHANGED),
        (_thread(assignee_user_id="u-1"), ChatEndedReason.TAKEN_OVER),
        # handed back: the thread holds a NEW session
        (_thread(bot_session_id="new-session"), ChatEndedReason.TAKEN_OVER),
        # a teammate resolved it hours before the window's end
        (_thread(resolved_at=NOW), ChatEndedReason.TAKEN_OVER),
        # the closing sweep resolved it inside the lead
        (
            _thread(resolved_at=NOW + timedelta(hours=22, minutes=30)),
            ChatEndedReason.WINDOW_CLOSED,
        ),
        (None, ChatEndedReason.WINDOW_CLOSED),
    ],
)
def test_a_released_session_ends_with_the_honest_reason(thread, reason) -> None:
    assert sessions.end_reason(SESSION, thread, BINDING, 24) == reason


class _Lock:
    busy: set = set()
    held: List[str] = []

    def __init__(self, key: str, ttl_seconds: int) -> None:
        self.key = key

    async def acquire(self) -> None:
        if self.key in _Lock.busy:
            raise LockAcquireError(self.key)
        _Lock.held.append(self.key)

    async def release(self) -> None:
        _Lock.held.remove(self.key)


@pytest.fixture(autouse=True)
def _locks(monkeypatch):
    _Lock.busy, _Lock.held = set(), []
    monkeypatch.setattr(inbox_answer, "RedisLock", _Lock)
    monkeypatch.setattr(sessions, "RedisLock", _Lock)


async def test_the_sweeper_ends_only_what_the_threads_let_go(monkeypatch) -> None:
    held_id = "11111111-0000-4000-8000-000000000001"
    open_sessions = [
        # spelled differently from the table: still its thread
        SimpleNamespace(
            id="s-held",
            merchant_id="shop",
            metadata={"conversation_id": held_id.upper()},
        ),
        SimpleNamespace(
            id="s-taken", merchant_id="shop", metadata={"conversation_id": THREAD}
        ),
        SimpleNamespace(
            id="s-busy", merchant_id="shop", metadata={"conversation_id": THREAD}
        ),
        # names no thread: nothing to read, never taken as deleted
        SimpleNamespace(
            id="s-odd", merchant_id="shop", metadata={"conversation_id": "t9"}
        ),
        SimpleNamespace(id="s-web", merchant_id=None, metadata={}),
    ]
    threads = {
        held_id: _thread(id=held_id, bot_session_id="s-held"),
        THREAD: _thread(assignee_user_id="u-1"),
    }
    reads: List[tuple] = []
    ended: List[tuple] = []

    async def listing(channel, statuses, limit, after=None):
        assert channel == "whatsapp"
        assert statuses == [ChatSessionStatus.ACTIVE, ChatSessionStatus.IDLE]
        return open_sessions

    async def threads_by_ids(merchant_id, thread_ids):
        reads.append((merchant_id, *thread_ids))
        return {t: threads[t] for t in thread_ids if t in threads}

    async def buddy(merchant_id, channel):
        return SimpleNamespace(id=BINDING)

    async def end(session_id, reason):
        ended.append((session_id, reason))
        return object()

    monkeypatch.setattr(sessions, "list_open_sessions_on_channel", listing)
    monkeypatch.setattr(sessions, "threads_by_ids", threads_by_ids)
    monkeypatch.setattr(sessions, "buddy_binding", buddy)
    monkeypatch.setattr(sessions, "end_chat_session", end)
    _Lock.busy = {"chat:session:s-busy:lock"}  # a turn in flight: next pass
    assert await sessions.end_released_sessions() == 1
    assert ended == [("s-taken", "taken_over")]
    assert reads == [("shop", held_id, THREAD, THREAD)]  # one read for the page
    assert _Lock.held == []


async def test_a_thread_gets_a_whatsapp_session_once(monkeypatch) -> None:
    created: List[Dict[str, Any]] = []
    bound: List[tuple] = []

    async def get_session(session_id):
        return SimpleNamespace(
            id=session_id, status=ChatSessionStatus.ENDED, template_id=AGENT
        )

    async def template(template_id):
        return SimpleNamespace(reseller_id="r-1")

    async def create(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(id="s-new")

    async def bind(merchant_id, thread_id, session_id):
        bound.append((thread_id, session_id))

    monkeypatch.setattr(sessions, "get_chat_session_by_id", get_session)
    monkeypatch.setattr(sessions, "get_template_by_id_cached", template)
    monkeypatch.setattr(sessions, "create_chat_session", create)
    monkeypatch.setattr(sessions, "set_bot_session", bind)
    assert await sessions.session_for(_work()) == ("s-new", True)
    assert created == [
        dict(
            template_id=AGENT,
            reseller_id="r-1",
            merchant_id="shop",
            metadata={"conversation_id": THREAD, "template_vars": {}},
            channel="whatsapp",
        )
    ]
    assert bound == [(THREAD, "s-new")]

    async def live(session_id):
        return SimpleNamespace(
            id=session_id, status=ChatSessionStatus.ACTIVE, template_id=AGENT
        )

    monkeypatch.setattr(sessions, "get_chat_session_by_id", live)
    assert await sessions.session_for(_work()) == (SESSION, False)


# --- Buddy's answer -----------------------------------------------------------


class _World:
    """Every seam the answer calls, faked and recorded."""

    def __init__(self, monkeypatch, rows: List[TimelineRow]) -> None:
        self.rows = rows
        self.may_speak: List[bool] = []
        self.sent: List[Dict[str, Any]] = []
        self.recorded: List[str] = []
        self.cursor: List[datetime] = []
        self.turns: List[str] = []
        self.events: List[SSEEvent] = []
        self.closed = False
        self.session = (SESSION, False)
        self.earlier: List[TimelineRow] = []
        self.earlier_before: List[datetime] = []
        self.status = "accepted"
        #: the thread's cursor as another pod may have left it
        self.cursor_at: Optional[datetime] = None
        self.log: List[str] = []
        world = self

        async def pending(merchant_id, thread_id):
            return world.rows

        async def may_speak(merchant_id, thread_id, session_id=None):
            return world.may_speak.pop(0) if world.may_speak else True

        async def session_for(work):
            return world.session

        async def earlier(merchant_id, thread_id, since, before):
            world.earlier_before.append(before)
            return world.earlier

        async def thread_by_id(merchant_id, thread_id):
            return SimpleNamespace(bot_cursor_at=world.cursor_at)

        async def send(**kwargs):
            world.sent.append(kwargs)
            return SimpleNamespace(
                message_id=f"m-{len(world.sent)}",
                provider_message_id=None,
                status=world.status,
                reason=None,
                duplicate=False,
            )

        async def record(
            merchant_id, thread_id, message_id, provider_id, body, preview
        ):
            world.recorded.append(body["text"])

        async def cursor(merchant_id, thread_id, upto):
            world.cursor.append(upto)
            world.log.append("cursor")

        async def settings(merchant_id, channel, binding_id):
            return SimpleNamespace(
                non_text_message="I can only read text here.", claim_sla_minutes=10
            )

        async def run_turn(*, session_id, user_content):
            world.turns.append(user_content)
            world.log.append("turn")
            try:
                for event in world.events:
                    yield event
            finally:
                world.closed = True

        for name, fake in {
            "pending_inbound": pending,
            "bot_may_speak": may_speak,
            "session_for": session_for,
            "human_era_slice": earlier,
            "thread_by_id": thread_by_id,
            "send_session": send,
            "record_bot_reply": record,
            "mark_bot_cursor": cursor,
            "conversation_settings": settings,
            "run_chat_turn": run_turn,
        }.items():
            monkeypatch.setattr(inbox_answer, name, fake)


def _said(idx: int, content: str) -> SSEEvent:
    return SSEEvent(event="assistant_message", data={"idx": idx, "content": content})


async def test_every_assistant_message_goes_out_in_order(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi"), _row(2, text="order 42?")])
    long_reply = "\n\n".join([" ".join(["word"] * 300)] * 4)
    world.events = [
        SSEEvent(event="assistant_token", data={"delta": "Let"}),
        _said(3, "Let me **check**."),
        SSEEvent(event="function_call_completed", data={}),
        _said(5, long_reply),
        SSEEvent(event="turn_end", data={}),
    ]
    assert await inbox_answer.answer(_work()) == inbox_answer.DONE
    assert world.turns == ["hi\norder 42?"]
    keys = [s["dedupe_key"] for s in world.sent]
    assert keys[:3] == [
        f"buddy:{SESSION}:3:0",
        f"buddy:{SESSION}:5:0",
        f"buddy:{SESSION}:5:1",
    ]
    assert world.sent[0]["body"].text == "Let me *check*."
    assert all(len(s["body"].text) <= TEXT_MAX for s in world.sent)
    first = world.sent[0]
    assert (first["source_kind"], first["purpose_key"], first["binding_id"]) == (
        "agent",
        "service.conversation",
        BINDING,
    )
    assert world.recorded == [s["body"].text for s in world.sent]
    assert world.cursor == [_written(2)]
    assert _Lock.held == []


async def test_a_teammate_taking_over_mid_turn_stops_the_rest(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [_said(3, "one"), _said(5, "two"), _said(7, "three")]
    world.may_speak = [True, True, False]  # the first check, part one, part two
    assert await inbox_answer.answer(_work()) == inbox_answer.DONE
    assert [s["body"].text for s in world.sent] == ["one"]
    assert world.closed  # the turn was closed, not left running
    assert world.cursor == [_written(1)]


async def test_out_of_credits_is_silent(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [
        SSEEvent(event="user_committed", data={}),
        SSEEvent(event="error", data={"code": "insufficient_credits", "message": "x"}),
        SSEEvent(event="turn_end", data={"session_status": "FAILED"}),
    ]
    assert await inbox_answer.answer(_work()) == inbox_answer.DONE
    assert world.sent == [] and world.recorded == []
    assert world.cursor == [_written(1)]


async def test_only_unreadable_sends_the_non_text_message_once(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, "audio"), _row(2, "sticker")])
    assert await inbox_answer.answer(_work()) == inbox_answer.NON_TEXT
    assert world.turns == []
    assert [(s["body"].text, s["dedupe_key"]) for s in world.sent] == [
        ("I can only read text here.", f"buddy:non_text:{THREAD}:row-2")
    ]
    assert world.cursor == [_written(2)]


async def test_a_thread_no_longer_buddys_is_passed_over_and_kept(monkeypatch) -> None:
    """Taken, or a person is awaited: Buddy says nothing and leaves the cursor
    — what she wrote stays hers to answer if the handoff lapses."""
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.may_speak = [False]
    assert await inbox_answer.answer(_work()) == inbox_answer.NOT_BUDDYS
    assert world.turns == [] and world.sent == [] and world.cursor == []


async def test_a_busy_session_is_left_for_her_next_message(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    _Lock.busy = {f"chat:session:{SESSION}:lock"}
    assert await inbox_answer.answer(_work()) == inbox_answer.BUSY
    assert world.turns == [] and world.cursor == []


async def test_a_new_session_starts_with_the_earlier_conversation(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(3, text="and returns?")])
    world.session = ("s-new", True)
    world.earlier = [
        _row(1, text="where is it"),
        _row(2, text="ships today", row_kind="outbound"),
        _row(3, text="and returns?"),
    ]
    await inbox_answer.answer(_work())
    assert world.turns[0].startswith("[Earlier in this conversation")
    assert "Team: ships today" in world.turns[0]
    # the latest of the conversation before her burst, not the oldest
    assert world.earlier_before == [world.rows[0].occurred_at]


async def test_a_hand_back_tells_buddy_in_a_turn_of_its_own(monkeypatch) -> None:
    """Handed back with nothing of hers waiting: a new session hears the
    teammate's conversation, then why Buddy has the thread back. No cursor
    moves — there was nothing of hers to answer."""
    world = _World(monkeypatch, [])
    world.session = ("s-new", True)
    world.earlier = [
        _row(1, text="where is it"),
        _row(2, text="ships today", row_kind="outbound"),
    ]
    assert await inbox_answer.answer(_work(), RESUME_HANDED_BACK) == inbox_answer.DONE
    [turn] = world.turns
    assert turn.startswith("[Earlier in this conversation, before you joined]")
    assert turn.endswith(
        "[A teammate handled this conversation and handed it back to you."
        " What the customer asked before this has been dealt with.]"
    )
    assert world.cursor == []


async def test_a_claim_timeout_tells_buddy_with_what_she_wrote(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="still there?")])
    assert await inbox_answer.answer(_work(), RESUME_CLAIM_TIMEOUT) == inbox_answer.DONE
    assert world.turns == [
        "[Nobody took this conversation within 10 minutes of your request for a"
        " teammate. It is back with you.]\n\n"
        "[New message from the customer]\nstill there?"
    ]
    assert world.cursor == [_written(1)]


async def test_buddy_is_told_once_then_answers_only_her(monkeypatch) -> None:
    """The reason rides the request's first turn only; the look for work
    then wants her messages again."""
    looks: List[bool] = []
    told: List[Optional[str]] = []
    works = [_work(), _work(), None]

    async def bot_work(merchant_id, thread_id, unanswered_only=True):
        looks.append(unanswered_only)
        return works.pop(0)

    async def answer(work, reason=None):
        told.append(reason)
        return inbox_answer.DONE

    monkeypatch.setattr(inbox_answer, "bot_work", bot_work)
    monkeypatch.setattr(inbox_answer, "answer", answer)
    await inbox_answer.answer_thread("shop", THREAD, RESUME_HANDED_BACK)
    assert looks == [False, True, True]
    assert told == [RESUME_HANDED_BACK, None]


async def test_a_refused_send_stops_the_turn(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [_said(3, "one"), _said(5, "two")]
    world.status = "blocked"
    await inbox_answer.answer(_work())
    assert [s["body"].text for s in world.sent] == ["one"]
    assert world.recorded == ["one"]  # the Inbox still shows what was tried


# --- the engine on a WhatsApp session ---------------------------------------


def _agent(**over: Any) -> ChatAgent:
    template = TemplateModel.model_construct(
        id="tpl-1",
        name="t",
        flow={},
        configurations=ConfigurationModel.model_construct(
            render_ui=RenderUiConfig(enabled=True)
        ),
    )
    fields: Dict[str, Any] = dict(
        session_id=SESSION,
        template=template,
        llm=object(),
        template_vars={},
        catalog_version="v2",
        merchant_id="shop",
    )
    fields.update(over)
    agent = ChatAgent(**fields)
    agent._turn_id = "turn-1"
    return agent


def test_whatsapp_turns_render_no_ui() -> None:
    whatsapp = _agent(channel="whatsapp", conversation_id=THREAD)
    assert whatsapp._text_only and not whatsapp._render_ui_enabled
    web = _agent(channel="web")
    assert not web._text_only and web._render_ui_enabled


async def test_handoff_is_offered_only_when_the_binding_allows_it(monkeypatch) -> None:
    asked: List[tuple] = []
    answer: Dict[str, Any] = {"value": True}

    async def available(merchant_id, thread_id, session_id):
        asked.append((merchant_id, thread_id, session_id))
        if isinstance(answer["value"], Exception):
            raise answer["value"]
        return answer["value"]

    monkeypatch.setattr(agent_handoff, "handoff_available", available)
    agent = _agent(channel="whatsapp", conversation_id=THREAD)
    assert [t.name for t in await agent._handoff_tools()] == ["handoff_to_human"]
    answer["value"] = False
    assert await agent._handoff_tools() == []
    answer["value"] = RuntimeError("db down")
    assert await agent._handoff_tools() == []  # fail closed
    assert await _agent(channel="web")._handoff_tools() == []
    assert asked == [("shop", THREAD, SESSION)] * 3


async def test_the_handoff_tool_ends_the_turn_or_keeps_buddy_helping(
    monkeypatch,
) -> None:
    opened: List[tuple] = []

    async def request(merchant_id, thread_id, session_id, reason, summary, priority):
        opened.append((thread_id, session_id, reason, priority))
        return SimpleNamespace(id="h-1")

    monkeypatch.setattr(agent_handoff, "request_handoff", request)
    agent = _agent(channel="whatsapp", conversation_id=THREAD)
    result = await agent._handoff_handler(
        {
            "reason": "customer_requested",
            "summary": "wants a refund",
            "priority": "loud",
        }
    )
    assert result[TURN_END_REPLY_KEY] == agent_handoff.WAITING_MESSAGE
    # passed as the model wrote it: conversations owns the priority words
    assert opened == [(THREAD, SESSION, "customer_requested", "loud")]

    async def refuse(*args):
        raise RuntimeError("handoff is off")

    monkeypatch.setattr(agent_handoff, "request_handoff", refuse)
    refused = await agent._handoff_handler({"reason": "policy", "summary": "s"})
    assert refused["status"] == "error" and TURN_END_REPLY_KEY not in refused


async def test_a_turn_ending_tool_skips_the_next_llm_call(monkeypatch) -> None:
    call = FunctionCallFromLLM(
        function_name="handoff_to_human",
        tool_call_id="fc-1",
        arguments={"reason": "customer_requested", "summary": "s"},
        context=None,
    )
    cycles = [[("text", "Sure, one moment."), ("tool_call", call)]]
    calls = {"n": 0}

    async def stream(_llm, _context, **_kwargs):
        script = cycles[calls["n"]]  # a second cycle would IndexError
        calls["n"] += 1
        for event in script:
            yield event

    async def dispatch(self, _call, _node, _funcs, injected_args=None):
        return {"status": "ok", TURN_END_REPLY_KEY: "Connecting you."}, None

    async def insert(**kwargs):
        return None

    async def noop(**kwargs):
        return None

    monkeypatch.setattr(llm_driver, "stream", stream)
    monkeypatch.setattr(ChatAgent, "_dispatch_tool_call", dispatch)
    for module in (agent_core, agent_cycle, agent_context, agent_tooling):
        for name, fake in (
            ("insert_chat_message", insert),
            ("update_chat_session_after_turn", noop),
            ("upsert_agent_session_state_merge", noop),
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fake)

    agent = _agent(channel="whatsapp", conversation_id=THREAD)
    prep = _PreparedTools(
        flow_config={}, global_funcs=[], tool_retention=None, tool_projection=None
    )
    context = LLMContext(messages=[{"role": "user", "content": "a human please"}])
    events = [
        ev
        async for ev in agent._cycle_loop(
            context, {"name": "start", "functions": []}, prep
        )
    ]
    said = [ev.data["content"] for ev in events if ev.event == "assistant_message"]
    assert calls["n"] == 1
    assert said and said[-1].endswith("Connecting you.")
    assert "Sure, one moment." in "\n".join(said)


def test_the_text_only_style_names_no_channel_but_adds_its_markup() -> None:
    assert "WhatsApp" not in agent_context.TEXT_ONLY_STYLE
    assert agent_context.text_only_style("whatsapp").endswith(
        "Use *bold* sparingly and plain links."
    )
    assert agent_context.text_only_style("instagram") == agent_context.TEXT_ONLY_STYLE


def test_the_engine_takes_its_inbox_channels_from_the_registry() -> None:
    from app.ai.voice.agents.breeze_buddy.chat.agent import runtime
    from app.crm.connectivity.contracts import conversation_channels

    channels = frozenset(conversation_channels())
    assert runtime.CONVERSATION_CHANNELS == channels


# --- the idle sweeper: web sessions as before, inbox sessions once let go ----


async def test_the_idle_sweeper_idles_out_web_sessions_only(monkeypatch) -> None:
    """Nothing existing breaks: web sessions still end for inactivity, by
    the same query; thread-bound ones are ended by their own half."""
    seen: Dict[str, Any] = {}
    released: List[str] = []

    async def listing(**kwargs):
        seen.update(kwargs)
        return []

    async def timeout():
        return 600

    async def end_released():
        released.append("ran")
        return 0

    monkeypatch.setattr(cleanup, "list_idle_chat_sessions", listing)
    monkeypatch.setattr(cleanup, "CHAT_SESSION_END_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(cleanup, "end_released_sessions", end_released)
    await cleanup.end_idle_chat_sessions()
    assert seen["channels"] == ["web"] and released == ["ran"]


async def test_a_failing_inbox_half_never_stops_the_web_sweep(monkeypatch) -> None:
    seen: Dict[str, Any] = {}

    async def listing(**kwargs):
        seen.update(kwargs)
        return []

    async def timeout():
        return 600

    async def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(cleanup, "list_idle_chat_sessions", listing)
    monkeypatch.setattr(cleanup, "CHAT_SESSION_END_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(cleanup, "end_released_sessions", broken)
    await cleanup.end_idle_chat_sessions()
    assert seen["channels"] == ["web"]


# --- a turn is answered once ---------------------------------------------------


async def test_the_cursor_moves_as_the_turn_starts(monkeypatch) -> None:
    """The cursor is past her messages before the model is asked, under the
    session lock, so they are never answered twice."""
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [_said(3, "hello")]
    mark = inbox_answer.mark_bot_cursor  # the world's
    locked: List[bool] = []

    async def cursor(merchant_id, thread_id, upto):
        locked.append(bool(_Lock.held))
        await mark(merchant_id, thread_id, upto)

    monkeypatch.setattr(inbox_answer, "mark_bot_cursor", cursor)
    assert await inbox_answer.answer(_work()) == inbox_answer.DONE
    assert world.log == ["cursor", "turn"]
    assert world.cursor == [_written(1)] and locked == [True]
    assert _Lock.held == []


async def test_a_turn_that_fails_still_frees_the_thread(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])

    async def broken(*, session_id, user_content):
        world.log.append("turn")
        raise RuntimeError("model down")
        yield  # an async generator, like run_chat_turn

    monkeypatch.setattr(inbox_answer, "run_chat_turn", broken)
    with pytest.raises(RuntimeError):
        await inbox_answer.answer(_work())
    assert world.log == ["cursor", "turn"] and _Lock.held == []


async def test_a_burst_answered_meanwhile_is_left_alone(monkeypatch) -> None:
    """This read the burst while another turn was still answering it; that
    one finished and let go of the session just before this took it."""
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.cursor_at = _written(1)  # answered up to this row
    assert await inbox_answer.answer(_work()) == inbox_answer.NOTHING
    assert world.turns == [] and world.sent == [] and world.cursor == []
    assert _Lock.held == []


async def test_a_turn_ends_before_its_lock_can_expire(monkeypatch) -> None:
    import asyncio

    world = _World(monkeypatch, [_row(1, text="hi")])

    async def slow_turn(*, session_id, user_content):
        world.turns.append(user_content)
        yield _said(3, "thinking")
        await asyncio.sleep(10)
        yield _said(5, "never sent")

    monkeypatch.setattr(inbox_answer, "run_chat_turn", slow_turn)
    monkeypatch.setattr(inbox_answer, "TURN_LIMIT_SECONDS", 0.05)
    assert await inbox_answer.answer(_work()) == inbox_answer.DONE
    assert [s["body"].text for s in world.sent] == ["thinking"]
    assert world.cursor == [_written(1)] and _Lock.held == []


# --- the sweeper walks every open thread-bound session --------------------------


async def test_the_sweeper_pages_past_sessions_still_held(monkeypatch) -> None:
    """Hundreds of held sessions older than a released one: the sweep pages
    on until it reaches it."""
    held = [
        SimpleNamespace(
            id=f"s-{n}",
            merchant_id="shop",
            metadata={"conversation_id": THREAD},
            last_activity_at=NOW + timedelta(seconds=n),
        )
        for n in range(3)
    ]
    released = SimpleNamespace(
        id="s-released",
        merchant_id="shop",
        metadata={"conversation_id": "11111111-0000-4000-8000-000000000002"},
        last_activity_at=NOW + timedelta(seconds=9),
    )
    pages = [held[:2], [held[2], released]]
    asked: List[Any] = []

    async def listing(channel, statuses, limit, after=None):
        asked.append(after)
        return pages[len(asked) - 1] if len(asked) <= len(pages) else []

    async def threads_by_ids(merchant_id, thread_ids):
        return {}  # end_reason is decided below

    ended: List[str] = []

    async def end(session_id, reason):
        ended.append(session_id)
        return object()

    async def buddy(merchant_id, channel):
        return SimpleNamespace(id=BINDING)

    monkeypatch.setattr(sessions, "SWEEP_BATCH", 2)
    monkeypatch.setattr(sessions, "list_open_sessions_on_channel", listing)
    monkeypatch.setattr(sessions, "threads_by_ids", threads_by_ids)
    monkeypatch.setattr(sessions, "buddy_binding", buddy)
    monkeypatch.setattr(sessions, "end_chat_session", end)
    monkeypatch.setattr(
        sessions,
        "end_reason",
        lambda sid, thread, buddy_id, hours: (
            None
            if sid.startswith("s-") and sid != "s-released"
            else ChatEndedReason.TAKEN_OVER
        ),
    )
    await sessions.end_released_sessions()
    assert ended == ["s-released"]
    assert asked == [
        None,
        (held[1].last_activity_at, "s-1"),
        (released.last_activity_at, "s-released"),
    ]


# --- only a thread's own WhatsApp session may hand it off -----------------------


def test_a_web_sessions_metadata_never_names_a_thread() -> None:
    from app.ai.voice.agents.breeze_buddy.chat.turn_core import thread_of

    forged = SimpleNamespace(channel="web", metadata={"conversation_id": THREAD})
    assert thread_of(forged) is None
    mine = SimpleNamespace(channel="whatsapp", metadata={"conversation_id": THREAD})
    assert thread_of(mine) == THREAD
    assert thread_of(SimpleNamespace(channel="whatsapp", metadata=None)) is None


# --- no channel is named: each thread's own channel decides --------------------


async def test_a_reply_is_split_at_its_own_channels_ceiling(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [_said(3, "one two three four five")]
    monkeypatch.setattr(
        inbox_answer,
        "conversation_profile",
        lambda channel: SimpleNamespace(text_max=10, window_hours=24),
    )
    assert await inbox_answer.answer(_work()) == inbox_answer.DONE
    sent = [s["body"].text for s in world.sent]
    assert len(sent) > 1 and all(len(part) <= 10 for part in sent)
    assert " ".join(sent) == "one two three four five"


async def test_a_session_is_opened_on_its_threads_channel(monkeypatch) -> None:
    created: List[Dict[str, Any]] = []

    async def template(template_id):
        return SimpleNamespace(reseller_id="r-1")

    async def create(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(id="s-new")

    async def bind(merchant_id, thread_id, session_id):
        pass

    monkeypatch.setattr(sessions, "get_template_by_id_cached", template)
    monkeypatch.setattr(sessions, "create_chat_session", create)
    monkeypatch.setattr(sessions, "set_bot_session", bind)
    await sessions.session_for(_work(channel="instagram", session_id=None))
    assert created[0]["channel"] == "instagram"


async def test_the_sweeper_walks_every_conversation_channel(monkeypatch) -> None:
    asked: List[str] = []
    buddies: List[tuple] = []

    async def listing(channel, statuses, limit, after=None):
        asked.append(channel)
        return [
            SimpleNamespace(
                id=f"s-{channel}",
                merchant_id="shop",
                metadata={"conversation_id": THREAD},
                last_activity_at=NOW,
            )
        ]

    async def threads_by_ids(merchant_id, thread_ids):
        return {}

    async def buddy(merchant_id, channel):
        buddies.append((merchant_id, channel))
        return SimpleNamespace(id=BINDING)

    async def end(session_id, reason):
        return object()

    monkeypatch.setattr(
        sessions, "conversation_channels", lambda: ("whatsapp", "instagram")
    )
    monkeypatch.setattr(sessions, "list_open_sessions_on_channel", listing)
    monkeypatch.setattr(sessions, "threads_by_ids", threads_by_ids)
    monkeypatch.setattr(sessions, "buddy_binding", buddy)
    monkeypatch.setattr(sessions, "end_chat_session", end)
    assert await sessions.end_released_sessions() == 2
    assert asked == ["whatsapp", "instagram"]
    # Buddy's binding is read per channel, never one channel's for another's
    assert buddies == [("shop", "whatsapp"), ("shop", "instagram")]


# --- one answer per thread, and one more turn if she wrote meanwhile ---------


def _answering(monkeypatch, works: List[Any], outcomes: List[str]) -> List[str]:
    """Fake bot_work (one value per look) and answer (one outcome per turn);
    returns the log of turns run."""
    turns: List[str] = []

    async def bot_work(merchant_id, thread_id, unanswered_only=True):
        return works.pop(0) if works else None

    async def answer(work, reason=None):
        turns.append(work.thread_id)
        return outcomes.pop(0) if outcomes else inbox_answer.DONE

    monkeypatch.setattr(inbox_answer, "bot_work", bot_work)
    monkeypatch.setattr(inbox_answer, "answer", answer)
    return turns


async def test_nothing_owed_runs_no_turn(monkeypatch) -> None:
    turns = _answering(monkeypatch, [None], [])
    await inbox_answer.answer_thread("shop", THREAD)
    assert turns == [] and _Lock.held == []


async def test_a_thread_being_answered_is_left_to_that_answer(monkeypatch) -> None:
    """Two quick messages, two requests: the second finds the thread's lock
    taken and leaves; the first looks again when it lets go."""
    turns = _answering(monkeypatch, [_work()], [])
    _Lock.busy = {f"chat:inbox:thread:{THREAD}:lock"}
    await inbox_answer.answer_thread("shop", THREAD)
    assert turns == []


async def test_she_wrote_during_the_turn_so_one_more_turn(monkeypatch) -> None:
    turns = _answering(monkeypatch, [_work(), _work(), None], [])
    await inbox_answer.answer_thread("shop", THREAD)
    assert turns == [THREAD, THREAD] and _Lock.held == []


async def test_a_failed_turn_lets_go_of_the_thread(monkeypatch) -> None:
    async def bot_work(merchant_id, thread_id, unanswered_only=True):
        return _work()

    async def broken(work, reason=None):
        raise RuntimeError("model down")

    monkeypatch.setattr(inbox_answer, "bot_work", bot_work)
    monkeypatch.setattr(inbox_answer, "answer", broken)
    await inbox_answer.answer_thread("shop", THREAD)  # logged, never raised
    assert _Lock.held == []


async def test_one_request_runs_a_bounded_number_of_turns(monkeypatch) -> None:
    works = [_work() for _ in range(20)]
    turns = _answering(monkeypatch, works, [])
    await inbox_answer.answer_thread("shop", THREAD)
    assert len(turns) == inbox_answer.MAX_TURNS_PER_REQUEST


async def test_the_answer_runs_in_the_background(monkeypatch) -> None:
    import asyncio

    started = asyncio.Event()

    async def answer_thread(merchant_id, thread_id, reason=None):
        started.set()

    monkeypatch.setattr(inbox_answer, "answer_thread", answer_thread)
    inbox_answer.start_answer("shop", THREAD)  # returns before it runs
    assert not started.is_set()
    await asyncio.wait_for(started.wait(), timeout=1)


# --- the answer route: the platform's RBAC token, the merchant's own --------


def _route(monkeypatch) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routers.breeze_buddy import inbox as inbox_route

    asked: List[tuple] = []
    monkeypatch.setattr(
        inbox_route, "start_answer", lambda m, t, r=None: asked.append((m, t, r))
    )
    app = FastAPI()
    app.include_router(inbox_route.router)
    return TestClient(app), asked


def _token(merchant_id: str, role: str = "merchant") -> Dict[str, str]:
    from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
    from app.schemas import UserRole

    token = rbac_token_manager.create_access_token_with_rbac(
        user_id=f"merchant:{merchant_id}",
        username=f"merchant-{merchant_id}",
        role=UserRole(role),
        reseller_ids=[],
        merchant_ids=[merchant_id],
    )
    return {"Authorization": f"Bearer {token}"}


def test_the_route_starts_the_answer_for_the_merchants_own_thread(
    monkeypatch,
) -> None:
    client, asked = _route(monkeypatch)
    res = client.post(
        f"/inbox/threads/{THREAD}/answer",
        json={"merchant_id": "shop"},
        headers=_token("shop"),
    )
    assert res.status_code == 202 and asked == [("shop", THREAD, None)]


def test_the_route_passes_on_why_buddy_has_the_thread_back(monkeypatch) -> None:
    client, asked = _route(monkeypatch)
    url = f"/inbox/threads/{THREAD}/answer"
    body = {"merchant_id": "shop", "reason": RESUME_HANDED_BACK}
    assert client.post(url, json=body, headers=_token("shop")).status_code == 202
    assert asked == [("shop", THREAD, RESUME_HANDED_BACK)]
    odd = {"merchant_id": "shop", "reason": "because"}
    assert client.post(url, json=odd, headers=_token("shop")).status_code == 422
    assert len(asked) == 1


def test_the_route_refuses_anyone_but_the_merchant(monkeypatch) -> None:
    client, asked = _route(monkeypatch)
    url = f"/inbox/threads/{THREAD}/answer"
    no_token = client.post(url, json={"merchant_id": "shop"})
    other = client.post(url, json={"merchant_id": "shop"}, headers=_token("rival"))
    forged = client.post(
        url,
        json={"merchant_id": "shop"},
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert no_token.status_code in (401, 403) and other.status_code == 403
    assert forged.status_code in (401, 403)
    assert asked == []


def test_the_route_refuses_a_malformed_thread_id(monkeypatch) -> None:
    client, asked = _route(monkeypatch)
    res = client.post(
        "/inbox/threads/not-a-uuid/answer",
        json={"merchant_id": "shop"},
        headers=_token("shop"),
    )
    assert res.status_code == 422 and asked == []


def test_the_api_pods_mount_the_answer_route() -> None:
    """D41: Buddy's inbox answers ride the API pods' HTTP like the widget's
    messages — one route on the app."""
    import app.main as main

    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert "/agent/voice/breeze-buddy/inbox/threads/{thread_id}/answer" in paths


# --- only Buddy's inbox answer drives an inbox session ------------------------


@pytest.mark.parametrize(("channel", "refused"), [("whatsapp", True), ("web", False)])
async def test_the_chat_routes_refuse_an_inbox_session(
    monkeypatch, channel, refused
) -> None:
    """A web chat turn on an inbox session would land in her history unseen
    and bill a credit: 409, under the lock, which is let go. Web and widget
    sessions pass the check as before."""
    from fastapi import HTTPException

    from app.api.routers.breeze_buddy.chat import handlers

    async def session(session_id):
        return SimpleNamespace(
            id=session_id,
            channel=channel,
            status=ChatSessionStatus.ACTIVE,
            template_id=AGENT,
        )

    async def no_template(template_id):
        return None  # past the guard: the route stops here, with a 500

    monkeypatch.setattr(handlers, "RedisLock", _Lock)
    monkeypatch.setattr(handlers, "get_chat_session_by_id", session)
    monkeypatch.setattr(handlers, "get_template_by_id_cached", no_template)
    with pytest.raises(HTTPException) as raised:
        await handlers.send_chat_message_handler(SESSION, SimpleNamespace(context=None))
    assert raised.value.status_code == (409 if refused else 500)
    assert _Lock.held == []
