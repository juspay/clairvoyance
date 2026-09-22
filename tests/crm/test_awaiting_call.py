"""The call square that waits for its call (22 Sep 2026;
docs/crm/runbooks/awaiting-call.md).

`"event_name": "call.completed"` on a call square: queue the lead, then
hold the token on the square until that call's own report lands (matched
on the lead id it queued) or the backstop fires; then the one plain edge,
with the call's outcome on the step. Pinned here against the real engine
with a fake accessor slice (the shape test_workflow_walker.py uses).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.walker as walker
from app.crm.outreach.nodes import awaits, listens
from app.crm.outreach.nodes.call import CALL_COMPLETED, awaiting_key, report_outcome
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
    WorkflowNode,
)
from app.crm.record.contracts import RawEvent
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


# The ladder: quiet -> call-1 (waits for its report) -> gap -> call-2 -> listen.
_LADDER: Dict[str, Any] = {
    "entry": {"topic": "OFFERED", "key": "customer_id"},
    "nodes": [
        {"id": "quiet", "type": "wait", "minutes": 15, "window": IST},
        {
            "id": "call-1",
            "type": "call",
            "template_id": "t-1",
            "event_name": "call.completed",
            "await_minutes": 60,
        },
        {"id": "gap", "type": "wait", "minutes": 30, "window": IST},
        {
            "id": "call-2",
            "type": "call",
            "template_id": "t-1",
            "event_name": "call.completed",
        },
        {"id": "listen", "type": "wait", "topics": ["OFFERED"], "key": "$topic"},
    ],
    "edges": [
        ["quiet", "call-1"],
        ["call-1", "gap"],
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
        self.status = "live"

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
    one does, names it as the lead the square now waits for."""
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


# --- the word ----------------------------------------------------------------


def test_a_call_square_waits_only_when_the_plan_names_the_event() -> None:
    plain = WorkflowNode.model_validate({"id": "c", "type": "call", "template_id": "t"})
    waits = WorkflowNode.model_validate(
        {"id": "c", "type": "call", "template_id": "t", "event_name": "call.completed"}
    )
    assert (awaits(plain), awaits(waits)) == (False, True)
    assert (listens(plain), listens(waits)) == (False, True)
    assert plain.topics == [] and waits.topics == [CALL_COMPLETED]
    assert waits.key == "$topic" and waits.await_minutes == 1440


def test_the_ladder_publishes_and_the_other_shapes_are_refused() -> None:
    assert validate_definition(_LADDER) == []
    call = _LADDER["nodes"][1]

    def with_call(**words: Any) -> Dict[str, Any]:
        return {
            **_LADDER,
            "nodes": [_LADDER["nodes"][0], {**call, **words}, *_LADDER["nodes"][2:]],
        }

    two_plain = {**_LADDER, "edges": [*_LADDER["edges"], ["call-1", "listen"]]}
    assert any("exactly one plain edge" in p for p in validate_definition(two_plain))
    labelled = {**_LADDER, "edges": [*_LADDER["edges"], ["call-1", "quiet", "OFFERED"]]}
    assert any("does not list" in p for p in validate_definition(labelled))
    other_event = with_call(event_name="OFFERED")
    assert any("own report" in p for p in validate_definition(other_event))
    matched = with_call(match={"run": "customer_id", "payload": "customer_id"})
    assert validate_definition(matched) == []  # judges the topics it lists
    deaf_with_topics = with_call(event_name=None, topics=["OFFERED"], key="$topic")
    assert any(
        "only a call that waits" in p for p in validate_definition(deaf_with_topics)
    )


# --- the waiting call square -------------------------------------------------


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


def test_the_backstop_takes_the_plain_edge_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run("call-1", {awaiting_key("call-1"): "lead-1", "lead_call-1": "lead-1"})
    _advance(writes, run)
    (moved,) = writes.advances
    assert moved["node"] == "gap"
    (row,) = moved["steps"]
    assert row["outcome"] == "timeout"
    assert writes.aborts == [(str(run.id), "call call-1 not placed within 60 minutes")]


def test_a_report_without_an_outcome_still_moves_the_square_on(
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
            "reply_call-1": CALL_COMPLETED,
        },
    )
    _advance(writes, run)
    (moved,) = writes.advances
    (row,) = moved["steps"]
    assert (row["node"], row["outcome"], row["next_node"]) == (
        "call-1",
        CALL_COMPLETED,
        "gap",
    )


def test_a_plain_call_square_still_queues_and_moves_on_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet, call = _LADDER["nodes"][0], _LADDER["nodes"][1]
    old_shape: Dict[str, Any] = {
        **_LADDER,
        "nodes": [quiet, {**call, "event_name": None}, *_LADDER["nodes"][2:]],
    }
    writes = _Writes(old_shape)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    _advance(writes, _run("quiet"))
    (moved,) = writes.advances
    assert moved["node"] == "gap"  # queued and gone, as before
    assert [s["node"] for s in moved["steps"]] == ["quiet", "call-1"]


def test_report_outcome_reads_only_this_squares_report() -> None:
    assert (
        report_outcome({"facts": {"call-1": {"outcome": "BUSY"}}}, "call-1") == "BUSY"
    )
    assert report_outcome({"facts": {"call-2": {"outcome": "BUSY"}}}, "call-1") is None
    assert report_outcome({"facts": "legacy"}, "call-1") is None
    assert report_outcome({}, "call-1") is None


# --- the consumer: the report is matched on the lead ------------------------


def test_the_report_is_about_the_run_only_when_it_names_the_awaited_lead() -> None:
    node = WorkflowNode.model_validate(_LADDER["nodes"][1])
    run = _run("call-1", {awaiting_key("call-1"): "lead-1"})
    mine = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    late = _event(CALL_COMPLETED, {"lead_id": "lead-0"}, source="telephony")
    nobody = _event(CALL_COMPLETED, {"outcome": "BUSY"}, source="telephony")
    assert entry._is_about(node, mine, run)
    assert not entry._is_about(node, late, run)
    assert not entry._is_about(node, nobody, run)
    # a square that waits for nothing claims no report
    moved_on = _run("call-1", {})
    assert not entry._is_about(node, mine, moved_on)


def test_a_call_report_answers_the_square_but_never_takes_the_latest_letter() -> None:
    node = WorkflowNode.model_validate(_LADDER["nodes"][1])
    report = _event(CALL_COMPLETED, {"lead_id": "lead-1"}, source="telephony")
    assert entry._reply_patch(node, report, CALL_COMPLETED) == {
        "reply_call-1": CALL_COMPLETED,
        "cut_short_by": "ev-1",
    }
    listen = WorkflowNode.model_validate(_LADDER["nodes"][4])
    letter = _event("OFFERED", {"customer_id": "c-1"})
    assert entry._reply_patch(listen, letter, "OFFERED") == {
        "reply_listen": "OFFERED",
        "cut_short_by": "ev-1",
        "latest_letter": "listen",
    }
