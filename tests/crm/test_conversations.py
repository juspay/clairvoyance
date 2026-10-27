"""Conversations (inbox PR 2): the pure rules — the window, who holds a
thread, what a customer's message does, what the closing sweep does — the
projector's "Buddy's binding only", the move consumer, who may act, and the
SQL's tenancy and vocabulary discipline."""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, List, Optional, cast

import pytest

from app.crm.conversations import moves, project, status as words, sweeps
from app.crm.conversations.access import actor_of
from app.crm.conversations.db.accessors import notify as notify_accessor
from app.crm.conversations.db.queries import (
    handoff as handoff_q,
    message as message_q,
    thread as thread_q,
)
from app.crm.conversations.route import plan_inbound
from app.crm.conversations.schemas import Handoff, Thread
from app.crm.conversations.state import held_by, lapsed
from app.crm.conversations.window import closes_at, closing_due, is_open
from app.crm.record.schemas import RawEvent
from app.schemas import UserInfo, UserRole

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


# --- the window: a predicate, never stored ----------------------------------


def test_the_window_is_her_last_message_plus_the_channel_hours() -> None:
    last = NOW - timedelta(hours=23)
    assert closes_at(last, 24) == NOW + timedelta(hours=1)
    assert is_open(last, 24, NOW)
    assert not is_open(NOW - timedelta(hours=24), 24, NOW)
    assert not is_open(None, 24, NOW) and closes_at(None, 24) is None


def test_the_closing_message_is_due_inside_the_lead_only() -> None:
    last = NOW - timedelta(hours=23, minutes=50)  # shuts in 10 min
    assert closing_due(last, 24, 15, NOW)
    assert not closing_due(last, 24, 5, NOW)
    assert not closing_due(NOW - timedelta(hours=25), 24, 15, NOW)  # already shut


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


# --- the closing sweep (R5, D9, R7) -----------------------------------------


def _closing(thread: Thread, held: str, quiet: Optional[timedelta] = None) -> str:
    last = NOW - quiet if quiet is not None else None
    return sweeps.closing_action(thread, BUDDY_BINDING, held, 15, 24, last, NOW)


def test_a_thread_off_buddys_binding_resolves_quietly() -> None:
    assert (
        _closing(_thread(binding_id="another"), words.HELD_BY_BUDDY)
        == sweeps.ACTION_MOVED
    )
    assert (
        sweeps.closing_action(_thread(), None, words.HELD_BY_BUDDY, 15, 24, None, NOW)
        == sweeps.ACTION_MOVED
    )


def test_buddy_and_a_waiting_thread_say_goodbye_inside_the_lead() -> None:
    due = _thread(last_inbound_at=NOW - timedelta(hours=23, minutes=50))
    assert _closing(due, words.HELD_BY_BUDDY) == sweeps.ACTION_CLOSE
    assert _closing(due, words.HELD_WAITING) == sweeps.ACTION_CLOSE
    assert _closing(_thread(), words.HELD_BY_BUDDY) == sweeps.ACTION_WAIT


def test_a_teammate_gets_the_goodbye_sent_only_after_an_hour_quiet() -> None:
    due = _thread(last_inbound_at=NOW - timedelta(hours=23, minutes=50))
    assert (
        _closing(due, words.HELD_BY_TEAMMATE, timedelta(minutes=20))
        == sweeps.ACTION_WAIT
    )
    assert (
        _closing(due, words.HELD_BY_TEAMMATE, timedelta(hours=2)) == sweeps.ACTION_CLOSE
    )


def test_nobody_answering_gets_no_goodbye_and_a_shut_window_expires() -> None:
    due = _thread(last_inbound_at=NOW - timedelta(hours=23, minutes=50))
    assert _closing(due, words.HELD_BY_NOBODY) == sweeps.ACTION_WAIT
    shut = _thread(last_inbound_at=NOW - timedelta(hours=25))
    assert _closing(shut, words.HELD_BY_BUDDY) == sweeps.ACTION_EXPIRE


# --- who may act (D19) ------------------------------------------------------


def _user(id_: str, role: UserRole) -> UserInfo:
    return UserInfo(id=id_, username="u", role=role, merchant_ids=["shop"])


def test_a_login_link_session_reads_but_never_acts() -> None:
    launch = actor_of(_user("merchant:shop", UserRole.MERCHANT))
    assert launch.read_only and not launch.manager


def test_merchant_admins_manage_and_users_act() -> None:
    assert actor_of(_user("u-1", UserRole.MERCHANT)).manager
    user = actor_of(_user("u-2", UserRole.USER))
    assert not user.manager and not user.read_only


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
    nowhere = _World(None)  # Buddy on no binding at all
    nowhere.install(monkeypatch)
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    assert world.atoms == [] and nowhere.atoms == []


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
CROSS_TENANT = {
    "closing_candidates_query",
    "delete_resolved_query",
    "unclaimed_older_than_query",
}


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
    assert "ON CONFLICT (merchant_id, conversation_id) WHERE closed_at IS NULL" in (
        handoff_q.open_handoff_query("m", "t", "s", None, None, "normal")[0]
    )


def test_take_over_is_a_compare_and_set() -> None:
    query, _ = thread_q.take_over_query("m", "t", "u", {})
    assert "AND assignee_user_id IS NULL" in query


@pytest.mark.parametrize(
    ("over", "answers"),
    [
        (dict(bot_template_id=AGENT, last_inbound_at=NOW), True),
        (dict(last_inbound_at=NOW), False),  # no agent
        (dict(bot_template_id=AGENT, last_inbound_at=None), False),  # she never wrote
        (dict(bot_template_id=AGENT, last_inbound_at=NOW, resolved_at=NOW), False),
        (dict(bot_template_id=AGENT, last_inbound_at=NOW, assignee_user_id="u"), False),
    ],
)
def test_a_thread_is_buddys_to_answer_only_while_nobody_else_holds_it(
    over, answers
) -> None:
    from app.crm.conversations import bot

    work = bot._work(_thread(**over))
    assert (work is not None) is answers
    if work is not None:
        assert work.agent_id == AGENT


@pytest.mark.parametrize("unanswered", [True, False])
async def test_buddy_owes_her_what_she_wrote_past_his_cursor(
    monkeypatch, unanswered
) -> None:
    """Owed = an inbound row written after the cursor — read from the
    timeline, never by comparing provider times: two messages in one second,
    or one that lands late, are still owed."""
    from app.crm.conversations import bot

    cursor = NOW - timedelta(minutes=1)
    asked: List[tuple] = []

    async def rows_after(merchant_id, thread_id, kinds, after, limit):
        asked.append((kinds, after, limit))
        return [SimpleNamespace(id="r1")] if unanswered else []

    monkeypatch.setattr(bot.message_accessor, "rows_after", rows_after)
    thread = _thread(bot_template_id=AGENT, last_inbound_at=NOW, bot_cursor_at=cursor)
    assert await bot.owes_reply(thread) is unanswered
    assert asked == [([words.KIND_INBOUND], cursor, 1)]


async def test_a_thread_buddy_does_not_answer_owes_nothing_without_a_read(
    monkeypatch,
) -> None:
    from app.crm.conversations import bot

    async def rows_after(*args: Any):
        raise AssertionError("no read for a thread a teammate holds")

    monkeypatch.setattr(bot.message_accessor, "rows_after", rows_after)
    taken = _thread(bot_template_id=AGENT, last_inbound_at=NOW, assignee_user_id="u")
    assert await bot.owes_reply(taken) is False


def test_buddy_reads_her_messages_in_the_order_they_were_written() -> None:
    """Inbound rows are stamped with clock_timestamp() under the thread's
    row lock, and Buddy's reads and cursor follow that stamp — not the
    provider's whole-second occurred_at."""
    insert, _ = message_q.insert_inbound_query(
        "m", "t", "inbound", "customer", None, None, None, NOW
    )
    assert "occurred_at, created_at)" in insert and "$8, clock_timestamp())" in insert
    after, values = message_q.rows_after_query("m", "t", ["inbound"], NOW, 50)
    assert "created_at > $4" in after and "ORDER BY created_at, id" in after
    assert "occurred_at >" not in after
    assert values[3] == NOW


def test_every_view_has_a_predicate() -> None:
    assert set(thread_q.VIEW_PREDICATES) == set(words.VIEWS)


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


#: Buddy's cursor on a hand back: never back past what Buddy answered
#: (GREATEST), never into the future, the clock when nothing was seen (LEAST).
HAND_BACK_CURSOR = (
    "bot_cursor_at = GREATEST( bot_cursor_at, "
    "LEAST($6::timestamptz, clock_timestamp()) )"
)


def test_a_hand_back_moves_buddys_cursor_past_everything_she_wrote() -> None:
    """Stamped under the same row lock as her messages: what she wrote
    before the hand back was the teammate's, the next one is Buddy's.
    LEAST ignores the missing ``seen_upto``, so the cursor is the clock."""
    query, values = thread_q.hand_back_query("shop", "t", "u", AGENT, {})
    assert HAND_BACK_CURSOR in " ".join(query.split())
    assert values[5] is None


def test_a_hand_back_leaves_buddy_what_the_teammate_never_saw() -> None:
    """She wrote while the teammate clicked Hand back: the cursor stops at
    when the newest of her messages the console showed was written, so the
    one it did not show is Buddy's to answer instead of being taken as
    handled — and a stale console never moves it back past what Buddy
    already answered."""
    seen = NOW - timedelta(seconds=5)
    query, values = thread_q.hand_back_query("shop", "t", "u", AGENT, {}, seen)
    assert HAND_BACK_CURSOR in " ".join(query.split())
    assert values[5] == seen


async def test_resolve_refuses_while_she_has_written_since_the_teammate_looked(
    monkeypatch,
) -> None:
    """She wrote "wait, one more thing" as the teammate clicked Resolve: the
    thread is locked first, then the server reads when the newest of her
    messages the console showed was written, then looks past it — a message
    already written is seen and the resolve refused; nothing is resolved."""
    from app.crm.conversations import threads as threads_module
    from app.crm.conversations.errors import ThreadConflict

    calls: List[tuple] = []
    wrote = {"since": True}
    seen = NOW - timedelta(seconds=5)

    async def get_thread(merchant_id, thread_id, txn=None, for_update=False):
        calls.append(("lock", for_update))
        return _thread()

    async def written_at(txn, merchant_id, thread_id, row_id, kind):
        calls.append(("seen", row_id, kind))
        return seen if row_id == "m-shown" else None

    async def wrote_since(txn, merchant_id, thread_id, kind, after):
        calls.append(("look", kind, after))
        return wrote["since"]

    async def resolve(txn, merchant_id, thread_id, last_inbound_at=None):
        calls.append(("resolve",))
        return _thread(resolved_at=NOW)

    async def close(*args: Any):
        return None

    async def wake(*args: Any):
        return None

    monkeypatch.setattr(threads_module.thread_accessor, "get_thread", get_thread)
    monkeypatch.setattr(threads_module.message_accessor, "written_at", written_at)
    monkeypatch.setattr(threads_module.message_accessor, "wrote_since", wrote_since)
    monkeypatch.setattr(threads_module.thread_accessor, "resolve", resolve)
    monkeypatch.setattr(threads_module.handoff_accessor, "close", close)
    monkeypatch.setattr(threads_module, "wake", wake)
    txn: Any = object()  # the fakes above never touch it

    async def resolve_seen(seen_message_id: Optional[str]):
        return await threads_module.resolve_thread_in_txn(
            txn,
            "shop",
            "t",
            words.OUTCOME_RESOLVED,
            "u-1",
            seen_message_id=seen_message_id,
        )

    looked = [
        ("lock", True),
        ("seen", "m-shown", words.KIND_INBOUND),
        ("look", words.KIND_INBOUND, seen),
    ]
    with pytest.raises(ThreadConflict, match="written since you last looked"):
        await resolve_seen("m-shown")
    assert calls == looked

    calls.clear()
    wrote["since"] = False  # nothing new: resolved as asked
    assert await resolve_seen("m-shown")
    assert calls == [*looked, ("resolve",)]

    calls.clear()  # not one of her messages on this thread: refresh first
    with pytest.raises(ThreadConflict, match="refresh"):
        await resolve_seen("m-teammates-own-reply")
    assert ("resolve",) not in calls

    calls.clear()  # nothing seen sent (a sweep, an older console): no look
    await resolve_seen(None)
    assert calls == [("resolve",)]
    query, values = message_q.wrote_since_query("shop", "t", "inbound", seen)
    assert "created_at > $4" in query and values[2:] == ["inbound", seen]
    query, values = message_q.written_at_query("shop", "t", "m-shown", "inbound")
    assert "id = $3::uuid" in query and "kind = $4" in query


async def test_a_hand_back_reads_the_seen_message_on_the_server(monkeypatch) -> None:
    """The console sends which of her messages it saw, never a clock: the
    server reads when that row was written and stops Buddy's cursor there."""
    from app.crm.conversations import threads as threads_module

    seen = NOW - timedelta(seconds=5)
    handed: Dict[str, Any] = {}

    async def written_at(txn, merchant_id, thread_id, row_id, kind):
        return seen if (row_id, kind) == ("m-shown", words.KIND_INBOUND) else None

    async def hand_back(txn, merchant_id, thread_id, user_id, agent_id, trail, upto):
        handed["upto"] = upto
        return _thread()

    async def nothing(*args: Any):
        return None

    monkeypatch.setattr(threads_module.message_accessor, "written_at", written_at)
    monkeypatch.setattr(threads_module.thread_accessor, "hand_back", hand_back)
    monkeypatch.setattr(threads_module.handoff_accessor, "close", nothing)
    monkeypatch.setattr(threads_module, "wake", nothing)
    txn: Any = object()
    await threads_module._hand_back_in_txn(
        txn, "shop", "t", "u-1", AGENT, {}, "m-shown"
    )
    assert handed["upto"] == seen
    await threads_module._hand_back_in_txn(txn, "shop", "t", "u-1", AGENT, {}, None)
    assert handed["upto"] is None  # the clock, as before


async def test_a_note_and_its_wake_up_share_one_atom(monkeypatch) -> None:
    """A wake-up that fails must undo the note too: the console keeps the
    text on an error and the teammate saves it again."""
    from app.crm.conversations import threads as threads_module
    from app.crm.conversations.access import Actor

    atoms: List[tuple] = []

    async def thread_or_404(merchant_id, thread_id):
        return _thread()

    async def atomically(fn, *args):
        atoms.append((fn.__name__, args))
        return SimpleNamespace()

    monkeypatch.setattr(threads_module, "thread_or_404", thread_or_404)
    monkeypatch.setattr(threads_module, "atomically", atomically)
    teammate = Actor(user_id="u-1", read_only=False, manager=False)
    await threads_module.add_note("shop", "t", "call her back", teammate)
    [(name, args)] = atoms
    assert name == "_add_note_in_txn"

    seen: List[tuple] = []
    txn: Any = object()

    async def insert_outbound(handle: Any, *rest: Any):
        seen.append(("insert", handle))
        return SimpleNamespace()

    async def wake(*rest: Any):
        seen.append(("wake", rest[-1]))

    monkeypatch.setattr(
        threads_module.message_accessor, "insert_outbound", insert_outbound
    )
    monkeypatch.setattr(threads_module, "wake", wake)
    await threads_module._add_note_in_txn(txn, *args)
    assert seen == [("insert", txn), ("wake", txn)]


def test_the_lapse_closes_only_the_handoff_it_judged_while_still_unclaimed() -> None:
    """The sweep judged a handoff from a page it read earlier: a teammate who
    took it since, or a newer handoff on the thread, is never closed."""
    query, values = handoff_q.lapse_query("shop", "h", words.OUTCOME_SLA_LAPSED, 10)
    assert "AND id = $2::uuid" in query and "conversation_id =" not in query
    assert "AND closed_at IS NULL" in query and "AND claimed_at IS NULL" in query
    assert "AND opened_at <= now() - make_interval(mins => $4::int)" in query
    assert values == ["shop", "h", "sla_lapsed", 10]


def test_retention_rechecks_the_age_on_the_row_it_deletes() -> None:
    """A delete that waited on her reopening the thread re-checks only its
    own WHERE: the age is there too, so the reopened thread survives."""
    query, _ = thread_q.delete_resolved_query(90, 500)
    outer = query[query.index("LIMIT $2") :]
    assert "AND resolved_at < now() - make_interval(days => $1::int)" in outer


# --- the sweeps resolve only the thread they read ----------------------------


def test_a_sweep_resolve_is_guarded_by_what_it_read() -> None:
    query, values = thread_q.resolve_query("shop", "t", NOW)
    assert "AND ($3::timestamptz IS NULL OR last_inbound_at = $3)" in query
    assert values == ["shop", "t", NOW]
    # a teammate's resolve stays unconditional
    assert thread_q.resolve_query("shop", "t")[1] == ["shop", "t", None]


async def test_a_message_that_lands_mid_sweep_keeps_the_thread_open(
    monkeypatch,
) -> None:
    """The window shut and the sweep read the thread; she writes before it
    resolves — the guarded resolve misses and the thread stays open."""
    calls: List[tuple] = []

    async def buddy_binding(merchant_id, channel):
        return SimpleNamespace(id=BUDDY_BINDING)

    async def settings(merchant_id, channel, binding_id=None):
        return SimpleNamespace(
            claim_sla_minutes=10, closing_lead_minutes=15, closing_message="bye"
        )

    async def open_for_thread(merchant_id, thread_id):
        return None

    async def atomically(fn, *args):
        calls.append((fn.__name__, args))
        return None  # her message moved last_inbound_at: the guard missed

    monkeypatch.setattr(sweeps, "buddy_binding", buddy_binding)
    monkeypatch.setattr(sweeps, "conversation_settings", settings)
    monkeypatch.setattr(sweeps.handoff_accessor, "open_for_thread", open_for_thread)
    monkeypatch.setattr(sweeps, "atomically", atomically)
    read = NOW - timedelta(hours=25)  # her window shut an hour ago
    thread = _thread(bot_template_id=AGENT, last_inbound_at=read)
    action = await sweeps._close_one(thread, 24, {}, {}, NOW)
    [(name, args)] = calls
    assert name == "resolve_thread_in_txn" and args[-1] == read
    assert action == sweeps.ACTION_WAIT  # not counted as expired


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


async def test_the_closing_sweep_walks_every_conversation_channel(
    monkeypatch,
) -> None:
    asked: List[str] = []

    async def candidates(channel, older_than, after, limit):
        asked.append(channel)
        return []

    monkeypatch.setattr(
        sweeps, "conversation_channels", lambda: ("whatsapp", "instagram")
    )
    monkeypatch.setattr(
        sweeps, "conversation_profile", lambda channel: SimpleNamespace(window_hours=24)
    )
    monkeypatch.setattr(sweeps.thread_accessor, "closing_candidates", candidates)
    await sweeps.sweep_closing(50)
    assert asked == ["whatsapp", "instagram"]


async def test_each_waiting_thread_reads_its_own_bindings_claim_sla(
    monkeypatch,
) -> None:
    """The Inbox list and the open thread read the same settings: the ones
    on the thread's own channel and binding — one read per binding."""
    from app.crm.conversations import threads as threads_module

    reads: List[Optional[str]] = []

    async def settings_for(thread):
        reads.append(thread.binding_id)
        return SimpleNamespace(
            claim_sla_minutes={"b-1": 10, "b-2": 30}[thread.binding_id]
        )

    monkeypatch.setattr(threads_module, "settings_for", settings_for)
    waiting = [
        _thread(id="t-a", binding_id="b-1"),
        _thread(id="t-b", binding_id="b-2"),
        _thread(id="t-c", binding_id="b-1"),
    ]
    quiet = _thread(id="t-d", binding_id="b-2")
    handoffs = {t.id: _handoff() for t in waiting}
    slas = await threads_module._claim_slas([*waiting, quiet], handoffs)
    assert slas == {"t-a": 10, "t-b": 30, "t-c": 10}
    assert reads == ["b-1", "b-2"]


@pytest.mark.parametrize(
    ("channel", "templates", "closed_hint"),
    [
        ("whatsapp", True, "send a template instead"),
        ("instagram", False, "it opens again when they write"),
        ("widget", False, None),
    ],
)
async def test_a_template_reply_needs_a_channel_that_registers_them(
    monkeypatch, channel, templates, closed_hint
) -> None:
    from app.crm.conversations import reply
    from app.crm.conversations.access import Actor
    from app.crm.conversations.errors import ThreadConflict

    teammate = Actor(user_id="u-1", read_only=False, manager=False)

    async def holder(merchant_id, thread_id, actor):
        return _thread(channel=channel)

    monkeypatch.setattr(reply, "_holder", holder)
    monkeypatch.setattr(reply, "registers_templates_for", lambda c: c == "whatsapp")
    monkeypatch.setattr(reply, "window_of", lambda t, now: SimpleNamespace(open=False))
    if not templates:
        with pytest.raises(ThreadConflict, match="has no templates"):
            await reply.reply_template("shop", "t", "tpl-1", {}, teammate)
    if closed_hint is not None:
        with pytest.raises(ThreadConflict, match=closed_hint):
            await reply.reply_text("shop", "t", "hi", teammate)


async def test_a_reply_is_recorded_in_one_atom_with_a_short_preview(
    monkeypatch,
) -> None:
    """Sent, then recorded: the timeline row, the list row and the wake-up
    share one atom (a failure part-way never leaves the list stale or the
    Inbox unwoken), and the preview is the list's one short line."""
    from app.crm.conversations import reply
    from app.crm.conversations.access import Actor

    teammate = Actor(user_id="u-1", read_only=False, manager=False)
    sent: Dict[str, Any] = {}
    atoms: List[tuple] = []

    async def holder(merchant_id, thread_id, actor):
        return _thread(assignee_user_id="u-1")

    async def send_session(**kwargs: Any):
        sent.update(kwargs)
        return SimpleNamespace(
            message_id="m-1",
            provider_message_id="wamid.9",
            status="accepted",
            reason=None,
        )

    async def atomically(fn, *args):
        atoms.append((fn.__name__, args))
        return SimpleNamespace()  # the timeline row

    monkeypatch.setattr(reply, "_holder", holder)
    monkeypatch.setattr(reply, "window_of", lambda t, now: SimpleNamespace(open=True))
    monkeypatch.setattr(reply, "send_session", send_session)
    monkeypatch.setattr(reply, "atomically", atomically)
    await reply.reply_text("shop", "t", "line one\n" + "word " * 1000, teammate)
    # users.id is free text (migration 019) and source_id a uuid: the teammate
    # is named on the timeline row (author_user_id), never on the manifest.
    assert sent["source_id"] is None
    [(name, args)] = atoms
    assert name == "_record_in_txn"
    preview = args[6]
    assert len(preview) <= project.PREVIEW_MAX and "\n" not in preview

    seen: List[tuple] = []
    txn: Any = object()

    async def insert_outbound(handle: Any, *rest: Any):
        seen.append(("insert", handle))
        return SimpleNamespace()

    async def touch_outbound(*rest: Any):
        seen.append(("touch", rest[-1]))

    async def wake(*rest: Any):
        seen.append(("wake", rest[-1]))

    monkeypatch.setattr(reply.message_accessor, "insert_outbound", insert_outbound)
    monkeypatch.setattr(reply.thread_accessor, "touch_outbound", touch_outbound)
    monkeypatch.setattr(reply, "wake", wake)
    await reply._record_in_txn(txn, *args)
    assert seen == [("insert", txn), ("touch", txn), ("wake", txn)]


# --- may Buddy speak: re-checked before every send ---------------------------

#: bot_may_speak reads the wall clock: a handoff opened "now" is inside its SLA.
_JUST_NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize(
    ("thread", "handoff", "session_id", "may"),
    [
        (_thread(bot_template_id=AGENT), None, None, True),
        # her handoff is open: Buddy is silent...
        (_thread(bot_template_id=AGENT), _handoff(opened_at=_JUST_NOW), None, False),
        # ...bar the turn that asked for it (its waiting message)
        (
            _thread(bot_template_id=AGENT),
            _handoff(opened_at=_JUST_NOW),
            "55555555-5555-4555-8555-555555555555",
            True,
        ),
        # a teammate claimed it: even the asking session is silent
        (
            _thread(bot_template_id=AGENT, assignee_user_id="u-1"),
            _handoff(opened_at=_JUST_NOW, claimed_by="u-1", claimed_at=_JUST_NOW),
            "55555555-5555-4555-8555-555555555555",
            False,
        ),
        # Buddy moved to another number
        (
            _thread(bot_template_id=AGENT, binding_id="other-number"),
            None,
            None,
            False,
        ),
    ],
)
async def test_bot_may_speak(monkeypatch, thread, handoff, session_id, may) -> None:
    from app.crm.conversations import bot

    async def get_thread(merchant_id, thread_id):
        return thread

    async def open_for_thread(merchant_id, thread_id):
        return handoff

    async def settings(_thread):
        return SimpleNamespace(claim_sla_minutes=10)

    async def buddy(merchant_id, channel):
        return SimpleNamespace(id=BUDDY_BINDING)

    monkeypatch.setattr(bot.thread_accessor, "get_thread", get_thread)
    monkeypatch.setattr(bot.handoff_accessor, "open_for_thread", open_for_thread)
    monkeypatch.setattr(bot, "settings_for", settings)
    monkeypatch.setattr(bot, "buddy_binding", buddy)
    assert await bot.bot_may_speak("shop", thread.id, session_id) is may


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


# --- only the session answering a thread may hand it off ----------------------

_SESSION = "55555555-5555-4555-8555-555555555555"


@pytest.mark.parametrize(
    ("thread", "refused"),
    [
        (_thread(bot_template_id=AGENT, bot_session_id=_SESSION), None),
        # a web session naming someone else's thread
        (_thread(bot_template_id=AGENT, bot_session_id="other-session"), "NotAllowed"),
        # a teammate took it while Buddy's turn was running
        (_thread(assignee_user_id="u-1"), "ThreadConflict"),
        (_thread(resolved_at=NOW), "ThreadConflict"),
    ],
)
async def test_a_handoff_is_asked_only_by_the_threads_own_session(
    monkeypatch, thread, refused
) -> None:
    from app.crm.conversations import handoffs

    opened: List[str] = []

    async def get_thread(merchant_id, thread_id, txn=None, for_update=False):
        assert for_update, "checked under the row lock"
        return thread

    async def open_handoff(txn, merchant_id, thread_id, session_id, *rest):
        opened.append(session_id)
        return _handoff()

    async def wake(*args):
        return None

    async def settings(_thread):
        return SimpleNamespace(human_handoff=True)

    monkeypatch.setattr(handoffs.thread_accessor, "get_thread", get_thread)
    monkeypatch.setattr(handoffs.handoff_accessor, "open_handoff", open_handoff)
    monkeypatch.setattr(handoffs, "wake", wake)
    monkeypatch.setattr(handoffs, "settings_for", settings)
    txn: Any = object()  # the accessors are faked; nothing touches it
    call = handoffs._request_handoff_in_txn(
        txn, "shop", thread.id, _SESSION, "customer_requested", None, "normal"
    )
    if refused is None:
        assert (await call).id == _handoff().id and opened == [_SESSION]
    else:
        with pytest.raises(Exception) as caught:
            await call
        assert type(caught.value).__name__ == refused and opened == []

    async def plain_get_thread(merchant_id, thread_id, txn=None, for_update=False):
        return thread

    monkeypatch.setattr(handoffs.thread_accessor, "get_thread", plain_get_thread)
    assert await handoffs.handoff_available("shop", thread.id, _SESSION) is (
        refused is None
    )


# --- a new session hears the latest of the conversation ------------------------


def test_the_context_slice_is_the_latest_rows_before_the_burst() -> None:
    since, before = NOW - timedelta(hours=24), NOW
    query, values = message_q.rows_before_query(
        "shop", "t", ["inbound", "outbound"], since, before, 50
    )
    assert "ORDER BY occurred_at DESC, id DESC" in query
    assert "occurred_at > $4" in query and "occurred_at < $5" in query
    assert values == ["shop", "t", ["inbound", "outbound"], since, before, 50]


async def test_the_context_slice_reads_oldest_first(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from app.crm.conversations.db.accessors import message as message_accessor

    def row(n: int) -> Dict[str, Any]:
        return dict(
            id=f"r{n}",
            conversation_id="t",
            kind="inbound",
            author_kind="customer",
            author_user_id=None,
            event_raw_id=None,
            message_id=None,
            provider_message_id=None,
            body={"type": "text", "text": str(n)},
            occurred_at=NOW + timedelta(minutes=n),
            created_at=NOW + timedelta(minutes=n),
        )

    class Conn:
        async def fetch(self, query, *values):
            return [row(3), row(2), row(1)]  # the database answers newest first

    @asynccontextmanager
    async def crm_connection():
        yield Conn()

    monkeypatch.setattr(message_accessor, "crm_connection", crm_connection)
    rows = await message_accessor.rows_before("shop", "t", ["inbound"], NOW, NOW, 3)
    assert [r.id for r in rows] == ["r1", "r2", "r3"]


def test_a_widget_thread_is_never_this_answers_work() -> None:
    from app.crm.conversations import bot

    widget = _thread(channel="widget", bot_template_id=AGENT, last_inbound_at=NOW)
    assert bot._work(widget) is None


# --- the plan's once-only guarantees ------------------------------------------


def test_a_handoff_closes_once() -> None:
    """Closing is one conditional UPDATE: a second close (resolve racing the
    lapse sweep, say) finds nothing open and returns no row."""
    query, values = handoff_q.close_query("m", "t", words.OUTCOME_RESOLVED, None)
    assert "AND closed_at IS NULL" in query and "RETURNING" in query
    assert values == ["m", "t", "resolved", None]


async def test_the_closing_message_is_sent_once_per_window(monkeypatch) -> None:
    """Its dedupe key names the window, so a second sweep or pod finds the
    first send; her next message opens a new window and a new key."""
    keys: List[str] = []
    rows: List[str] = []

    async def send_session(**kwargs: Any):
        duplicate = kwargs["dedupe_key"] in keys
        keys.append(kwargs["dedupe_key"])
        return SimpleNamespace(
            duplicate=duplicate, message_id="m-1", provider_message_id="w-1"
        )

    async def insert_outbound(*args: Any):
        rows.append("row")
        return SimpleNamespace(id="r-1")

    monkeypatch.setattr(sweeps, "send_session", send_session)
    monkeypatch.setattr(sweeps.message_accessor, "insert_outbound", insert_outbound)
    thread = _thread(bot_template_id=AGENT, last_inbound_at=NOW)
    await sweeps._send_closing(thread, "bye", words.HELD_BY_BUDDY, NOW)
    await sweeps._send_closing(thread, "bye", words.HELD_BY_BUDDY, NOW)
    later = _thread(bot_template_id=AGENT, last_inbound_at=NOW + timedelta(days=1))
    await sweeps._send_closing(later, "bye", words.HELD_BY_BUDDY, NOW)
    assert keys[0] == keys[1] != keys[2]
    assert rows == ["row", "row"]  # the duplicate wrote no second timeline row


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
