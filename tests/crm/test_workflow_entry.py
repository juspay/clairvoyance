"""The entry-rules consumer — one consumer, two reads (ADR 0023 §4;
context/reading-notes.md §15.3; rollout phase 13).

Read one: HER OPEN RUNS, each judged by the version it entered under —
a v3 run is ended by v3's goals and woken by v3's wait_event squares even
after v5 changed them, and every goal-cancel and reply names the run it
is about, never a sibling on another version. Read two: THE LIVE PLANS,
latest document — a new run always starts on the newest version.

Carried forward from earlier phases, now per run: B1 (phase 01 — no
resume without an answer), goal tiers keyed-first with the entry event's
time (phase 06), the goal stash (phase 09)."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.ladder import expand_stages
from app.crm.outreach.nodes import TIMEOUT
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
    WorkflowNode,
)
from app.crm.outreach.walker import pick_next
from app.crm.record.schemas import RawEvent
from tests.crm.doubles import patch_accessors

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)

# A COD-confirmation board: the first square listens for a button reply.
_LISTENING_PLAN = {
    "entry": {"topic": "orders/create"},
    "nodes": [
        {
            "id": "ask",
            "type": "wait_event",
            "topics": ["button.reply"],
            "key": "button_id",
            "minutes": 60,
        },
        {"id": "confirm", "type": "wait", "minutes": 1},
        {"id": "call", "type": "call", "template_id": "tpl-1"},
    ],
    "edges": [["ask", "confirm", "YES"], ["ask", "call", TIMEOUT]],
    "goal": {"topics": ["order.confirmed"]},
}

_CART_PLAN = {
    "entry": {"topic": "checkouts/update", "reenter": True, "cooldown_hours": 0},
    "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
    "edges": [],
    "goals": [
        {
            "topics": ["orders/create"],
            "key": {"event": "cart_token", "run": "cart_token"},
            "exit_reason": "goal_met",
        },
        {"topics": ["orders/create"], "exit_reason": "converted_elsewhere"},
    ],
}


def _flow(definition: Dict[str, Any] = _LISTENING_PLAN, version: int = 1) -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="plan",
        status="live",
        version=version,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=definition,
        draft=None,
    )


def _run(
    flow: Workflow,
    version: int,
    node: str,
    context: Optional[Dict[str, Any]] = None,
    key: str = "c-1",
) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=flow.id,
        workflow_version=version,
        customer_id=uuid4(),
        status="waiting",
        current_node=node,
        wake_at=NOW + timedelta(minutes=30),
        entered_at=NOW - timedelta(hours=1),
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919876543210", **(context or {})},
        enrollment_key=key,
        attempts=0,
        last_error=None,
    )


def _event(topic: str, payload: Dict[str, Any], event_id: str = "ev-1") -> RawEvent:
    return RawEvent(
        id=event_id,
        merchant_id="m1",
        source="shopify",
        topic=topic,
        schema_version="1",
        external_id=f"{topic}:{event_id}",
        payload=payload,
        received_at=NOW,
        occurred_at=NOW,
    )


class _Spine:
    """The accessor slice the consumer reads and writes through: the live
    plans, her open runs and the version rows are seeded; every write is
    recorded by the run it named. A run ends once — a second cancel of
    the same run answers False, as the UPDATE's status guard would."""

    def __init__(
        self,
        flows: List[Workflow],
        runs: List[EnrollmentRun],
        versions: Dict[Tuple[UUID, int], Dict[str, Any]],
    ) -> None:
        self.flows = flows
        self.runs = runs
        self.versions = {(str(wf), v): d for (wf, v), d in versions.items()}
        self.definition_reads: List[Tuple[str, int]] = []
        self.cancels: List[Tuple[str, str, Optional[Tuple[str, str]], Any]] = []
        self.resumes: List[Tuple[str, str, Dict[str, Any]]] = []
        self.facts: List[Tuple[str, str, Any]] = []
        self.exited: set = set()

    async def live_workflows(self, merchant_id: str) -> List[Workflow]:
        return list(self.flows)

    async def open_runs_for_customer(
        self, merchant_id: str, customer_id: str
    ) -> List[EnrollmentRun]:
        return list(self.runs)

    async def get_definition(
        self, merchant_id: str, workflow_id: str, version: int
    ) -> Optional[Dict[str, Any]]:
        self.definition_reads.append((workflow_id, version))
        return self.versions.get((workflow_id, version))

    async def cancel_run(
        self,
        merchant_id: str,
        run_id: str,
        exit_reason: str,
        occurred_at: Optional[datetime] = None,
        key: Optional[Tuple[str, str]] = None,
        context_patch: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self.cancels.append((run_id, exit_reason, key, context_patch))
        if run_id in self.exited:
            return False
        self.exited.add(run_id)
        return True

    async def resume_run_by_id(
        self,
        merchant_id: str,
        run_id: str,
        node_id: str,
        patch: Dict[str, Any],
        facts: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self.resumes.append((run_id, node_id, patch))
        self.facts.append((run_id, node_id, facts))
        return True


def _install(monkeypatch: pytest.MonkeyPatch, spine: _Spine) -> None:
    """Seed the spine on the per-table accessor each read lives in — the
    workflow read, the run reads, and the pinned-definition read (which
    definitions.py owns, on the version table)."""
    definitions._definitions.clear()
    for module, name in (
        (entry.workflow_accessor, "live_workflows"),
        (entry.enrollment_accessor, "open_runs_for_customer"),
        (definitions.version_accessor, "get_definition"),
        (entry.enrollment_accessor, "cancel_run"),
        (entry.enrollment_accessor, "resume_run_by_id"),
    ):
        monkeypatch.setattr(module, name, getattr(spine, name))


def _consume(event: RawEvent) -> None:
    asyncio.run(consume_attributed_event(event, "c-1", {}))


# --- phase 01, B1: no resume without an answer ---


@pytest.fixture
def listening(monkeypatch: pytest.MonkeyPatch) -> _Spine:
    flow = _flow()
    run = _run(flow, 1, "ask")
    spine = _Spine([flow], [run], {(flow.id, 1): _LISTENING_PLAN})
    _install(monkeypatch, spine)
    return spine


def test_a_reply_carrying_the_key_wakes_the_listening_run(listening: _Spine) -> None:
    _consume(_event("button.reply", {"button_id": "YES"}))
    (run,) = listening.runs
    assert listening.resumes == [
        (str(run.id), "ask", {"reply_ask": "YES", "latest_letter": "ask"})
    ]
    assert listening.cancels == []  # a button reply is not a goal event


def test_a_reply_without_the_key_never_wakes_the_run(listening: _Spine) -> None:
    """B1: with no answer there is nothing to branch on. Resuming with
    None made the walker take the timeout edge immediately — the listening
    window ended early on an unrelated letter. The window continues; only
    the alarm may time it out."""
    _consume(_event("button.reply", {"text": "hello"}))
    assert listening.resumes == []


# --- phase 06 + 09: goal tiers keyed-first, per run; the goal stash ---


@pytest.fixture
def carts(monkeypatch: pytest.MonkeyPatch) -> _Spine:
    """Two open cart runs of hers on one keyed plan: chk-1 and chk-2."""
    flow = _flow(_CART_PLAN)
    runs = [
        _run(flow, 1, "wait-30m", {"cart_token": "chk-1"}, key="chk-1"),
        _run(flow, 1, "wait-30m", {"cart_token": "chk-2"}, key="chk-2"),
    ]
    spine = _Spine([flow], runs, {(flow.id, 1): _CART_PLAN})
    _install(monkeypatch, spine)
    return spine


def test_an_order_carrying_the_cart_token_is_judged_keyed_first(carts: _Spine) -> None:
    """THIS cart recovered -> goal_met on the run keyed to it, and that
    run is ended once (the keyed tier's verdict stands; the unkeyed tier
    never sweeps it). Any other open run of hers still ends, as
    converted_elsewhere (never nudge someone who just bought)."""
    _consume(_event("orders/create", {"cart_token": "chk-1"}))
    first, second = carts.runs
    assert [(r, reason, key) for r, reason, key, _ in carts.cancels] == [
        (str(first.id), "goal_met", ("cart_token", "chk-1")),
        (str(second.id), "converted_elsewhere", None),
    ]


def test_an_order_without_the_key_can_only_end_runs_elsewhere(carts: _Spine) -> None:
    _consume(_event("orders/create", {"total_price": "1850.00"}))
    assert [reason for _, reason, _, _ in carts.cancels] == ["converted_elsewhere"] * 2


def test_the_goal_cancel_stashes_the_goal_event_with_its_amount(carts: _Spine) -> None:
    """Phase 09: the run remembers which letter ended it — and how much it
    was worth, when the payload says so as a number — so the summary can
    sum recovered revenue without re-reading the spine."""
    _consume(
        _event(
            "orders/create",
            {"cart_token": "chk-1", "total_price": "1850.00"},
            "ev-order",
        )
    )
    stash = {
        "goal": {"topic": "orders/create", "event_id": "ev-order", "amount": "1850.00"}
    }
    assert [patch for _, _, _, patch in carts.cancels] == [stash, stash]
    carts.cancels.clear()
    _consume(
        _event(
            "orders/create", {"cart_token": "chk-1", "total_price": "n/a"}, "ev-order"
        )
    )
    assert carts.cancels[0][3] == {
        "goal": {"topic": "orders/create", "event_id": "ev-order"}
    }


# --- phase 13: each open run is judged by the version it entered under ---

# One keyed order plan, three versions apart: v3's goal is the payment, v5
# moved the goal to fulfilment, renamed the listening topic AND the entry
# topic. Order A entered under v3, order B under v5; both are open.
_V3 = {
    "entry": {
        "topic": "orders/create",
        "key": "order_id",
        "on_repeat": "refresh_latest",
    },
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {
            "id": "ask",
            "type": "wait_event",
            "topics": ["button.reply"],
            "key": "button_id",
            "minutes": 60,
        },
    ],
    "edges": [["wait-30m", "ask"]],
    "goals": [
        {
            "topics": ["orders/paid"],
            "key": {"event": "order_id", "run": "order_id"},
            "exit_reason": "goal_met",
        }
    ],
}
_V5 = {
    "entry": {"topic": "orders/confirmed", "key": "order_id", "on_repeat": "ignore"},
    "nodes": [
        {"id": "hold-30m", "type": "wait", "minutes": 30},
        {
            "id": "ask",
            "type": "wait_event",
            "topics": ["list.reply"],
            "key": "button_id",
            "minutes": 60,
        },
    ],
    "edges": [["hold-30m", "ask"]],
    "goals": [
        {
            "topics": ["orders/fulfilled"],
            "key": {"event": "order_id", "run": "order_id"},
            "exit_reason": "goal_met",
        }
    ],
}


@pytest.fixture
def two_versions(monkeypatch: pytest.MonkeyPatch) -> _Spine:
    flow = _flow(_V5, version=5)
    runs = [
        _run(flow, 3, "ask", {"order_id": "A"}, key="A"),
        _run(flow, 5, "ask", {"order_id": "B"}, key="B"),
    ]
    spine = _Spine([flow], runs, {(flow.id, 3): _V3, (flow.id, 5): _V5})
    _install(monkeypatch, spine)
    return spine


def test_a_goal_ends_only_the_runs_whose_own_version_names_it(
    two_versions: _Spine,
) -> None:
    """orders/paid is v3's goal, not v5's: order A's run ends, order B's
    is untouched — even though the live plan (v5) has no such goal."""
    a, b = two_versions.runs
    _consume(_event("orders/paid", {"order_id": "A"}))
    assert [(r, reason) for r, reason, _, _ in two_versions.cancels] == [
        (str(a.id), "goal_met")
    ]
    two_versions.cancels.clear()
    _consume(_event("orders/fulfilled", {"order_id": "B"}))
    assert [(r, reason) for r, reason, _, _ in two_versions.cancels] == [
        (str(b.id), "goal_met")
    ]


def test_a_reply_wakes_only_the_runs_whose_own_version_listens_for_it(
    two_versions: _Spine,
) -> None:
    a, b = two_versions.runs
    _consume(_event("button.reply", {"button_id": "YES"}))
    assert two_versions.resumes == [
        (str(a.id), "ask", {"reply_ask": "YES", "latest_letter": "ask"})
    ]
    two_versions.resumes.clear()
    _consume(_event("list.reply", {"button_id": "NO"}))
    assert two_versions.resumes == [
        (str(b.id), "ask", {"reply_ask": "NO", "latest_letter": "ask"})
    ]


def test_entries_still_match_the_latest_version(
    two_versions: _Spine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new run always starts on the newest document: v5's entry topic
    enrols, v3's old one no longer does."""
    enrolled: List[Any] = []

    async def enrol(**kwargs: Any) -> object:
        enrolled.append(kwargs["workflow"].version)
        return object()

    monkeypatch.setattr(entry, "enrol", enrol)
    _consume(_event("orders/confirmed", {"order_id": "C"}))
    assert enrolled == [5]
    _consume(_event("orders/create", {"order_id": "D"}))
    assert enrolled == [5]


def test_a_repeat_is_judged_by_the_open_runs_own_version(
    two_versions: _Spine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused enrol is a repeat of order A's run, which entered under
    v3: its repeat words (refresh_latest) and its first square (wait-30m)
    are v3's — the live v5 says ignore and renamed the square, and with
    v5's words the patch would never find the run."""
    repeats: List[Any] = []

    async def refused(**kwargs: Any) -> None:
        return None

    async def apply_repeat(*args: Any) -> bool:
        repeats.append(args)
        return True

    monkeypatch.setattr(entry, "enrol", refused)
    monkeypatch.setattr(entry, "apply_repeat", apply_repeat)
    # v5's entry topic, but the order is A's -> the open run on v3
    _consume(_event("orders/confirmed", {"order_id": "A"}))
    ((_, _, key, door, _, _),) = repeats
    assert key == "A"
    assert door.on_repeat == "refresh_latest"
    assert door.start == "wait-30m"


def test_a_run_whose_version_is_missing_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No version row for a pin: that run is neither ended nor woken (the
    walker parks it honestly at its next claim); her other runs are still
    judged, and the row is never left pending by a raise."""
    flow = _flow(_V5, version=5)
    orphan = _run(flow, 2, "ask", {"order_id": "A"}, key="A")
    fine = _run(flow, 5, "ask", {"order_id": "B"}, key="B")
    spine = _Spine([flow], [orphan, fine], {(flow.id, 5): _V5})
    _install(monkeypatch, spine)
    _consume(_event("orders/fulfilled", {"order_id": "B"}))
    assert [r for r, _, _, _ in spine.cancels] == [str(fine.id)]


def test_each_pinned_version_is_read_once_across_her_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _flow(_V5, version=5)
    runs = [_run(flow, 5, "ask", {"order_id": k}, key=k) for k in ("A", "B", "C")]
    spine = _Spine([flow], runs, {(flow.id, 5): _V5})
    _install(monkeypatch, spine)
    _consume(_event("list.reply", {"button_id": "YES"}))
    assert len(spine.resumes) == 3
    assert spine.definition_reads == [(str(flow.id), 5)]


# --- carried: the founding stamp, and pick_next's alarm law ---


def test_enrol_stamps_the_entry_events_time_and_repeats_never_move_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G7: the run remembers WHEN its founding letter happened (occurred_at,
    else received_at) under a bookkeeping key, so goals compare against the
    event's time rather than the row's insert time. A repeat (phase 00)
    refreshes facts, never the founding time — or an order placed between
    the founding checkout and a later cart update would stop counting."""
    seen: Dict[str, Any] = {}

    async def enrol(**kwargs: Any) -> None:
        seen["enrol"] = dict(kwargs["context"])
        return None

    async def apply_repeat(*args: Any) -> bool:
        seen["repeat"] = dict(args[5])
        return True

    monkeypatch.setattr(entry, "enrol", enrol)
    monkeypatch.setattr(entry, "apply_repeat", apply_repeat)
    definition = WorkflowDefinition.model_validate(
        {**_CART_PLAN, "entry": {**_CART_PLAN["entry"], "on_repeat": "refresh_latest"}}
    )
    happened = datetime(2026, 9, 3, 9, 30, tzinfo=timezone.utc)
    event = RawEvent(
        id="ev-1",
        merchant_id="m1",
        source="shopify",
        topic="checkouts/update",
        schema_version="1",
        external_id="x",
        payload={"cart_token": "chk-1", "entered_event_at": "forged"},
        received_at=NOW,
        occurred_at=happened,
    )
    asyncio.run(
        entry._try_enrol(_flow(), definition, definition.entries[0], event, "c-1", {})
    )
    assert seen["enrol"]["entered_event_at"] == happened.isoformat()
    assert "entered_event_at" not in seen["repeat"]
    assert "source_event_id" not in seen["repeat"]
    assert seen["repeat"]["cart_token"] == "chk-1"


def test_the_alarm_path_still_times_a_listening_node_out() -> None:
    """pick_next keeps its law: no answer in context = the alarm fired ->
    the timeout edge. B1 is fixed at the consumer, never here."""
    node = WorkflowNode(
        id="ask",
        type="wait_event",
        topics=["button.reply"],
        key="button_id",
        minutes=60,
    )
    arrows: List[Tuple[str, Optional[str]]] = [("confirm", "YES"), ("call", TIMEOUT)]
    assert pick_next(node, arrows, {}) == "call"
    assert pick_next(node, arrows, {"reply_ask": "YES"}) == "confirm"


# --- rollout phase 15: $topic branching, and a door per topic ---

_LADDER: Dict[str, Any] = {
    "entry": [
        {"topic": "loan.profile_created", "start": "at-profile"},
        {"topic": "loan.kyc_completed", "start": "at-kyc"},
    ],
    "key": "application_id",
    "reenter": True,
    "cooldown_hours": 0,
    "nodes": [
        {
            "id": "at-profile",
            "type": "wait_event",
            "key": "$topic",
            "minutes": 30,
            "topics": ["loan.kyc_completed", "loan.bank_linked"],
        },
        {"id": "call-profile", "type": "call", "template_id": "tpl-p"},
        {"id": "at-kyc", "type": "wait", "minutes": 30},
    ],
    "edges": [
        ["at-profile", "at-kyc", "loan.kyc_completed"],
        ["at-profile", "call-profile", "timeout"],
    ],
    "goal": {"topics": ["loan.disbursed"]},
}


def test_a_topic_keyed_square_wakes_with_the_topic_as_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """key: "$topic" — the branch is the event's TOPIC, not a payload
    field, so a stage board can say "she went to KYC" from the letter's
    name alone (§14.1 prerequisite 1)."""
    flow = _flow(_LADDER, version=1)
    run = _run(flow, 1, "at-profile", {"application_id": "L-1"}, key="L-1")
    spine = _Spine([flow], [run], {(flow.id, 1): _LADDER})
    _install(monkeypatch, spine)
    # bank_linked is listened for at-profile but is not a door of this
    # plan, so the letter wakes the run and starts nothing.
    _consume(_event("loan.bank_linked", {"application_id": "L-1"}))
    assert spine.resumes == [
        (
            str(run.id),
            "at-profile",
            {"reply_at-profile": "loan.bank_linked", "latest_letter": "at-profile"},
        )
    ]


def test_each_door_starts_the_run_on_its_own_square(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A journey first seen at KYC enrols on the KYC square, not on
    nodes[0] (§14.1 prerequisite 2)."""
    flow = _flow(_LADDER, version=1)
    spine = _Spine([flow], [], {(flow.id, 1): _LADDER})
    _install(monkeypatch, spine)
    doors: List[Tuple[str, str]] = []

    async def enrol(**kwargs: Any) -> object:
        doors.append((kwargs["door"].topic, kwargs["door"].start))
        return object()

    monkeypatch.setattr(entry, "enrol", enrol)
    _consume(_event("loan.kyc_completed", {"application_id": "L-2"}))
    _consume(_event("loan.profile_created", {"application_id": "L-3"}))
    _consume(_event("loan.bank_linked", {"application_id": "L-4"}))  # no door
    assert doors == [
        ("loan.kyc_completed", "at-kyc"),
        ("loan.profile_created", "at-profile"),
    ]


# --- rollout phase 16: the letter's facts ride with the reply; a parked run
# hears its square ---


def test_a_reply_carries_the_letters_scalar_facts_for_its_square(
    listening: _Spine,
) -> None:
    """The scalar facts of the letter (never nested objects, never the
    walker's bookkeeping names) are offered under the square that heard
    it, so a later call template can say what this stage's letter said."""
    (run,) = listening.runs
    run.status = "parked"  # an event moves a parked run too (the statement decides)
    _consume(
        _event(
            "button.reply",
            {
                "button_id": "YES",
                "amount": 5,
                "nested": {"a": 1},
                "reply_ask": "forged",
            },
        )
    )
    assert listening.resumes == [
        (str(run.id), "ask", {"reply_ask": "YES", "latest_letter": "ask"})
    ]
    assert listening.facts == [(str(run.id), "ask", {"button_id": "YES", "amount": 5})]


# --- rollout phase 17 sweep: a letter that moves a run is its answer, never
# also its repeat ---


def test_a_letter_the_square_listens_for_moves_the_run_and_is_not_its_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KYC letter arrives while the run stands on at-profile, which
    listens for it: the reply moves the run (its alarm becomes now). The
    same letter is also the KYC door's topic and enrol refuses it — a run
    for this application is open — and with restart_on_repeat that
    refusal would patch the run "anywhere", pushing the alarm the wake
    just set back by the debounce: the token would sit half an hour on a
    square it has already answered, and every stage clock of a ladder
    would run twice. A letter the current square listens for is that
    run's answer, not its repeat. The same stage's retry — a letter the
    square does NOT listen for — is the repeat, as before."""
    ladder = {**_LADDER, "restart_on_repeat": True, "debounce_minutes": 30}
    flow = _flow(ladder, version=1)
    run = _run(flow, 1, "at-profile", {"application_id": "L-1"}, key="L-1")
    spine = _Spine([flow], [run], {(flow.id, 1): ladder})
    _install(monkeypatch, spine)
    repeats: List[Any] = []

    async def refused(**kwargs: Any) -> None:
        return None

    async def apply_repeat(*args: Any) -> bool:
        repeats.append(args)
        return True

    monkeypatch.setattr(entry, "enrol", refused)
    monkeypatch.setattr(entry, "apply_repeat", apply_repeat)
    _consume(_event("loan.kyc_completed", {"application_id": "L-1"}))
    assert spine.resumes == [
        (
            str(run.id),
            "at-profile",
            {"reply_at-profile": "loan.kyc_completed", "latest_letter": "at-profile"},
        )
    ]
    assert repeats == []
    # the profile stage retried: at-profile does not listen for its own
    # topic, so this IS a repeat of the profile door
    _consume(_event("loan.profile_created", {"application_id": "L-1"}, "ev-2"))
    assert len(spine.resumes) == 1
    ((_, _, key, door, event_id, _),) = repeats
    assert (key, door.topic, event_id) == ("L-1", "loan.profile_created", "ev-2")


# --- rollout phase 18: a listening square hears only the letter about ITS run ---

_CALL_PLAN: Dict[str, Any] = {
    "entry": {"topic": "checkout.created", "key": "order_id"},
    "nodes": [
        {"id": "rescue-call", "type": "call", "template_id": "t"},
        {
            "id": "after-call",
            "type": "wait_event",
            "topics": ["call.completed"],
            "key": "outcome",
            "minutes": 1440,
            "match": {"payload": "enrollment_id", "run": "id"},
        },
        {"id": "wa-fallback", "type": "wait", "minutes": 5},
        {"id": "wait-1d", "type": "wait", "minutes": 1440},
    ],
    "edges": [
        ["rescue-call", "after-call"],
        ["after-call", "wa-fallback", "NO_ANSWER"],
        ["after-call", "wait-1d", "else"],
    ],
    "goal": {"topics": ["order.placed"]},
}


def test_a_call_outcome_wakes_only_the_run_that_placed_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs of one customer (two orders) both wait after their calls.
    The outcome letter names the run that placed the call (enrollment_id,
    phase 18's mirror); the square's `match` compares it with the run's
    own id, so only that run hears it. A letter that names no run wakes
    neither — a claim about nobody is not a claim about this run."""
    flow = _flow(_CALL_PLAN, version=1)
    a = _run(flow, 1, "after-call", {"order_id": "A"}, key="A")
    b = _run(flow, 1, "after-call", {"order_id": "B"}, key="B")
    spine = _Spine([flow], [a, b], {(flow.id, 1): _CALL_PLAN})
    _install(monkeypatch, spine)
    _consume(
        _event("call.completed", {"enrollment_id": str(b.id), "outcome": "NO_ANSWER"})
    )
    assert spine.resumes == [
        (
            str(b.id),
            "after-call",
            {"reply_after-call": "NO_ANSWER", "latest_letter": "after-call"},
        )
    ]
    _consume(_event("call.completed", {"outcome": "BUSY"}, "ev-2"))
    _consume(
        _event(
            "call.completed",
            {"enrollment_id": "someone-else", "outcome": "BUSY"},
            "ev-3",
        )
    )
    assert len(spine.resumes) == 1


def test_match_may_name_a_context_field_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run side of `match` is `id` or any context field — the lead the
    call node wrote (lead_<node>), or the message a send queued
    (message_<node>, for the receipts of #1040), compared as text."""
    plan = {
        **_CALL_PLAN,
        "nodes": [
            _CALL_PLAN["nodes"][0],
            {
                **_CALL_PLAN["nodes"][1],
                "match": {"payload": "lead_id", "run": "lead_rescue-call"},
            },
            *_CALL_PLAN["nodes"][2:],
        ],
    }
    flow = _flow(plan, version=1)
    a = _run(
        flow, 1, "after-call", {"order_id": "A", "lead_rescue-call": "L-A"}, key="A"
    )
    b = _run(
        flow, 1, "after-call", {"order_id": "B", "lead_rescue-call": "L-B"}, key="B"
    )
    spine = _Spine([flow], [a, b], {(flow.id, 1): plan})
    _install(monkeypatch, spine)
    _consume(_event("call.completed", {"lead_id": "L-A", "outcome": "BUSY"}))
    assert [r[0] for r in spine.resumes] == [str(a.id)]


def test_else_catches_any_answer_without_an_arrow_of_its_own() -> None:
    """Buddy's outcome after a connected call is the template's own word
    (CONFIRMED, not_found, …), unknowable to the plan: `else` keeps the
    post-call listening day for every outcome the plan did not name. A
    named arrow wins over it; with no timeout arrow, the alarm takes it
    too."""
    node = WorkflowNode(
        id="after-call",
        type="wait_event",
        topics=["call.completed"],
        key="outcome",
        minutes=5,
    )
    arrows: List[Tuple[str, Optional[str]]] = [
        ("wa-fallback", "NO_ANSWER"),
        ("wait-1d", "else"),
    ]
    assert pick_next(node, arrows, {"reply_after-call": "NO_ANSWER"}) == "wa-fallback"
    assert pick_next(node, arrows, {"reply_after-call": "CONFIRMED"}) == "wait-1d"
    assert pick_next(node, arrows, {}) == "wait-1d"
    assert pick_next(node, arrows + [("end", TIMEOUT)], {}) == "end"
    assert pick_next(node, arrows[:1], {"reply_after-call": "CONFIRMED"}) is None


def test_on_a_keyed_ladder_a_stage_letter_moves_only_its_own_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One customer, two loan applications, both at the profile stage. The
    KYC letter for application B moves B's run only — A keeps its clock."""
    board = expand_stages(
        {
            "key": "application_id",
            "reenter": True,
            "cooldown_hours": 0,
            "goals": [{"topics": ["loan.disbursed"]}],
            "stages": {
                "order": ["loan.profile_created", "loan.kyc_completed"],
                "idle_minutes": 30,
                "on_idle": {"type": "call", "template_id": "t"},
                "after_action_minutes": 60,
            },
        }
    )
    flow = _flow(board, version=1)
    a = _run(flow, 1, "at-profile-created", {"application_id": "A"}, key="A")
    b = _run(flow, 1, "at-profile-created", {"application_id": "B"}, key="B")
    spine = _Spine([flow], [a, b], {(flow.id, 1): board})
    _install(monkeypatch, spine)
    repeats: List[Any] = []

    async def refused(**kwargs: Any) -> None:
        return None  # the KYC door: a run for B is open

    async def apply_repeat(*args: Any) -> bool:
        repeats.append(args)
        return True

    monkeypatch.setattr(entry, "enrol", refused)
    monkeypatch.setattr(entry, "apply_repeat", apply_repeat)
    _consume(_event("loan.kyc_completed", {"application_id": "B"}))
    # one ask per listening square of B's document (at- and after-; the
    # statement's current_node guard takes the one B stands on) — none for A
    assert {r[0] for r in spine.resumes} == {str(b.id)}
    assert (str(b.id), "at-profile-created") in [(r[0], r[1]) for r in spine.resumes]
    assert repeats == []  # B's square answered it: moved, not a repeat


# --- merchant-level letters (Extracted.about == "merchant") -------------------


class _UntouchableAccessor:
    """Any attribute read is a failure: the consumer must return before it
    reaches the database for a letter that names no person."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"accessor.{name} touched for a merchant-level letter")


def test_a_merchant_level_letter_starts_ends_and_wakes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A template review or an account notice has no person: outreach's
    # business begins where a customer does, so the consumer returns at
    # once — no open-run read, no entry match.
    import app.crm.outreach.entry as entry_module

    patch_accessors(monkeypatch, entry_module, _UntouchableAccessor())
    event = RawEvent(
        id="e-tpl",
        merchant_id="m1",
        source="whatsapp",
        topic="template.status",
        schema_version="v23.0",
        external_id="waba:t-1:APPROVED:1",
        payload={"event": "APPROVED"},
        received_at=datetime.now(timezone.utc),
    )
    asyncio.run(entry_module.consume_attributed_event(event, None, {}))
