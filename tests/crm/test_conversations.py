"""Conversations (inbox PR 2): the pure rules — who holds a thread, what a
customer's message does — the projector's "Buddy's binding only", the move
consumer, and the SQL's tenancy and vocabulary discipline."""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, cast

import pytest

from app.crm.conversations import moves, project, status as words
from app.crm.conversations.db.accessors import notify as notify_accessor
from app.crm.conversations.db.queries import (
    handoff as handoff_q,
    message as message_q,
    thread as thread_q,
)
from app.crm.conversations.route import plan_inbound
from app.crm.conversations.schemas import Handoff, Thread
from app.crm.conversations.state import held_by, lapsed
from app.crm.record.schemas import RawEvent

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
AGENT = "7c1e0f4a-2b3c-4d5e-8f90-a1b2c3d4e5f6"
BUDDY_BINDING = "11111111-1111-4111-8111-111111111111"


def _thread(**over: Any) -> Thread:
    fields: Dict[str, Any] = dict(
        id="22222222-2222-4222-8222-222222222222",
        merchant_id="shop",
        channel="whatsapp",
        contact_key="33333333-3333-4333-8333-333333333333",
        customer_id="33333333-3333-4333-8333-333333333333",
        address="+919876543210",
        binding_id=BUDDY_BINDING,
        last_inbound_at=NOW - timedelta(hours=1),
        last_message_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
    )
    fields.update(over)
    return Thread(**fields)


def _handoff(**over: Any) -> Handoff:
    fields: Dict[str, Any] = dict(
        id="44444444-4444-4444-8444-444444444444",
        merchant_id="shop",
        conversation_id="22222222-2222-4222-8222-222222222222",
        chat_session_id="55555555-5555-4555-8555-555555555555",
        opened_at=NOW - timedelta(minutes=2),
    )
    fields.update(over)
    return Handoff(**fields)


# --- who holds a thread: derived ------------------------------------------


@pytest.mark.parametrize(
    ("thread", "handoff", "held"),
    [
        (_thread(resolved_at=NOW), None, words.HELD_RESOLVED),
        (_thread(assignee_user_id="u-1"), _handoff(), words.HELD_BY_TEAMMATE),
        (_thread(bot_template_id=AGENT), _handoff(), words.HELD_WAITING),
        (_thread(bot_template_id=AGENT), None, words.HELD_BY_BUDDY),
        (_thread(), None, words.HELD_BY_NOBODY),
    ],
)
def test_who_holds_a_thread(thread, handoff, held) -> None:
    assert held_by(thread, handoff, 10, NOW) == held


def test_a_handoff_past_its_sla_hands_the_thread_back_before_the_sweep() -> None:
    """The fork's scar: a stopped sweep must never strand a customer."""
    stale = _handoff(opened_at=NOW - timedelta(minutes=11))
    assert lapsed(stale, 10, NOW)
    assert (
        held_by(_thread(bot_template_id=AGENT), stale, 10, NOW) == words.HELD_BY_BUDDY
    )
    claimed = _handoff(
        opened_at=NOW - timedelta(hours=1), claimed_at=NOW, claimed_by="u"
    )
    assert not lapsed(claimed, 10, NOW)


# --- what her message does (R2) ---------------------------------------------


def test_a_resolved_thread_reopens_fresh_with_the_agent() -> None:
    plan = plan_inbound(
        _thread(resolved_at=NOW, bot_session_id="66666666-6666-4666-8666-666666666666"),
        None,
        AGENT,
        10,
        NOW,
    )
    assert plan.reopen and plan.bot_template_id == AGENT and plan.bot_session_id is None


def test_nobody_holding_it_starts_buddy_only_when_an_agent_is_set() -> None:
    assert plan_inbound(_thread(), None, AGENT, 10, NOW).bot_template_id == AGENT
    unassigned = plan_inbound(_thread(), None, None, 10, NOW)
    assert unassigned.bot_template_id is None and not unassigned.reopen


def test_whoever_holds_it_keeps_it() -> None:
    teammate = _thread(assignee_user_id="u-1")
    assert plan_inbound(teammate, None, AGENT, 10, NOW).bot_template_id is None
    buddy = _thread(
        bot_template_id=AGENT, bot_session_id="66666666-6666-4666-8666-666666666666"
    )
    plan = plan_inbound(buddy, None, "another-agent", 10, NOW)
    assert (plan.bot_template_id, plan.bot_session_id) == (AGENT, buddy.bot_session_id)
    waiting = plan_inbound(_thread(bot_template_id=AGENT), _handoff(), AGENT, 10, NOW)
    assert waiting.bot_template_id == AGENT and not waiting.reopen


# --- the projector: Buddy's binding only (R1) -------------------------------


def _letter(topic: str, payload: Dict[str, Any], source: str = "whatsapp") -> RawEvent:
    return RawEvent(
        id="77777777-7777-4777-8777-777777777777",
        merchant_id="shop",
        source=source,
        topic=topic,
        schema_version="1",
        external_id="wamid.1",
        payload=payload,
        received_at=NOW,
    )


def _inbound(number: str) -> Dict[str, Any]:
    return {
        "metadata": {"phone_number_id": number},
        "messages": [
            {
                "from": "919876543210",
                "id": "wamid.1",
                "type": "text",
                "text": {"body": "hi"},
            }
        ],
    }


class _World:
    def __init__(self, buddy: Optional[SimpleNamespace]) -> None:
        self.buddy = buddy
        self.atoms: List[tuple] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def buddy_binding(merchant_id: str, channel: str):
            return self.buddy

        async def settings(
            merchant_id: str, channel: str, binding_id: Optional[str] = None
        ):
            return SimpleNamespace(default_agent_id=AGENT, claim_sla_minutes=10)

        async def atomically(fn, *args):
            self.atoms.append((fn.__name__, args))
            return None

        monkeypatch.setattr(project, "buddy_binding", buddy_binding)
        monkeypatch.setattr(project, "conversation_settings", settings)
        monkeypatch.setattr(project, "atomically", atomically)


async def test_a_message_to_buddys_binding_is_projected(monkeypatch) -> None:
    world = _World(
        SimpleNamespace(id=BUDDY_BINDING, address="PN_BUDDY", is_primary=False)
    )
    world.install(monkeypatch)
    await project.consume_conversation_event(
        _letter("message.inbound", _inbound("PN_BUDDY")),
        "33333333-3333-4333-8333-333333333333",
    )
    [(name, args)] = world.atoms
    assert name == "_project_inbound_in_txn"
    assert args[3] == "+919876543210" and args[4] == BUDDY_BINDING  # address, binding
    assert args[7] == {"type": "text", "text": "hi", "caption": None}


async def test_a_message_to_any_other_binding_does_nothing(monkeypatch) -> None:
    world = _World(
        SimpleNamespace(id=BUDDY_BINDING, address="PN_BUDDY", is_primary=False)
    )
    world.install(monkeypatch)
    letter = _letter("message.inbound", _inbound("PN_PRIMARY"))
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    _World(None).install(monkeypatch)  # Buddy on no binding at all
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    assert world.atoms == []


async def test_letters_about_nobody_or_another_channel_are_ignored(monkeypatch) -> None:
    world = _World(
        SimpleNamespace(id=BUDDY_BINDING, address="PN_BUDDY", is_primary=False)
    )
    world.install(monkeypatch)
    await project.consume_conversation_event(
        _letter("message.inbound", _inbound("PN_BUDDY")), None
    )
    await project.consume_conversation_event(
        _letter("message.inbound", _inbound("PN_BUDDY"), source="shopify"), "c"
    )
    assert world.atoms == []


async def test_a_template_joins_a_thread_only_on_a_shared_binding(monkeypatch) -> None:
    """Templates go out from the primary; only when that is Buddy's binding
    too does one belong on Buddy's thread."""
    seen: List[str] = []

    async def thread_for_contact(*args):
        seen.append("looked")
        return None

    monkeypatch.setattr(
        project.thread_accessor, "thread_for_contact", thread_for_contact
    )
    letter = _letter(
        "message.queued", {"kind": "template", "template_id": "cod", "message_id": "m"}
    )
    _World(SimpleNamespace(id=BUDDY_BINDING, address="PN", is_primary=False)).install(
        monkeypatch
    )
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    assert seen == []
    _World(SimpleNamespace(id=BUDDY_BINDING, address="PN", is_primary=True)).install(
        monkeypatch
    )
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    assert seen == ["looked"]


@pytest.mark.parametrize("source_kind", ["human", "agent"])
async def test_a_template_its_sender_records_is_never_projected(
    monkeypatch, source_kind
) -> None:
    """A teammate's template (reply.py) and Buddy's are put on the timeline by
    their writer at once; projecting the letter too races that insert for the
    same message and fails the teammate's reply."""

    async def thread_for_contact(*args):
        raise AssertionError("its writer records it")

    monkeypatch.setattr(
        project.thread_accessor, "thread_for_contact", thread_for_contact
    )
    _World(SimpleNamespace(id=BUDDY_BINDING, address="PN", is_primary=True)).install(
        monkeypatch
    )
    letter = _letter(
        "message.queued",
        {
            "kind": "template",
            "template_id": "cod",
            "message_id": "m",
            "source_kind": source_kind,
        },
    )
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )


async def test_a_templates_row_list_row_and_wake_up_share_one_atom(
    monkeypatch,
) -> None:
    """A raise after the timeline row is written must undo the row too: a
    retried letter would otherwise find it, stop short, and never move the
    list row or wake the Inbox."""
    world = _World(SimpleNamespace(id=BUDDY_BINDING, address="PN", is_primary=True))
    world.install(monkeypatch)

    async def thread_for_contact(*args: Any):
        return _thread()

    monkeypatch.setattr(
        project.thread_accessor, "thread_for_contact", thread_for_contact
    )
    letter = _letter(
        "message.queued", {"kind": "template", "template_id": "cod", "message_id": "m"}
    )
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    [(name, args)] = world.atoms
    assert name == "_project_template_in_txn"

    seen: List[tuple] = []
    txn: Any = object()

    async def insert_outbound(handle: Any, *rest: Any):
        seen.append(("insert", handle))
        return SimpleNamespace()

    async def touch_outbound(*rest: Any):
        seen.append(("touch", rest[-1]))

    async def wake(*rest: Any):
        seen.append(("wake", rest[-1]))

    monkeypatch.setattr(project.message_accessor, "insert_outbound", insert_outbound)
    monkeypatch.setattr(project.thread_accessor, "touch_outbound", touch_outbound)
    monkeypatch.setattr(project, "wake", wake)
    await project._project_template_in_txn(txn, *args)
    assert seen == [("insert", txn), ("touch", txn), ("wake", txn)]

    async def already_there(handle: Any, *rest: Any):
        return None

    seen.clear()
    monkeypatch.setattr(project.message_accessor, "insert_outbound", already_there)
    await project._project_template_in_txn(txn, *args)
    assert seen == []


# --- Buddy moved (R7) -------------------------------------------------------


async def test_buddy_moving_resolves_the_old_bindings_threads(monkeypatch) -> None:
    resolved: List[tuple] = []

    async def resolve_binding(merchant_id: str, binding_id: str):
        resolved.append((merchant_id, binding_id))
        return ["t-1"]

    async def buddy_binding(merchant_id: str, channel: str):
        return SimpleNamespace(id="new")

    monkeypatch.setattr(moves, "resolve_binding", resolve_binding)
    monkeypatch.setattr(moves, "buddy_binding", buddy_binding)
    payload = {"channel": "whatsapp", "from_binding_id": "old", "to_binding_id": "new"}
    await moves.consume_buddy_moved(_letter("binding.buddy_moved", payload), None)
    await moves.consume_buddy_moved(_letter("message.inbound", payload), None)
    assert resolved == [("shop", "old")]


async def test_a_late_moved_letter_leaves_buddys_current_binding_alone(
    monkeypatch,
) -> None:
    """Moved A→B, then back to A before the A→B letter was read: A's threads
    are Buddy's again, so the late letter resolves nothing."""

    async def resolve_binding(merchant_id: str, binding_id: str):
        raise AssertionError("Buddy is back on this binding")

    async def buddy_binding(merchant_id: str, channel: str):
        return SimpleNamespace(id="old")

    monkeypatch.setattr(moves, "resolve_binding", resolve_binding)
    monkeypatch.setattr(moves, "buddy_binding", buddy_binding)
    payload = {"channel": "whatsapp", "from_binding_id": "old", "to_binding_id": "new"}
    await moves.consume_buddy_moved(_letter("binding.buddy_moved", payload), None)


async def test_her_message_beating_the_moved_letter_reopens_her_thread_fresh(
    monkeypatch,
) -> None:
    """Buddy moved A→B and her message to B is read before the move's letter.
    Her thread, still open on A with its holder, is resolved first and its
    handoff closed binding_changed — exactly what the letter would do — so
    the message reopens it fresh on B instead of carrying A's session or
    teammate over, and the late letter then finds nothing on A."""
    thread_id = "22222222-2222-4222-8222-222222222222"
    contact_key = "33333333-3333-4333-8333-333333333333"
    calls: List[tuple] = []
    recorded: Dict[str, Any] = {}

    async def resolve_off_binding(txn: Any, merchant_id, channel, key, binding_id):
        calls.append(("resolve", key, binding_id))
        return [thread_id]

    async def close_for_threads(txn: Any, merchant_id, thread_ids, outcome):
        calls.append(("close", thread_ids, outcome))
        return 1

    async def ensure_thread(txn: Any, *args: Any):
        calls.append(("ensure",))
        return _thread(resolved_at=NOW)  # what the resolve above left

    async def insert_inbound(txn: Any, *args: Any):
        return SimpleNamespace(conversation_id=thread_id, created_at=NOW)

    async def open_for_thread(*args: Any):
        return None  # closed above

    async def record_inbound(txn: Any, merchant_id, tid, at, preview, reopen, *bot):
        recorded.update(reopen=reopen, bot=bot[:2])

    async def wake(*args: Any):
        return None

    monkeypatch.setattr(
        project.thread_accessor, "resolve_off_binding", resolve_off_binding
    )
    monkeypatch.setattr(
        project.handoff_accessor, "close_for_threads", close_for_threads
    )
    monkeypatch.setattr(project.thread_accessor, "ensure_thread", ensure_thread)
    monkeypatch.setattr(project.message_accessor, "insert_inbound", insert_inbound)
    monkeypatch.setattr(project.handoff_accessor, "open_for_thread", open_for_thread)
    monkeypatch.setattr(project.thread_accessor, "record_inbound", record_inbound)
    monkeypatch.setattr(project, "wake", wake)
    txn: Any = object()  # the fakes above never touch it
    await project._project_inbound_in_txn(
        txn,
        "shop",
        "whatsapp",
        contact_key,
        "+919876543210",
        BUDDY_BINDING,
        "77777777-7777-4777-8777-777777777777",
        "wamid.1",
        {"type": "text", "text": "hi", "caption": None},
        "hi",
        NOW,
        AGENT,
        10,
    )
    assert calls == [
        ("resolve", contact_key, BUDDY_BINDING),
        ("close", [thread_id], words.OUTCOME_BINDING_CHANGED),
        ("ensure",),
    ]
    assert recorded == {"reopen": True, "bot": (AGENT, None)}
    # Only her thread, only while open, only on a binding that is not Buddy's.
    query, values = thread_q.resolve_off_binding_query(
        "shop", "whatsapp", contact_key, BUDDY_BINDING
    )
    assert "contact_key = $3" in query and "binding_id <> $4::uuid" in query
    assert "resolved_at IS NULL" in query and values[3] == BUDDY_BINDING


# --- the SQL: tenancy first, vocabulary bound ------------------------------

QUERY_MODULES = (thread_q, message_q, handoff_q)
#: The builders that deliberately cross tenants: the drains.
CROSS_TENANT: Set[str] = set()


def _builders():
    for module in QUERY_MODULES:
        for name, fn in vars(module).items():
            if name.endswith("_query") and callable(fn):
                yield module, name, fn


def test_every_tenant_read_and_write_is_scoped_to_the_merchant() -> None:
    for module, name, fn in _builders():
        if name in CROSS_TENANT:
            continue
        source = (
            ast.get_source_segment(
                Path(module.__file__).read_text(), _def(module, name)
            )
            or ""
        )
        # A read or an update filters on the merchant; an insert writes it as
        # the first value (and its ON CONFLICT target starts with it).
        scoped = "merchant_id = $1" in source or (
            "INSERT INTO" in source
            and "(merchant_id," in source
            and "VALUES ($1," in source
        )
        assert source and scoped, f"{name} is not merchant-scoped"


def _def(module, name):
    tree = ast.parse(Path(module.__file__).read_text())
    return next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
    )


def test_no_builder_spells_a_vocabulary_word() -> None:
    """status.py is the one home; SQL binds the words as $n."""
    vocabulary = {
        v
        for k, v in vars(words).items()
        if k.isupper()
        and isinstance(v, str)
        and k.split("_")[0] in ("KIND", "AUTHOR", "OUTCOME")
    }
    for module, name, _fn in _builders():
        node = _def(module, name)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "query" for t in sub.targets
            ):
                sql = ast.unparse(sub.value)
                for word in vocabulary:
                    assert f"'{word}'" not in sql, f"{name} spells '{word}'"


def test_the_replay_guards_match_the_partial_unique_indexes() -> None:
    ddl = (
        Path(__file__).parents[2]
        / "app/database/migrations/086_create_inbox_schema.sql"
    ).read_text()
    assert (
        "ON crm_conversation_message (merchant_id, event_raw_id)\n    WHERE event_raw_id IS NOT NULL"
        in ddl
    )
    assert "ON CONFLICT (merchant_id, event_raw_id) WHERE event_raw_id IS NOT NULL" in (
        message_q.insert_inbound_query(
            "m", "t", "inbound", "customer", None, None, None, NOW
        )[0]
    )
    assert "ON CONFLICT (merchant_id, message_id) WHERE message_id IS NOT NULL" in (
        message_q.insert_outbound_query(
            "m", "t", "outbound", "assist", None, None, None, None, NOW
        )[0]
    )


# --- a reopened thread starts fresh ------------------------------------------


def test_a_reopened_thread_moves_buddys_cursor_to_just_before_her_message() -> None:
    """Resolved, then she writes again: Buddy answers only the new message,
    never what a teammate already handled (nor her oldest 50)."""
    written = NOW + timedelta(seconds=3)
    query, values = thread_q.record_inbound_query(
        "shop", "t", NOW, "hi", True, AGENT, None, written
    )
    assert (
        "bot_cursor_at = CASE WHEN $5 THEN $8::timestamptz - interval '1 microsecond'"
        in query
    )
    assert values[4] is True and values[7] == written


# --- the live stream notices a lost connection --------------------------------


class _Link(notify_accessor.Listener):
    """The real handle, with a probe that answers or doesn't."""

    def __init__(self, probe_fails: bool = False) -> None:
        super().__init__(conn=None)
        self.probes = 0
        self._fails = probe_fails

    async def probe(self, timeout: float) -> None:
        self.probes += 1
        if self._fails:
            self.lost.set()
            raise TimeoutError("no answer")


async def test_the_hub_gives_up_a_lost_listener(monkeypatch) -> None:
    from app.crm.conversations import realtime

    monkeypatch.setattr(realtime, "TICK_SECONDS", 0.01)
    hub = realtime._Hub()
    hub._streams["shop"] = {asyncio.Queue()}  # someone is watching
    link = _Link()
    link.lost.set()  # asyncpg dropped the connection
    with pytest.raises(realtime.ListenerLost):
        await hub._hold(link)


async def test_the_hub_probes_and_gives_up_a_half_open_listener(monkeypatch) -> None:
    from app.crm.conversations import realtime

    monkeypatch.setattr(realtime, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(realtime, "PROBE_SECONDS", 0.03)
    hub = realtime._Hub()
    hub._streams["shop"] = {asyncio.Queue()}
    link = _Link(probe_fails=True)
    with pytest.raises(TimeoutError):
        await hub._hold(link)
    assert link.probes == 1


async def test_a_lost_listener_is_terminated_not_unlistened(monkeypatch) -> None:
    """A dead connection must not hang the UNLISTEN or the pool's reset —
    nor a half-open one no probe has caught yet, so a healthy-looking one is
    closed outright too."""
    from contextlib import asynccontextmanager

    from app.crm.conversations.db.accessors import notify

    class Conn:
        def __init__(self) -> None:
            self.calls: List[str] = []

        def add_termination_listener(self, fn) -> None:
            self.calls.append("watch")

        async def add_listener(self, channel, fn) -> None:
            self.calls.append("listen")

        async def remove_listener(self, channel, fn) -> None:
            self.calls.append("unlisten")

        def remove_termination_listener(self, fn) -> None:
            self.calls.append("unwatch")

        def terminate(self) -> None:
            self.calls.append("terminate")

    conn = Conn()

    @asynccontextmanager
    async def crm_connection():
        yield conn

    monkeypatch.setattr(notify, "crm_connection", crm_connection)
    async with notify.listening(lambda payload: None) as link:
        link.lost.set()
    assert conn.calls == ["watch", "listen", "terminate"]
    conn.calls.clear()
    async with notify.listening(lambda payload: None):
        pass  # looks healthy: closed all the same, never handed back
    assert conn.calls == ["watch", "listen", "terminate"]


async def test_listening_again_tells_every_open_stream_to_resync(monkeypatch) -> None:
    """What committed while the hub wasn't listening is never delivered;
    each open console is told to re-read its view instead."""
    from contextlib import asynccontextmanager

    from app.crm.conversations import realtime

    @asynccontextmanager
    async def listening(on_payload):
        yield _Link()

    monkeypatch.setattr(notify_accessor, "listening", listening)
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
    queue.put_nowait({"m": "shop", "t": "th-1", "k": "message"})  # before the drop
    seen: List[Dict[str, Any]] = []

    class Hub(realtime._Hub):
        async def _hold(self, link: notify_accessor.Listener) -> None:
            while not queue.empty():
                seen.append(queue.get_nowait())
            self._streams.clear()  # the console went away

    hub = Hub()
    hub._streams["shop"] = {queue}
    await hub._listen_forever()
    assert seen == [realtime.RESYNC]


async def test_a_stream_too_slow_to_keep_up_gets_one_resync() -> None:
    from app.crm.conversations import realtime

    hub = realtime._Hub()
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=2)
    hub._streams["shop"] = {queue}
    for n in range(3):
        hub._deliver({"m": "shop", "t": f"th-{n}", "k": "message"})
    assert queue.qsize() == 1 and queue.get_nowait() is realtime.RESYNC


async def test_a_wake_about_no_one_thread_goes_out_as_ready(monkeypatch) -> None:
    """Buddy moving binding resolves many threads at once: the console
    re-reads its view, as it does after a resync."""
    from app.crm.conversations import realtime

    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()

    class Hub(realtime._Hub):
        def subscribe(self, merchant_id: str) -> "asyncio.Queue[Dict[str, Any]]":
            return queue

    monkeypatch.setattr(realtime, "_hub", Hub())
    queue.put_nowait({"m": "shop", "t": None, "k": "state"})
    queue.put_nowait(realtime.RESYNC)
    queue.put_nowait({"m": "shop", "t": "th-1", "k": "message"})
    frames = cast(AsyncGenerator[str, None], realtime.stream("shop"))
    got = [await anext(frames) for _ in range(4)]
    await frames.aclose()
    ready = realtime._frame("ready", {})
    assert got[:3] == [ready, ready, ready]
    assert got[3] == realtime._frame("thread", {"id": "th-1", "kind": "message"})


# --- no channel is named: the registry says which carry a conversation -------


async def test_the_projector_follows_the_registrys_conversation_channels(
    monkeypatch,
) -> None:
    """A channel joins the inbox by declaring a conversation in
    connectivity's registry — the projector never names one."""
    world = _World(
        SimpleNamespace(id=BUDDY_BINDING, address="PN_BUDDY", is_primary=False)
    )
    world.install(monkeypatch)
    letter = _letter("message.inbound", _inbound("PN_BUDDY"))
    customer = "33333333-3333-4333-8333-333333333333"
    monkeypatch.setattr(project, "conversation_channels", lambda: ())
    await project.consume_conversation_event(letter, customer)
    assert world.atoms == []
    monkeypatch.setattr(project, "conversation_channels", lambda: ("whatsapp",))
    await project.consume_conversation_event(letter, customer)
    assert [name for name, _ in world.atoms] == ["_project_inbound_in_txn"]


# --- receipts wake the thread showing the send --------------------------------


def _receipt_letter(topic: str) -> RawEvent:
    """A filed receipt: it names a message, never a customer."""
    return RawEvent(
        id="77777777-7777-4777-8777-777777777777",
        merchant_id="shop",
        source="whatsapp",
        topic=topic,
        schema_version="v23.0",
        external_id="wamid.X:read",
        payload={},
        received_at=NOW,
    )


async def test_a_receipt_wakes_the_thread_showing_the_send(monkeypatch) -> None:
    from app.crm.conversations import project

    woken: List[tuple] = []
    asked: List[tuple] = []

    async def target(event):
        return "wamid.X", "66666666-6666-4666-8666-666666666666"

    async def thread_for_send(merchant_id, provider_message_id, message_id):
        asked.append((merchant_id, provider_message_id, message_id))
        return "22222222-2222-4222-8222-222222222222"

    async def wake(merchant_id, thread_id, kind):
        woken.append((thread_id, kind))

    monkeypatch.setattr(project, "receipt_target", target)
    monkeypatch.setattr(project.message_accessor, "thread_for_send", thread_for_send)
    monkeypatch.setattr(project, "wake", wake)
    receipt = _receipt_letter(project.TOPIC_STATUS)
    # receipts name no customer — they must not be turned away for it
    await project.consume_conversation_event(receipt, None)
    assert asked == [("shop", "wamid.X", "66666666-6666-4666-8666-666666666666")]
    assert woken == [("22222222-2222-4222-8222-222222222222", "receipt")]


async def test_a_receipt_for_a_send_no_thread_shows_wakes_nothing(monkeypatch) -> None:
    from app.crm.conversations import project

    async def none(*args):
        return None

    async def wake(*args):
        raise AssertionError("nothing to wake")

    monkeypatch.setattr(project, "receipt_target", none)
    monkeypatch.setattr(project, "wake", wake)
    receipt = _receipt_letter(project.TOPIC_STATUS)
    await project.consume_conversation_event(receipt, None)


def test_the_send_lookup_reads_either_id() -> None:
    query, values = message_q.thread_for_send_query("shop", "wamid.X", None)
    assert "provider_message_id = $2" in query and "message_id = $3::uuid" in query
    assert values == ["shop", "wamid.X", None]


# --- the plan's once-only guarantees ------------------------------------------


async def test_a_replayed_letter_changes_nothing(monkeypatch) -> None:
    """The timeline's partial unique finds the message already there: no
    window moves, nobody is woken."""
    calls: List[str] = []

    async def resolve_off_binding(txn: Any, *args: Any):
        return []  # her thread is on Buddy's binding

    async def ensure_thread(txn: Any, *args: Any):
        return _thread(bot_template_id=AGENT)

    async def insert_inbound(txn: Any, *args: Any):
        return None  # already on the timeline

    async def record_inbound(*args: Any):
        calls.append("record")

    async def wake(*args: Any):
        calls.append("wake")

    monkeypatch.setattr(
        project.thread_accessor, "resolve_off_binding", resolve_off_binding
    )
    monkeypatch.setattr(project.thread_accessor, "ensure_thread", ensure_thread)
    monkeypatch.setattr(project.message_accessor, "insert_inbound", insert_inbound)
    monkeypatch.setattr(project.thread_accessor, "record_inbound", record_inbound)
    monkeypatch.setattr(project, "wake", wake)
    txn: Any = object()  # the fakes above never touch it
    result = await project._project_inbound_in_txn(
        txn,
        "shop",
        "whatsapp",
        "33333333-3333-4333-8333-333333333333",
        "+919876543210",
        BUDDY_BINDING,
        "77777777-7777-4777-8777-777777777777",
        "wamid.1",
        {"type": "text", "text": "hi", "caption": None},
        "hi",
        NOW,
        AGENT,
        10,
    )
    assert result is None and calls == []
