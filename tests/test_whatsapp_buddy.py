# pyrefly: ignore-errors
# Duck-typed fakes over the responder's seams and ChatAgent internals — the
# same harness shape as tests/test_internal_turn_persistence.py.
"""Buddy answering on WhatsApp (inbox PR 3).

The responder turns a burst of her messages into one turn and sends every
assistant message in order as WhatsApp text, re-checking before each part;
out of credits it stays silent; it ends the sessions their threads let go
of with the honest reason. The engine, on a WhatsApp session: no UI, a
handoff_to_human tool only when the number allows it, and that tool ends
the turn with the waiting message.
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
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.whatsapp import (
    burst as burst_module,
    responder,
    sessions,
)
from app.ai.voice.agents.breeze_buddy.chat.whatsapp.format import (
    TEXT_MAX,
    split,
    to_whatsapp,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    RenderUiConfig,
    TemplateModel,
)
from app.crm.conversations.contracts import BotWork, Thread, TimelineRow
from app.schemas.breeze_buddy.chat import ChatEndedReason, ChatSessionStatus
from app.services.redis.locks import LockAcquireError

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
THREAD = "22222222-2222-4222-8222-222222222222"
AGENT = "7c1e0f4a-2b3c-4d5e-8f90-a1b2c3d4e5f6"
NUMBER = "11111111-1111-4111-8111-111111111111"
SESSION = "55555555-5555-4555-8555-555555555555"


def _row(n: int, kind: str = "text", **body: Any) -> TimelineRow:
    return TimelineRow(
        id=f"row-{n}",
        conversation_id=THREAD,
        kind=body.pop("row_kind", "inbound"),
        author_kind="customer",
        body={"type": kind, **body},
        occurred_at=NOW + timedelta(seconds=n),
    )


def _work(**over: Any) -> BotWork:
    fields: Dict[str, Any] = dict(
        thread_id=THREAD,
        merchant_id="shop",
        channel="whatsapp",
        customer_id="33333333-3333-4333-8333-333333333333",
        address="+919876543210",
        binding_id=NUMBER,
        agent_id=AGENT,
        session_id=SESSION,
        last_inbound_at=NOW,
        lease_until=NOW + timedelta(seconds=90),
    )
    fields.update(over)
    return BotWork(**fields)


def _thread(**over: Any) -> Thread:
    fields: Dict[str, Any] = dict(
        id=THREAD,
        merchant_id="shop",
        channel="whatsapp",
        contact_key="c",
        binding_id=NUMBER,
        last_inbound_at=NOW - timedelta(hours=1),
        last_message_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
    )
    fields.update(over)
    return Thread(**fields)


# --- WhatsApp text -----------------------------------------------------------


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
    parts = split(text)
    assert len(parts) > 1 and all(len(p) <= TEXT_MAX for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(text.split())
    assert split("x" * (TEXT_MAX + 10)) == ["x" * TEXT_MAX, "x" * 10]
    assert split("  ") == []


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
    assert burst.upto == NOW + timedelta(seconds=5) and burst.last_row_id == "row-5"


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
    text = burst_module.with_earlier("thanks, and returns?", earlier, {"row-3"})
    assert text.startswith("[Earlier in this conversation, before you joined]")
    assert "Customer: where is my order\nTeam: it ships today" in text
    assert text.endswith("[New message from the customer]\nthanks, and returns?")
    assert burst_module.with_earlier("hi", [_row(1, text="hi")], {"row-1"}) == "hi"


# --- how a session ended -----------------------------------------------------


@pytest.mark.parametrize(
    ("thread", "reason"),
    [
        (_thread(bot_session_id=SESSION), None),
        (_thread(binding_id="other", resolved_at=NOW), ChatEndedReason.NUMBER_CHANGED),
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
    assert sessions.end_reason(SESSION, thread, NUMBER, 24) == reason


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
    monkeypatch.setattr(responder, "RedisLock", _Lock)
    monkeypatch.setattr(sessions, "RedisLock", _Lock)


async def test_reconcile_ends_only_what_the_threads_let_go(monkeypatch) -> None:
    open_sessions = [
        SimpleNamespace(
            id="s-held", merchant_id="shop", metadata={"conversation_id": "t1"}
        ),
        SimpleNamespace(
            id="s-taken", merchant_id="shop", metadata={"conversation_id": "t2"}
        ),
        SimpleNamespace(
            id="s-busy", merchant_id="shop", metadata={"conversation_id": "t2"}
        ),
        SimpleNamespace(id="s-web", merchant_id=None, metadata={}),
    ]
    threads = {
        "t1": _thread(
            id="11111111-0000-4000-8000-000000000001", bot_session_id="s-held"
        ),
        "t2": _thread(assignee_user_id="u-1"),
    }
    ended: List[tuple] = []

    async def listing(channel, statuses, limit):
        assert channel == "whatsapp"
        assert statuses == [ChatSessionStatus.ACTIVE, ChatSessionStatus.IDLE]
        return open_sessions

    async def thread_by_id(merchant_id, thread_id):
        return threads[thread_id]

    async def buddy(merchant_id, channel):
        return SimpleNamespace(id=NUMBER)

    async def end(session_id, reason):
        ended.append((session_id, reason))
        return object()

    monkeypatch.setattr(sessions, "list_open_sessions_on_channel", listing)
    monkeypatch.setattr(sessions, "thread_by_id", thread_by_id)
    monkeypatch.setattr(sessions, "buddy_number", buddy)
    monkeypatch.setattr(sessions, "end_chat_session", end)
    _Lock.busy = {"chat:session:s-busy:lock"}  # a turn in flight: next pass
    assert await sessions.reconcile_sessions() == 1
    assert ended == [("s-taken", "taken_over")]
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


# --- the responder -----------------------------------------------------------


class _World:
    """Every seam the responder calls, faked and recorded."""

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
        self.status = "accepted"
        world = self

        async def pending(merchant_id, thread_id):
            return world.rows

        async def may_speak(merchant_id, thread_id, session_id=None):
            return world.may_speak.pop(0) if world.may_speak else True

        async def session_for(work):
            return world.session

        async def earlier(merchant_id, thread_id, since):
            return world.earlier

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

        async def settings(merchant_id, channel, binding_id):
            return SimpleNamespace(non_text_message="I can only read text here.")

        async def run_turn(*, session_id, user_content):
            world.turns.append(user_content)
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
            "send_session": send,
            "record_bot_reply": record,
            "mark_bot_cursor": cursor,
            "conversation_settings": settings,
            "run_chat_turn": run_turn,
        }.items():
            monkeypatch.setattr(responder, name, fake)


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
    assert await responder.answer(_work()) == responder.DONE
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
        NUMBER,
    )
    assert world.recorded == [s["body"].text for s in world.sent]
    assert world.cursor == [NOW + timedelta(seconds=2)]
    assert _Lock.held == []


async def test_a_teammate_taking_over_mid_turn_stops_the_rest(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [_said(3, "one"), _said(5, "two"), _said(7, "three")]
    world.may_speak = [True, True, False]  # the claim check, part one, part two
    assert await responder.answer(_work()) == responder.DONE
    assert [s["body"].text for s in world.sent] == ["one"]
    assert world.closed  # the turn was closed, not left running
    assert world.cursor == [NOW + timedelta(seconds=1)]


async def test_out_of_credits_is_silent(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [
        SSEEvent(event="user_committed", data={}),
        SSEEvent(event="error", data={"code": "insufficient_credits", "message": "x"}),
        SSEEvent(event="turn_end", data={"session_status": "FAILED"}),
    ]
    assert await responder.answer(_work()) == responder.DONE
    assert world.sent == [] and world.recorded == []
    assert world.cursor == [NOW + timedelta(seconds=1)]


async def test_only_unreadable_sends_the_non_text_message_once(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, "audio"), _row(2, "sticker")])
    assert await responder.answer(_work()) == responder.NON_TEXT
    assert world.turns == []
    assert [(s["body"].text, s["dedupe_key"]) for s in world.sent] == [
        ("I can only read text here.", f"buddy:non_text:{THREAD}:row-2")
    ]
    assert world.cursor == [NOW + timedelta(seconds=2)]


async def test_a_thread_no_longer_buddys_is_passed_over(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.may_speak = [False]
    assert await responder.answer(_work()) == responder.NOT_BUDDYS
    assert world.turns == [] and world.sent == []
    assert world.cursor == [NOW + timedelta(seconds=1)]


async def test_a_busy_session_is_left_for_the_next_lease(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    _Lock.busy = {f"chat:session:{SESSION}:lock"}
    assert await responder.answer(_work()) == responder.BUSY
    assert world.turns == [] and world.cursor == []


async def test_a_new_session_starts_with_the_earlier_conversation(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(3, text="and returns?")])
    world.session = ("s-new", True)
    world.earlier = [
        _row(1, text="where is it"),
        _row(2, text="ships today", row_kind="outbound"),
        _row(3, text="and returns?"),
    ]
    await responder.answer(_work())
    assert world.turns[0].startswith("[Earlier in this conversation")
    assert "Team: ships today" in world.turns[0]


async def test_a_refused_send_stops_the_turn(monkeypatch) -> None:
    world = _World(monkeypatch, [_row(1, text="hi")])
    world.events = [_said(3, "one"), _said(5, "two")]
    world.status = "blocked"
    await responder.answer(_work())
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


async def test_handoff_is_offered_only_when_the_number_allows_it(monkeypatch) -> None:
    asked: List[tuple] = []
    answer: Dict[str, Any] = {"value": True}

    async def available(merchant_id, thread_id):
        asked.append((merchant_id, thread_id))
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
    assert asked == [("shop", THREAD)] * 3


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
    assert opened == [(THREAD, SESSION, "customer_requested", "normal")]

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


def test_the_text_only_style_rides_after_the_template_instructions() -> None:
    assert "WhatsApp" in agent_context.TEXT_ONLY_STYLE


# --- the idle sweeper leaves WhatsApp alone ----------------------------------


async def test_the_idle_sweeper_ends_web_sessions_only(monkeypatch) -> None:
    seen: Dict[str, Any] = {}

    async def listing(**kwargs):
        seen.update(kwargs)
        return []

    async def timeout():
        return 600

    monkeypatch.setattr(cleanup, "list_idle_chat_sessions", listing)
    monkeypatch.setattr(cleanup, "CHAT_SESSION_END_TIMEOUT_SECONDS", timeout)
    await cleanup.end_idle_chat_sessions()
    assert seen["channels"] == ["web"]


# --- the pod -----------------------------------------------------------------


async def test_the_responder_loop_claims_whatsapp_and_answers(monkeypatch) -> None:
    import asyncio

    stop = asyncio.Event()
    claims: List[tuple] = []
    answered: List[str] = []

    async def claim(channels, batch, settle):
        claims.append((channels, batch, settle))
        return [_work()] if len(claims) == 1 else []

    async def answer(work):
        answered.append(work.thread_id)
        stop.set()
        return responder.DONE

    async def reconcile():
        return 0

    monkeypatch.setattr(responder, "claim_bot_work", claim)
    monkeypatch.setattr(responder, "answer", answer)
    monkeypatch.setattr(responder, "reconcile_sessions", reconcile)
    await asyncio.wait_for(responder.run_responder(stop), timeout=5)
    assert claims[0][0] == ["whatsapp"] and claims[0][2] == 2
    assert answered == [THREAD]


def test_main_starts_the_responder_on_its_role() -> None:
    import inspect

    import app.main as main

    source = inspect.getsource(main)
    assert "CRM_ROLE == RESPONDER_ROLE" in source
    assert main.start_responder is responder.start_responder
    assert main.stop_responder is responder.stop_responder
