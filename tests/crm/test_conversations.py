"""Conversations (inbox PR 2): the pure rules — the window, who holds a
thread, what a customer's message does, what the closing sweep does — the
projector's "Buddy's number only", the move consumer, who may act, and the
SQL's tenancy and vocabulary discipline."""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.crm.conversations import moves, project, status as words, sweeps
from app.crm.conversations.access import actor_of
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
BUDDY_NUMBER = "11111111-1111-4111-8111-111111111111"


def _thread(**over: Any) -> Thread:
    fields: Dict[str, Any] = dict(
        id="22222222-2222-4222-8222-222222222222",
        merchant_id="shop",
        channel="whatsapp",
        contact_key="33333333-3333-4333-8333-333333333333",
        customer_id="33333333-3333-4333-8333-333333333333",
        address="+919876543210",
        binding_id=BUDDY_NUMBER,
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
    assert plan_inbound(_thread(), None, AGENT, 10, NOW).buddy_answers
    unassigned = plan_inbound(_thread(), None, None, 10, NOW)
    assert not unassigned.buddy_answers and not unassigned.reopen


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
    return sweeps.closing_action(thread, BUDDY_NUMBER, held, 15, 24, last, NOW)


def test_a_thread_off_buddys_number_resolves_quietly() -> None:
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


# --- the projector: Buddy's number only (R1) --------------------------------


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
        async def buddy_number(merchant_id: str, channel: str):
            return self.buddy

        async def settings(
            merchant_id: str, channel: str, binding_id: Optional[str] = None
        ):
            return SimpleNamespace(default_agent_id=AGENT, claim_sla_minutes=10)

        async def atomically(fn, *args):
            self.atoms.append((fn.__name__, args))
            return None

        monkeypatch.setattr(project, "buddy_number", buddy_number)
        monkeypatch.setattr(project, "conversation_settings", settings)
        monkeypatch.setattr(project, "atomically", atomically)


async def test_a_message_to_buddys_number_is_projected(monkeypatch) -> None:
    world = _World(
        SimpleNamespace(id=BUDDY_NUMBER, address="PN_BUDDY", is_primary=False)
    )
    world.install(monkeypatch)
    await project.consume_conversation_event(
        _letter("message.inbound", _inbound("PN_BUDDY")),
        "33333333-3333-4333-8333-333333333333",
    )
    [(name, args)] = world.atoms
    assert name == "_project_inbound_in_txn"
    assert args[3] == "+919876543210" and args[4] == BUDDY_NUMBER  # address, number
    assert args[7] == {"type": "text", "text": "hi", "caption": None}


async def test_a_message_to_any_other_number_does_nothing(monkeypatch) -> None:
    world = _World(
        SimpleNamespace(id=BUDDY_NUMBER, address="PN_BUDDY", is_primary=False)
    )
    world.install(monkeypatch)
    letter = _letter("message.inbound", _inbound("PN_PRIMARY"))
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    _World(None).install(monkeypatch)  # Buddy on no number at all
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    assert world.atoms == []


async def test_letters_about_nobody_or_another_channel_are_ignored(monkeypatch) -> None:
    world = _World(
        SimpleNamespace(id=BUDDY_NUMBER, address="PN_BUDDY", is_primary=False)
    )
    world.install(monkeypatch)
    await project.consume_conversation_event(
        _letter("message.inbound", _inbound("PN_BUDDY")), None
    )
    await project.consume_conversation_event(
        _letter("message.inbound", _inbound("PN_BUDDY"), source="shopify"), "c"
    )
    assert world.atoms == []


async def test_a_template_joins_a_thread_only_on_a_shared_number(monkeypatch) -> None:
    """Templates go out from the primary; only when that is Buddy's number
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
    _World(SimpleNamespace(id=BUDDY_NUMBER, address="PN", is_primary=False)).install(
        monkeypatch
    )
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    assert seen == []
    _World(SimpleNamespace(id=BUDDY_NUMBER, address="PN", is_primary=True)).install(
        monkeypatch
    )
    await project.consume_conversation_event(
        letter, "33333333-3333-4333-8333-333333333333"
    )
    assert seen == ["looked"]


# --- Buddy moved (R7) -------------------------------------------------------


async def test_buddy_moving_resolves_the_old_numbers_threads(monkeypatch) -> None:
    resolved: List[tuple] = []

    async def resolve_number(merchant_id: str, binding_id: str):
        resolved.append((merchant_id, binding_id))
        return ["t-1"]

    monkeypatch.setattr(moves, "resolve_number", resolve_number)
    payload = {"channel": "whatsapp", "from_binding_id": "old", "to_binding_id": "new"}
    await moves.consume_buddy_moved(_letter("number.buddy_moved", payload), None)
    await moves.consume_buddy_moved(_letter("message.inbound", payload), None)
    assert resolved == [("shop", "old")]


def test_the_moved_letter_keys_are_spelled_the_same_on_both_sides() -> None:
    """letters.py writes them, record declares them (rule 12 forbids the
    import) — renamed on one side only, the consumer would read nothing."""
    from app.crm.record import catalog

    declared = {
        f.path.removeprefix("payload.")
        for f in catalog.CATALOG[("whatsapp", "number.buddy_moved")].fields
    }
    source = Path(__file__).parents[2] / "app/crm/connectivity/letters.py"
    assert all(f'"{key}"' in source.read_text() for key in declared)


# --- the SQL: tenancy first, vocabulary bound ------------------------------

QUERY_MODULES = (thread_q, message_q, handoff_q)
#: The builders that deliberately cross tenants: the drains.
CROSS_TENANT = {
    "claim_bot_work_query",
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
        / "app/database/migrations/082_create_inbox_schema.sql"
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


def test_take_over_is_a_compare_and_set_and_the_bot_claim_skips_locked_rows() -> None:
    query, _ = thread_q.take_over_query("m", "t", "u", {})
    assert "AND assignee_user_id IS NULL" in query
    query, values = thread_q.claim_bot_work_query(90, 2, ["whatsapp"], 10)
    assert "FOR UPDATE SKIP LOCKED" in query and values == [90, 10, 2, ["whatsapp"]]
    # the responder answers WhatsApp; the widget answers its own threads
    assert "t.channel = ANY($4::text[])" in query
    # a burst settles into one turn: her last message must be 2s old
    assert "t.last_inbound_at <= now() - make_interval(secs => $3::int)" in query
    assert "last_inbound_at > t.bot_cursor_at" in query


def test_every_view_has_a_predicate() -> None:
    assert set(thread_q.VIEW_PREDICATES) == set(words.VIEWS)


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
        return SimpleNamespace(id=BUDDY_NUMBER)

    monkeypatch.setattr(bot.thread_accessor, "get_thread", get_thread)
    monkeypatch.setattr(bot.handoff_accessor, "open_for_thread", open_for_thread)
    monkeypatch.setattr(bot, "settings_for", settings)
    monkeypatch.setattr(bot, "buddy_number", buddy)
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
