"""The overnight drain (21 Sep 2026; docs/crm/runbooks/overnight-drain.md).

Three things, each pinned here against the real engine with a fake
accessor slice (the shape test_workflow_walker.py uses):

- the LANE: a window that holds a timer makes the run cold; a merchant
  letter makes it hot, and so does queuing its call; our own call report
  leaves it alone; the walker claims hot first and cold only into the
  lines the plan's numbers have free (capacity.py);
- the AWAITING call square: queue, then wait for that call's own report
  before the edge; a merchant letter supersedes the queued call; the
  backstop aborts it; a call that could never be dialled ends the run;
- the ABORT: a run that ends early takes its queued calls with it, never
  a run that completed.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest

import app.crm.outreach.capacity as capacity
import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.progress as progress
import app.crm.outreach.walker as walker
from app.crm.outreach.capacity import Lines
from app.crm.outreach.db.queries.enrollment import claim_due_runs_query
from app.crm.outreach.nodes import awaits, listens
from app.crm.outreach.nodes.call import (
    CALL_COMPLETED,
    EJECT_OUTCOMES,
    NoRoom,
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
from app.crm.outreach.window import held_alarm
from app.crm.record.contracts import RawEvent
from app.database.queries.breeze_buddy.lead_call_tracker import (
    abort_queued_leads_by_enrollment_query,
    count_calls_holding_lines_query,
    insert_lead_if_lines_free_query,
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
    lane: str = "hot",
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
        lane=lane,
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
        self.cold_plans: List[Tuple[str, str]] = []
        self.status = "live"
        # capacity.py's slice: the plan's call templates, their lines and
        # the live count of calls holding them
        self.templates: List[str] = ["t-1"]
        self.lines: Optional[Lines] = Lines("n-1", ("t-1",), 100)
        self.holding: Optional[int] = 0

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

    async def claim_due_runs(
        self,
        limit: int,
        lease: int,
        lane: str = "hot",
        merchant_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
    ) -> List[EnrollmentRun]:
        self.claims.append(
            {
                "limit": limit,
                "lane": lane,
                "merchant_id": merchant_id,
                "wf": workflow_id,
            }
        )
        return [_run(lane=lane) for _ in range(min(limit, 2))]

    async def due_cold_plans(self) -> List[Tuple[str, str]]:
        return self.cold_plans

    async def cancel_queued_calls(self, run_id: str, reason: str) -> None:
        self.aborts.append((run_id, reason))

    async def plan_call_templates(
        self, merchant_id: str, workflow_id: str
    ) -> List[str]:
        return self.templates

    async def lines_for_template(self, template_id: str) -> Optional[Lines]:
        return self.lines

    async def count_calls_holding_lines(self, template_ids: List[str]) -> Optional[int]:
        return self.holding


def _install(monkeypatch: pytest.MonkeyPatch, writes: _Writes) -> None:
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)
    definitions._definitions.clear()
    monkeypatch.setattr(walker, "cancel_queued_calls", writes.cancel_queued_calls)
    patch_accessors(monkeypatch, capacity, writes)
    monkeypatch.setattr(capacity, "plan_call_templates", writes.plan_call_templates)
    monkeypatch.setattr(capacity, "lines_for_template", writes.lines_for_template)
    monkeypatch.setattr(
        capacity, "count_calls_holding_lines", writes.count_calls_holding_lines
    )


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


def test_a_window_that_moves_the_alarm_is_what_makes_a_run_cold() -> None:
    quiet = WorkflowNode.model_validate(_LADDER["nodes"][0])
    at_night = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)  # 21:30 IST
    wake, held = held_alarm(quiet, at_night, at_night + timedelta(days=7))
    assert held and wake == datetime(2026, 9, 22, 3, 30, tzinfo=timezone.utc)
    wake, held = held_alarm(quiet, NOW, NOW + timedelta(days=7))
    assert not held and wake == NOW + timedelta(minutes=15)


# --- the claim: hot first, cold oldest-first into free lines -------------------


def test_the_claim_takes_every_hot_run_then_cold_up_to_the_free_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]
    writes.holding = 90  # 100 lines, 90 held: 10 look free
    runs = asyncio.run(walker.claim_due_runs(50))
    assert [c["lane"] for c in writes.claims] == ["hot", "cold"]
    assert writes.claims[1] == {
        "limit": 10,
        "lane": "cold",
        "merchant_id": "m1",
        "wf": "wf-a",
    }
    assert [r.lane for r in runs] == ["hot", "hot", "cold", "cold"]


def test_cold_is_bounded_by_the_batch_after_hot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]
    asyncio.run(walker.claim_due_runs(10))  # 100 free, but 8 places left
    assert writes.claims[1]["limit"] == 8


def test_full_lines_claim_no_cold_run(monkeypatch: pytest.MonkeyPatch) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]
    writes.holding = 100
    asyncio.run(walker.claim_due_runs(10))
    assert [c["lane"] for c in writes.claims] == ["hot"]


def test_a_blind_count_claims_nothing_cold_rather_than_flood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]
    writes.holding = None
    asyncio.run(walker.claim_due_runs(10))
    assert [c["lane"] for c in writes.claims] == ["hot"]


def test_a_call_template_with_no_number_is_never_claimed_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]
    writes.lines = None
    asyncio.run(walker.claim_due_runs(10))
    assert [c["lane"] for c in writes.claims] == ["hot"]


def test_a_plan_with_no_call_square_is_claimed_cold_freely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]
    writes.templates = []
    writes.holding = None  # nothing to count, nothing asked
    asyncio.run(walker.claim_due_runs(10))
    assert writes.claims[1]["limit"] == 8


def test_a_full_hot_batch_leaves_no_room_for_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]
    asyncio.run(walker.claim_due_runs(2))
    assert [c["lane"] for c in writes.claims] == ["hot"]


def test_a_cold_claim_error_never_loses_the_hot_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    writes.cold_plans = [("m1", "wf-a")]

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(walker, "claim_cold_runs", boom)
    runs = asyncio.run(walker.claim_due_runs(10))
    assert [r.lane for r in runs] == ["hot", "hot"]


def test_cold_runs_are_claimed_oldest_held_first_and_hot_by_alarm() -> None:
    cold, _ = claim_due_runs_query(7, 300, "cold", "m1", "wf-1")
    hot, _ = claim_due_runs_query(7, 300, "hot")
    assert "ORDER BY node_arrived_at, id" in cold
    assert "ORDER BY wake_at, id" in hot


def test_the_lines_count_reads_exactly_what_index_078_covers() -> None:
    text, values = count_calls_holding_lines_query(["t-1", "t-2"])
    assert '"template_id" = ANY($1::uuid[])' in text
    assert '"status" = ANY($2::text[])' in text and "'BACKLOG'" not in text
    assert values == [
        ["t-1", "t-2"],
        [
            LeadCallStatus.BACKLOG.value,
            LeadCallStatus.RETRY.value,
            LeadCallStatus.PROCESSING.value,
        ],
    ]


def test_a_cold_call_is_written_by_one_statement_under_the_line_count() -> None:
    text, values = insert_lead_if_lines_free_query(
        ["t-1", "t-2"],
        100,
        id="lead-1",
        reseller_id="r",
        template="tpl",
        template_id="t-1",
        merchant_id="m1",
        next_attempt_at=NOW,
        payload={"a": 1},
        meta_data={"lane": "cold"},
    )
    assert "VALUES (" not in text
    assert "SELECT $1, $2, $3" in text
    assert "WHERE (" in text and "< $24" in text
    assert '"template_id" = ANY($22::uuid[])' in text
    assert values[-3:] == [
        ["t-1", "t-2"],
        [
            LeadCallStatus.BACKLOG.value,
            LeadCallStatus.RETRY.value,
            LeadCallStatus.PROCESSING.value,
        ],
        100,
    ]


def test_no_line_puts_the_cold_run_back_as_due_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call square found no free line: nothing queued, the token stays
    on the square due NOW (the arrival kept), the squares walked before it
    flush, and the next pass tries again."""
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)

    async def no_room(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        raise NoRoom("no free line on n-1")

    monkeypatch.setitem(
        walker.NODE_TYPES,
        "call",
        walker.NODE_TYPES["call"].__class__(
            validate=walker.NODE_TYPES["call"].validate, execute=no_room, is_wait=False
        ),
    )
    _advance(writes, _run("quiet", lane="cold"))
    (held,) = writes.advances
    assert (held["node"], held["wake"], held.get("lane")) == ("call-1", NOW, None)
    assert [row["node"] for row in held["steps"]] == ["quiet"]  # quiet was left
    assert held["node_arrived_at"] == NOW  # arrived on call-1 as quiet was left
    assert writes.aborts == []


def test_lines_are_resolved_once_per_cache_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: List[str] = []

    async def resolve(template_id: str) -> Optional[Lines]:
        reads.append(template_id)
        return Lines("n-1", ("t-1",), 10)

    monkeypatch.setattr(capacity, "_resolve_lines", resolve)
    capacity.forget_lines()
    asyncio.run(capacity.lines_for_template("t-1"))
    asyncio.run(capacity.lines_for_template("t-1"))
    asyncio.run(capacity.lines_for_template("t-2"))
    assert reads == ["t-1", "t-2"]
    capacity.forget_lines()


def test_a_call_square_waits_only_when_the_plan_says_so() -> None:
    plain = WorkflowNode.model_validate({"id": "c", "type": "call", "template_id": "t"})
    waits = WorkflowNode.model_validate(
        {"id": "c", "type": "call", "template_id": "t", "await": True}
    )
    assert (awaits(plain), awaits(waits)) == (False, True)
    assert plain.topics == [] and waits.topics == [CALL_COMPLETED]


# --- the lane on the walker's writes ------------------------------------------


def test_a_hold_marks_the_run_cold_and_an_open_arrival_keeps_its_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    # 21:30 IST: quiet's timer fired after hours -> held on quiet, cold
    monkeypatch.setattr(
        walker,
        "datetime",
        type(
            "_Night",
            (datetime,),
            {
                "now": classmethod(
                    lambda cls, tz=None: datetime(
                        2026, 9, 21, 16, 0, tzinfo=timezone.utc
                    )
                )
            },
        ),
    )
    _advance(writes, _run("quiet"))
    (hold,) = writes.advances
    assert (hold["node"], hold["lane"]) == ("quiet", "cold")


def test_queuing_the_call_turns_the_run_hot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pile's run reaches call-1 inside the hours: the lead is queued
    and, in the SAME write that starts the wait for its report, the run is
    hot — it holds a line now, and its report and its gap must never wait
    behind the pile."""
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    _advance(writes, _run("quiet", lane="cold"))
    (queued,) = writes.advances
    assert (queued["node"], queued["lane"]) == ("call-1", "hot")


def test_a_call_that_does_not_wait_still_turns_the_run_hot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        **_LADDER,
        "nodes": [
            *(n for n in _LADDER["nodes"] if n["id"] != "call-2"),
            {"id": "call-2", "type": "call", "template_id": "t-1", "await": False},
        ],
    }
    writes = _Writes(plan)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    _advance(writes, _run("call-2", lane="cold"))
    (arrived,) = writes.advances
    assert (arrived["node"], arrived["lane"]) == ("listen", "hot")


def test_the_report_visit_passes_no_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call-1 whose report lands moves to gap INSIDE the hours: the
    arrival passes no lane — the run was made hot when the call was queued,
    and a report changes nothing about that."""
    writes = _Writes(_LADDER)
    _install(monkeypatch, writes)
    _queue(monkeypatch)
    run = _run(
        "call-1",
        {
            awaiting_key("call-1"): "lead-1",
            "lead_call-1": "lead-1",
            "reply_call-1": CALL_COMPLETED,
            "facts": {"call-1": {"outcome": "NO_ANSWER"}},
        },
        lane="cold",
    )
    _advance(writes, run)
    (moved,) = writes.advances
    assert (moved["node"], moved["lane"]) == ("gap", None)


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
    assert entry._lane_for(letter) == "hot"
    assert entry._lane_for(report) is None


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


# --- the progress line --------------------------------------------------------


def test_the_progress_line_counts_placed_and_left() -> None:
    line = progress.progress_line("m1", 40, {"queued": 10, "on_line": 5, "ended": 100})
    assert (line["cold_calls_placed"], line["left"], line["drained"]) == (
        105,
        50,
        False,
    )
    done = progress.progress_line("m1", 0, {"queued": 0, "on_line": 0, "ended": 100})
    assert (done["left"], done["drained"]) == (0, True)
    assert "DRAINED" in progress.slack_text(done)
    assert "50 left" in progress.slack_text(line)


def test_the_drain_starts_when_the_plans_window_last_opened() -> None:
    from app.crm.outreach.window import last_opening

    window = WaitWindow.model_validate(IST)
    # 17:30 IST: the window opened at 09:00 IST today = 03:30Z
    assert last_opening(NOW, window) == datetime(
        2026, 9, 21, 3, 30, tzinfo=timezone.utc
    )
    # 02:00 IST on the 22nd: still the 21st's opening
    night = datetime(2026, 9, 21, 20, 30, tzinfo=timezone.utc)
    assert last_opening(night, window) == datetime(
        2026, 9, 21, 3, 30, tzinfo=timezone.utc
    )
    # two plans: the earlier opening wins; no window: a day back
    early = WaitWindow.model_validate({**IST, "opens": "08:00"})
    assert progress.drain_start(NOW, [window, early]) == last_opening(NOW, early)
    assert progress.drain_start(NOW, []) == NOW - timedelta(days=1)
    assert progress.plan_windows(
        [_LADDER, {"nodes": [{"id": "x", "type": "wait"}]}]
    ) == [
        window,
        window,
    ]


def test_the_line_is_posted_while_there_is_work_and_once_when_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pile: Dict[str, int] = {"m1": 3}
    calls: Dict[str, Dict[str, int]] = {"v": {"queued": 2, "on_line": 1, "ended": 0}}
    posted: List[str] = []
    asked: List[datetime] = []

    class _Reads:
        async def cold_pile_by_merchant(self) -> Dict[str, int]:
            return dict(pile)

        async def live_workflows(self, merchant_id: str) -> List[Workflow]:
            return [_Writes(_LADDER).workflow(merchant_id)]

    async def since(merchant_id: str, at: datetime) -> Dict[str, int]:
        asked.append(at)
        return dict(calls["v"])

    async def post(text: str) -> None:
        posted.append(text)

    patch_accessors(monkeypatch, progress, _Reads())
    monkeypatch.setattr(progress, "cold_calls_since", since)
    monkeypatch.setattr(progress, "_post", post)
    monkeypatch.setattr(progress, "datetime", _Frozen)
    last: Dict[str, int] = {}
    asyncio.run(progress._tick(last))  # 5 left
    pile["m1"], calls["v"] = 0, {"queued": 0, "on_line": 0, "ended": 6}
    asyncio.run(progress._tick(last))  # drained: said once
    asyncio.run(progress._tick(last))  # quiet
    assert len(posted) == 2 and "5 left" in posted[0] and "DRAINED" in posted[1]
    # counted from 09:00 IST, the ladder's window opening, both times
    assert asked == [datetime(2026, 9, 21, 3, 30, tzinfo=timezone.utc)] * 2


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
        "lane": "cold",
        "transfer": {"status": "success"},
        "pre_check_defer_count": 3,
    }
    ours = LeadCallTracker.model_construct(enrollment_id="run", metaData=meta)
    theirs = LeadCallTracker.model_construct(enrollment_id=None, metaData=meta)
    assert _workflow_meta(ours) == {
        "workflow_id": "wf",
        "enrollment_id": "run",
        "lane": "cold",
    }
    assert _workflow_meta(theirs) == {}


def test_the_abort_looks_again_while_the_dialler_holds_a_queued_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.crm.outreach.nodes.call as call

    aborted: List[str] = []
    left = [1, 1, 0]  # locked twice, then free

    async def abort(run_id: str, reason: str) -> List[Any]:
        aborted.append(reason)
        return []

    async def count(run_id: str) -> Optional[int]:
        return left.pop(0)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(call, "abort_queued_leads_by_enrollment", abort)
    monkeypatch.setattr(call, "count_queued_leads_by_enrollment", count)
    monkeypatch.setattr(call.asyncio, "sleep", no_sleep)
    asyncio.run(call.cancel_queued_calls("run-1", "superseded by OFFERED"))
    assert len(aborted) == 3  # the first try, then two more looks
