"""The morning offset (22 Sep 2026; docs/crm/runbooks/morning-offset.md).

Three things, each pinned here against the real engine with a fake
accessor slice (the shape test_workflow_walker.py uses):

- the OFFSET: a timer set inside a window's reserved minutes after the
  opening runs the offset longer, so the runs the window held overnight
  reach the dialler first and the day's live customers queue behind them,
  not beside them;
- the AWAITING call square (`"await": true`): queue, then wait for that
  call's own report before the edge; a merchant letter supersedes the
  queued call; the backstop aborts it; a customer that cannot be called
  ends the run;
- the ABORT: a run that ends early takes its queued calls with it, never
  a run that completed.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.walker as walker
from app.crm.outreach.db.queries.enrollment import resume_run_by_id_query
from app.crm.outreach.nodes import awaits, listens
from app.crm.outreach.nodes.call import (
    CALL_COMPLETED,
    EJECT_OUTCOMES,
    awaiting_key,
    report_outcome,
)
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    EnrollmentRun,
    WaitWindow,
    Workflow,
    WorkflowDefinition,
    WorkflowNode,
)
from app.crm.outreach.window import alarm, in_reserved_period
from app.crm.record.contracts import RawEvent
from app.database.queries.breeze_buddy.lead_call_tracker import (
    abort_queued_leads_by_enrollment_query,
)
from app.schemas import LeadCallStatus
from tests.crm.doubles import patch_accessors

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)  # 17:30 IST
LEASE = NOW + timedelta(seconds=300)
IST = {"opens": "09:00", "closes": "21:00", "timezone": "Asia/Kolkata"}


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: Optional[timezone] = None) -> datetime:  # type: ignore[override]
        return NOW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(walker, "datetime", _Frozen)


@pytest.fixture(autouse=True)
def no_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(walker, "customer_has_event", never)


# The ladder: rule -> quiet -> call-1 (awaits) -> gap -> call-2 -> listen.
_LADDER: Dict[str, Any] = {
    "entry": {"topic": "OFFERED", "key": "customer_id"},
    "nodes": [
        {"id": "quiet", "type": "wait", "minutes": 15, "window": IST},
        {
            "id": "call-1",
            "type": "call",
            "template_id": "t-1",
            "await": True,
            "await_minutes": 60,
            "topics": ["OFFERED"],
        },
        {"id": "gap", "type": "wait", "minutes": 30, "window": IST},
        {"id": "call-2", "type": "call", "template_id": "t-1", "await": True},
        {"id": "listen", "type": "wait", "topics": ["OFFERED"], "key": "$topic"},
    ],
    "edges": [
        ["quiet", "call-1"],
        ["call-1", "gap"],
        ["call-1", "quiet", "OFFERED"],
        ["gap", "call-2"],
        ["call-2", "listen"],
        ["listen", "quiet", "OFFERED"],
    ],
    "goal": {"topics": ["GRANTED"]},
}


def _run(
    node: str = "quiet",
    context: Optional[Dict[str, Any]] = None,
) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node=node,
        wake_at=LEASE,
        entered_at=NOW - timedelta(hours=1),
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919876543210", **(context or {})},
        enrollment_key="c-1",
        attempts=1,
        last_error=None,
        node_arrived_at=NOW - timedelta(minutes=20),
    )


class _Writes:
    """The walker's accessor slice, recording every write by name."""

    def __init__(self, definition: Dict[str, Any], matched: bool = True) -> None:
        self.definition = definition
        self.matched = matched
        self.advances: List[Dict[str, Any]] = []
        self.exits: List[Dict[str, Any]] = []
        self.aborts: List[Tuple[str, str]] = []
        self.claims: List[Dict[str, Any]] = []
        self.status = "live"

    def workflow(self, merchant_id: str) -> Workflow:
        return Workflow(
            id=uuid4(),
            merchant_id=merchant_id,
            name="plan",
            status=self.status,
            version=1,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            definition=self.definition,
            draft=None,
        )

    async def get_workflow(self, merchant_id: str, workflow_id: str) -> Workflow:
        return Workflow(
            id=uuid4(),
            merchant_id=merchant_id,
            name="plan",
            status=self.status,
            version=1,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            definition=self.definition,
            draft=None,
        )

    async def get_definition(
        self, merchant_id: str, workflow_id: str, version: int
    ) -> Optional[Dict[str, Any]]:
        return self.definition

    async def advance_run(self, *args: Any, **kwargs: Any) -> bool:
        run_id, node, wake, context, lease = args
        self.advances.append({"node": node, "wake": wake, "context": context, **kwargs})
        return self.matched

    async def exit_run(self, *args: Any, **kwargs: Any) -> bool:
        run_id, reason, lease = args
        self.exits.append({"reason": reason, **kwargs})
        return self.matched

    async def park_run(self, *args: Any) -> bool:
        return self.matched

    async def record_run_error(self, *args: Any) -> bool:
        return self.matched

    async def cancel_queued_calls(self, run_id: str, reason: str) -> None:
        self.aborts.append((run_id, reason))


def _install(monkeypatch: pytest.MonkeyPatch, writes: _Writes) -> None:
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)
    definitions._definitions.clear()
    monkeypatch.setattr(walker, "cancel_queued_calls", writes.cancel_queued_calls)


def _queue(monkeypatch: pytest.MonkeyPatch, lead: str = "lead-1") -> List[str]:
    """The call square's execute, faked: it queues `lead` and, as the real
    one does, names it as the lead the square now awaits."""
    fired: List[str] = []

    async def execute(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        fired.append(node.id)
        return {
            f"lead_{node.id}": lead,
            f"lead_visits_{node.id}": 1,
            awaiting_key(node.id): lead,
        }

    monkeypatch.setitem(
        walker.NODE_TYPES,
        "call",
        walker.NODE_TYPES["call"].__class__(
            validate=walker.NODE_TYPES["call"].validate, execute=execute, is_wait=False
        ),
    )
    return fired


def _advance(writes: _Writes, run: EnrollmentRun) -> None:
    definition = WorkflowDefinition.model_validate(writes.definition)
    asyncio.run(walker._advance(run, definition, LEASE))


# --- the words ---------------------------------------------------------------


def test_an_awaiting_call_hears_its_report_first_and_branches_on_the_name() -> None:
    node = WorkflowNode.model_validate(
        {
            "id": "c",
            "type": "call",
            "template_id": "t",
            "await": True,
            "topics": ["OFFERED"],
        }
    )
    assert awaits(node) and listens(node)
    assert node.topics == [CALL_COMPLETED, "OFFERED"] and node.key == "$topic"
    off = WorkflowNode.model_validate({"id": "c", "type": "call", "await": False})
    assert not awaits(off) and not listens(off) and off.topics == []


def test_the_ladder_publishes_and_the_old_shapes_are_refused() -> None:
    assert validate_definition(_LADDER) == []
    two_plain = {**_LADDER, "edges": [*_LADDER["edges"], ["call-1", "listen"]]}
    assert any("exactly one plain edge" in p for p in validate_definition(two_plain))
    unlisted = {**_LADDER, "edges": [*_LADDER["edges"], ["call-2", "quiet", "KYC"]]}
    assert any("does not list" in p for p in validate_definition(unlisted))
    deaf_with_topics = {
        **_LADDER,
        "nodes": [
            *_LADDER["nodes"][:1],
            {**_LADDER["nodes"][1], "await": False},
            *_LADDER["nodes"][2:],
        ],
    }
    assert any(
        "only an awaiting call" in p for p in validate_definition(deaf_with_topics)
    )


# --- the offset: the window's reserved minutes -------------------------------


def _window(offset: int) -> WaitWindow:
    return WaitWindow.model_validate({**IST, "offset_minutes": offset})


def _wait(minutes: int, offset: int) -> WorkflowNode:
    return WorkflowNode.model_validate(
        {
            "id": "quiet",
            "type": "wait",
            "minutes": minutes,
            "window": {**IST, "offset_minutes": offset},
        }
    )


def _ist(hh: int, mm: int, day: int = 22) -> datetime:
    return datetime(2026, 9, day, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(
        timezone.utc
    )


def test_the_reserved_period_is_the_offset_after_the_opening() -> None:
    window = _window(60)
    assert in_reserved_period(_ist(9, 0), window)
    assert in_reserved_period(_ist(9, 59), window)
    assert not in_reserved_period(_ist(10, 0), window)
    assert not in_reserved_period(_ist(8, 59), window)
    assert not in_reserved_period(_ist(9, 30), _window(0))  # no offset


def test_a_timer_set_in_the_reserved_period_runs_the_offset_longer() -> None:
    end = _ist(9, 0, 30)
    # Ravi enters at 09:20; quiet 15 would end 09:35; the offset makes it 10:35.
    assert alarm(_wait(15, 60), _ist(9, 20), end) == _ist(10, 35)
    # A held run's call ended 09:12; its gap 30 ends 09:42 -> 10:42.
    assert alarm(_wait(30, 60), _ist(9, 12), end) == _ist(10, 42)
    # After 10:00 nothing is added.
    assert alarm(_wait(15, 60), _ist(10, 5), end) == _ist(10, 20)
    # Without an offset, 09:20 + 15 is 09:35 as always.
    assert alarm(_wait(15, 0), _ist(9, 20), end) == _ist(9, 35)


def test_the_hold_still_applies_after_the_offset() -> None:
    # 09:30 + (11 h + 60 min offset) lands past 21:00: held to tomorrow's opening.
    assert alarm(_wait(11 * 60, 60), _ist(9, 30), _ist(9, 0, 30)) == _ist(9, 0, 23)


def test_the_night_pile_still_wakes_at_the_opening() -> None:
    # Set at 23:00 the night before, outside the reserved period: no offset,
    # the window's hold alone -> 09:00, the pile.
    assert alarm(_wait(15, 60), _ist(23, 0, 21), _ist(9, 0, 30)) == _ist(9, 0, 22)


def test_a_call_square_waits_only_when_the_plan_says_so() -> None:
    plain = WorkflowNode.model_validate({"id": "c", "type": "call", "template_id": "t"})
    waits = WorkflowNode.model_validate(
        {"id": "c", "type": "call", "template_id": "t", "await": True}
    )
    assert (awaits(plain), awaits(waits)) == (False, True)
    assert plain.topics == [] and waits.topics == [CALL_COMPLETED]


# --- the awaiting call square -------------------------------------------------


def test_the_call_square_queues_then_waits_in_the_same_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    fired = _queue(monkeypatch)
    _advance(writes, _run("quiet"))  # quiet is due at 17:30 IST -> call-1
    assert fired == ["call-1"]
    (wrote,) = writes.advances
    assert wrote["node"] == "call-1"  # the token STAYS on the square
    assert wrote["wake"] == NOW + timedelta(minutes=60)  # its backstop
    assert wrote["context"][awaiting_key("call-1")] == "lead-1"
    assert wrote["context"]["lead_call-1"] == "lead-1"
    # quiet closed in this write; call-1 has not been left, so no row for it
    assert [s["node"] for s in wrote["steps"]] == ["quiet"]
    assert writes.aborts == []


def test_a_visit_that_finds_the_square_waiting_never_queues_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker on the run is what says "queued, waiting": a claim that
    finds it (a lease retry after a crash, or the alarm itself) resolves
    the wait — it never runs the square's execute a second time."""
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    fired = _queue(monkeypatch)
    run = _run("call-1", {awaiting_key("call-1"): "lead-1", "lead_call-1": "lead-1"})
    _advance(writes, run)
    assert fired == []
    # a claim with no reply IS the backstop: the walker only hands out due runs
    assert writes.aborts == [(str(run.id), "call call-1 not placed within 60 minutes")]
    (moved,) = writes.advances
    assert moved["node"] == "gap"


def test_the_report_moves_the_square_on_with_its_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    _advance(
        writes,
        _run(
            "call-1",
            {
                awaiting_key("call-1"): "lead-1",
                "lead_call-1": "lead-1",
                "reply_call-1": CALL_COMPLETED,
                "facts": {"call-1": {"outcome": "NO_ANSWER"}},
            },
        ),
    )
    (moved,) = writes.advances
    assert moved["node"] == "gap"
    (row,) = moved["steps"]
    assert (row["node"], row["outcome"], row["dispatch_id"]) == (
        "call-1",
        "NO_ANSWER",
        "lead-1",
    )
    assert awaiting_key("call-1") not in moved["context"]
    assert "reply_call-1" not in moved["context"]
    assert writes.aborts == []  # the call happened; nothing to abort


def test_a_merchant_letter_supersedes_the_queued_call_and_re_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run(
        "call-1",
        {
            awaiting_key("call-1"): "lead-1",
            "lead_call-1": "lead-1",
            "reply_call-1": "OFFERED",
        },
    )
    _advance(writes, run)
    assert writes.aborts == [(str(run.id), "superseded by OFFERED")]
    # the OFFERED arrow leads back to quiet, which is due inside the hours
    # only after its 15 minutes: the run is armed on quiet
    (moved,) = writes.advances
    assert moved["node"] == "quiet"
    (row,) = moved["steps"]
    assert (row["node"], row["outcome"], row["next_node"]) == (
        "call-1",
        "OFFERED",
        "quiet",
    )


def test_the_backstop_aborts_the_unplaced_call_and_takes_the_plain_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run("call-1", {awaiting_key("call-1"): "lead-1", "lead_call-1": "lead-1"})
    _advance(writes, run)
    assert writes.aborts == [(str(run.id), "call call-1 not placed within 60 minutes")]
    (moved,) = writes.advances
    assert moved["node"] == "gap"
    (row,) = moved["steps"]
    assert row["outcome"] == "timeout"


@pytest.mark.parametrize("outcome", sorted(EJECT_OUTCOMES))
def test_a_call_that_could_never_be_dialled_ends_the_run(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run(
        "call-1",
        {
            awaiting_key("call-1"): "lead-1",
            "lead_call-1": "lead-1",
            "reply_call-1": CALL_COMPLETED,
            "facts": {"call-1": {"outcome": outcome}},
        },
    )
    _advance(writes, run)
    assert writes.advances == []
    (ended,) = writes.exits
    assert ended["reason"] == "ejected"
    (row,) = ended["steps"]
    assert (row["node"], row["outcome"], row["next_node"]) == ("call-1", outcome, None)
    assert awaiting_key("call-1") not in ended["context"]
    assert writes.aborts == [(str(run.id), "run exited: ejected")]


@pytest.mark.parametrize(
    "outcome", ["UNKNOWN", "NUMBER_UNAVAILABLE", "NO_CONFIG", "PRECHECK_FAILED"]
)
def test_a_failure_that_is_ours_takes_the_plain_edge_like_a_no_answer(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run(
        "call-1",
        {
            awaiting_key("call-1"): "lead-1",
            "lead_call-1": "lead-1",
            "reply_call-1": CALL_COMPLETED,
            "facts": {"call-1": {"outcome": outcome}},
        },
    )
    _advance(writes, run)
    assert writes.exits == []
    (moved,) = writes.advances
    assert moved["node"] == "gap"
    (row,) = moved["steps"]
    assert (row["node"], row["outcome"], row["next_node"]) == ("call-1", outcome, "gap")


def test_report_outcome_reads_only_this_squares_report() -> None:
    assert (
        report_outcome({"facts": {"call-1": {"outcome": "BUSY"}}}, "call-1") == "BUSY"
    )
    assert report_outcome({"facts": {"call-2": {"outcome": "BUSY"}}}, "call-1") is None
    assert report_outcome({"facts": "legacy"}, "call-1") is None
    assert report_outcome({}, "call-1") is None


# --- a run that ends early takes its queued calls with it ---------------------


def test_timed_out_and_ejected_abort_queued_calls_and_completed_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    old = _run("quiet")
    old.entered_at = NOW - timedelta(days=8)
    _advance(writes, old)
    assert writes.aborts == [(str(old.id), "run exited: timed_out")]

    writes = _Writes(_LADDER)
    writes.status = "archived"
    _install(monkeypatch, writes)
    gone = _run("quiet")
    asyncio.run(walker.walk_run(gone))
    assert writes.aborts == [(str(gone.id), "run exited: ejected")]

    # a plan whose last square is a call: completed, and the call stands
    quiet, call = _LADDER["nodes"][0], _LADDER["nodes"][1]
    last_call: Dict[str, Any] = {
        **_LADDER,
        "nodes": [quiet, {**call, "await": False, "topics": []}],
        "edges": [["quiet", "call-1"]],
    }
    writes = _Writes(last_call)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    _advance(writes, _run("quiet"))
    assert [e["reason"] for e in writes.exits] == ["completed"]
    assert writes.aborts == []


def test_an_exit_the_lease_refused_aborts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER, matched=False)
    _install(monkeypatch, writes)
    old = _run("quiet")
    old.entered_at = NOW - timedelta(days=8)
    _advance(writes, old)
    assert writes.aborts == []


# --- the consumer: the report is matched on the lead; a letter flips hot ------


def _event(topic: str, payload: Dict[str, Any], source: str = "flipkart") -> RawEvent:
    return RawEvent(
        id="ev-1",
        merchant_id="m1",
        source=source,
        topic=topic,
        schema_version="1",
        external_id=f"{topic}:ev-1",
        payload=payload,
        received_at=NOW,
        occurred_at=NOW,
    )


def test_a_merchant_letter_on_the_call_square_is_judged_by_match() -> None:
    """Two applications on one phone: the square's `match` keeps the other
    application's letter from aborting this run's call (the cross-over
    guard the plans' listening waits already carry)."""
    node = WorkflowNode.model_validate(
        {
            "id": "c",
            "type": "call",
            "template_id": "t",
            "await": True,
            "topics": ["OFFERED"],
            "match": {"run": "customer_id", "payload": "customer_id"},
        }
    )
    run = _run("c", {"customer_id": "app-A", awaiting_key("c"): "lead-1"})
    mine = _event("OFFERED", {"customer_id": "app-A"})
    theirs = _event("OFFERED", {"customer_id": "app-B"})
    assert entry._is_about(node, mine, run) is True
    assert entry._is_about(node, theirs, run) is False
    # the square's own report still answers to the lead id, not to match
    report = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    assert entry._is_about(node, report, run) is True


def test_the_report_is_about_the_run_only_when_it_names_the_awaited_lead() -> None:
    node = WorkflowNode.model_validate(_LADDER["nodes"][1])
    run = _run("call-1", {awaiting_key("call-1"): "lead-1"})
    mine = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    late = _event(CALL_COMPLETED, {"lead_id": "lead-0"}, source="telephony")
    nobody = _event(CALL_COMPLETED, {"outcome": "BUSY"}, source="telephony")
    assert entry._is_about(node, mine, run)
    assert not entry._is_about(node, late, run)
    assert not entry._is_about(node, nobody, run)
    # its merchant topics are judged as any square's: no match word = hers
    assert entry._is_about(node, _event("OFFERED", {"customer_id": "c-1"}), run)


def test_a_call_report_answers_the_square_but_never_takes_the_latest_letter() -> None:
    node = WorkflowNode.model_validate(_LADDER["nodes"][1])
    report = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    assert entry._reply_patch(node, report, CALL_COMPLETED) == {
        "reply_call-1": CALL_COMPLETED,
        "cut_short_by": "ev-1",
    }
    letter = _event("OFFERED", {"customer_id": "c-1"})
    assert entry._reply_patch(node, letter, "OFFERED") == {
        "reply_call-1": "OFFERED",
        "cut_short_by": "ev-1",
        "latest_letter": "call-1",
    }


# --- the lead store: the abort and the count are what the runbook says --------


def test_the_abort_names_only_the_runs_queued_undialled_leads() -> None:
    text, values = abort_queued_leads_by_enrollment_query(
        "run-1", "run exited: goal_met"
    )
    assert '"enrollment_id" = $4' in text
    assert '"status" IN ($5, $6)' in text
    assert '"is_locked" = FALSE' in text  # the dialler holds it: already dialling
    assert "run-1" not in text
    status, outcome, meta, run_id, backlog, retry = values
    assert (status, outcome, run_id) == (
        LeadCallStatus.FINISHED.value,
        "ABORT",
        "run-1",
    )
    assert (backlog, retry) == (
        LeadCallStatus.BACKLOG.value,
        LeadCallStatus.RETRY.value,
    )
    assert '"abort_reason": "run exited: goal_met"' in meta


# --- two writers on one square: the report yields, `else` never takes it ------


def test_the_report_and_the_backstop_take_the_plain_edge_before_else() -> None:
    """An `else` arrow on an awaiting call answers listed topics the author
    gave no arrow of their own — never the call. Before this law the `else`
    scan ran first, so on any plan with `else` the report and the backstop
    both left by `else` and the plain edge was dead."""
    node = WorkflowNode.model_validate(_LADDER["nodes"][1])
    arrows: List[Tuple[str, Optional[str]]] = [("gap", None), ("quiet", "else")]
    with_else = {**_LADDER, "edges": [*_LADDER["edges"], ["call-1", "quiet", "else"]]}
    assert validate_definition(with_else) == []  # the author may draw it
    assert walker.pick_next(node, arrows, {"reply_call-1": CALL_COMPLETED}) == "gap"
    assert walker.pick_next(node, arrows, {}) == "gap"  # the backstop
    assert walker.pick_next(node, arrows, {"reply_call-1": "OFFERED"}) == "quiet"
    labelled = [("quiet", "OFFERED"), *arrows]
    assert walker.pick_next(node, labelled, {"reply_call-1": "OFFERED"}) == "quiet"
    assert walker.pick_next(node, labelled, {"reply_call-1": CALL_COMPLETED}) == "gap"


def test_only_the_awaiting_calls_own_report_yields_to_a_letter_on_the_square() -> None:
    call = WorkflowNode.model_validate(_LADDER["nodes"][1])
    listen = WorkflowNode.model_validate(_LADDER["nodes"][4])
    report = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    letter = _event("OFFERED", {"customer_id": "c-1"})
    assert entry._yields_to(call, report) == "reply_call-1"
    assert entry._yields_to(call, letter) is None  # the latest word replaces
    assert entry._yields_to(listen, letter) is None


def test_the_resume_decides_the_yield_in_the_statement() -> None:
    """A letter heard while the call rang (a ringing lead is not aborted)
    answered the square; the report landing behind it before the walker's
    pass must not replace that answer and those facts. In SQL, so the two
    writers never race."""
    sql, params = resume_run_by_id_query(
        "m1", "run-1", "call-1", {"reply_call-1": CALL_COMPLETED}, {}, "reply_call-1"
    )
    assert "AND ($6::text IS NULL OR NOT (context ? $6::text))" in sql
    assert params[5] == "reply_call-1"
    sql, params = resume_run_by_id_query("m1", "run-1", "call-1", {"reply_call-1": "X"})
    assert params[5] is None  # a merchant letter: unconditional, as before


# --- the dialler side ---------------------------------------------------------


def test_a_retry_inherits_only_the_workflow_keys_and_only_for_a_runs_lead() -> None:
    # The dialler package has an import cycle that its worker resolves first;
    # load it the way the dialler pod does.
    import app.ai.voice.agents.breeze_buddy.dispatch.worker  # noqa: F401
    from app.ai.voice.agents.breeze_buddy.managers.calls import _workflow_meta
    from app.schemas.breeze_buddy.core import LeadCallTracker

    meta = {
        "workflow_id": "wf",
        "enrollment_id": "run",
        "transfer": {"status": "success"},
        "pre_check_defer_count": 3,
    }
    ours = LeadCallTracker.model_construct(enrollment_id="run", metaData=meta)
    theirs = LeadCallTracker.model_construct(enrollment_id=None, metaData=meta)
    assert _workflow_meta(ours) == {"workflow_id": "wf", "enrollment_id": "run"}
    assert _workflow_meta(theirs) == {}
