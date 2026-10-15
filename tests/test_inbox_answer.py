# pyrefly: ignore-errors
# Duck-typed fakes over the answer's seams and ChatAgent internals — the
# same harness shape as tests/test_internal_turn_persistence.py.
"""Buddy's chat on an inbox thread (inbox PR 3) — on every channel that
carries a conversation; WhatsApp is the one registered today.

A reply reads as the thread channel's text. A thread gets its own chat
session, and the idle sweeper ends the sessions their threads let go of,
with the honest reason. The engine, on a thread-bound
session: no UI, a handoff_to_human tool only when the binding allows it,
and that tool ends the turn with the waiting message.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

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
    sessions,
)
from app.ai.voice.agents.breeze_buddy.chat.inbox.format import render, split
from app.ai.voice.agents.breeze_buddy.chat.inbox.formats.whatsapp import to_whatsapp
from app.ai.voice.agents.breeze_buddy.chat.llm import driver as llm_driver
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    RenderUiConfig,
    TemplateModel,
)
from app.crm.connectivity.contracts import conversation_profile
from app.crm.conversations.contracts import (
    BotWork,
    Thread,
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
