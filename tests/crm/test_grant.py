"""materialize_call: a workflow call becomes a lead only when the dialler has
granted it a line.

The dialler hands back the id it was given when the run started waiting. The
lead is minted by the call square's own code (one visit of the walker), so
these tests pin what is NEW: the refusals, that a second grant changes
nothing, and that a run which moved on is never left with a dialable lead.

Fakes follow test_workflow_walker.py: one object answers for every accessor.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.grant as grant
import app.crm.outreach.nodes.call as call_node
import app.crm.outreach.walker as walker
import app.database.accessor.breeze_buddy.lead_call_tracker as leads_accessor
from app.crm.outreach.ceiling import today_on
from app.crm.outreach.db.queries.grant import get_runs_query
from app.crm.outreach.grant import Refusal, calls_still_wanted, materialize_call
from app.crm.outreach.schemas import EnrollmentRun
from app.schemas.breeze_buddy.core import LeadCallStatus
from app.utils.transformation import TEMPLATE_FUNCTION_REGISTRY
from tests.crm.doubles import patch_accessors

PLAN: Dict[str, Any] = {
    "entry": {"topic": "checkout.initiated"},
    "nodes": [
        {"id": "call-1", "type": "call", "template_id": "tpl-1"},
        {"id": "after-call-1", "type": "wait", "minutes": 120},
    ],
    "edges": [["call-1", "after-call-1"]],
    "goals": [{"topics": ["order.placed"]}],
}
NUMBER = "number-1"


def _lead_id(run: EnrollmentRun, visit: int = 1) -> str:
    return str(uuid5(NAMESPACE_URL, f"crm-workflow-lead:{run.id}:call-1:{visit}"))


class _World:
    """The run table, the lead table and the plan, behind every accessor."""

    def __init__(self, plan: Optional[Dict[str, Any]] = None) -> None:
        self.definition = plan or PLAN
        self.plan_status: Optional[str] = "live"
        self.goal_met = False
        self.runs: Dict[str, EnrollmentRun] = {}
        self.leads: Dict[str, Any] = {}
        self.moves = 0
        # Runs inside the call square's checks: between materialize's read and
        # its move.
        self.meanwhile: Optional[Callable[[], None]] = None

    def waiting(self, **changes: Any) -> EnrollmentRun:
        now = datetime.now(timezone.utc)
        fields: Dict[str, Any] = {
            "id": uuid4(),
            "merchant_id": "m1",
            "workflow_id": uuid4(),
            "workflow_version": 1,
            "customer_id": uuid4(),
            "status": "waiting",
            "current_node": "call-1",
            "wake_at": now + timedelta(days=30),
            "entered_at": now - timedelta(hours=1),
            "exited_at": None,
            "exit_reason": None,
            "context": {"phone": "+919876543210", "product_name": "vivo S2 5G 8GB"},
            "enrollment_key": "c-1",
            "attempts": 0,
            "last_error": None,
            "node_arrived_at": now - timedelta(minutes=10),
        }
        run = EnrollmentRun(**{**fields, **changes})
        self.runs[str(run.id)] = run
        return run

    def run(self, run: EnrollmentRun) -> EnrollmentRun:
        return self.runs[str(run.id)]

    # --- outreach accessors -------------------------------------------------
    async def get_runs(self, run_ids: List[str]) -> List[EnrollmentRun]:
        return [self.runs[i] for i in run_ids if i in self.runs]

    async def workflow_status(self, merchant_id: str, workflow_id: str) -> Any:
        return self.plan_status

    async def get_definition(self, *args: Any) -> Dict[str, Any]:
        return self.definition

    async def customer_has_event(self, *args: Any, **kwargs: Any) -> bool:
        return self.goal_met

    async def advance_run(
        self,
        run_id: str,
        current_node: str,
        wake_at: datetime,
        context: Dict[str, Any],
        leased_wake_at: datetime,
        node_arrived_at: Optional[datetime] = None,
        steps: Any = None,
    ) -> bool:
        run = self.runs[run_id]
        if run.status != "waiting" or run.wake_at != leased_wake_at:
            return False
        self.moves += 1
        self.runs[run_id] = run.model_copy(
            update={
                "current_node": current_node,
                "wake_at": wake_at,
                "context": context,
            }
        )
        return True

    async def exit_run(
        self,
        run_id: str,
        exit_reason: str,
        leased_wake_at: datetime,
        current_node: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        steps: Any = None,
    ) -> bool:
        run = self.runs[run_id]
        if run.status == "exited" or run.wake_at != leased_wake_at:
            return False
        self.runs[run_id] = run.model_copy(
            update={
                "status": "exited",
                "exit_reason": exit_reason,
                "wake_at": None,
                "context": run.context if context is None else context,
            }
        )
        return True

    async def park_run(self, run_id: str, error: str, leased_wake_at: datetime) -> bool:
        run = self.runs[run_id]
        if run.wake_at != leased_wake_at:
            return False
        self.runs[run_id] = run.model_copy(
            update={"status": "parked", "wake_at": None, "last_error": error}
        )
        return True

    # --- the lead table (data layer) -----------------------------------------
    async def create_lead_call_tracker(self, **row: Any) -> Any:
        if row["id"] in self.leads:
            return None  # the accessor swallows a duplicate key
        self.leads[row["id"]] = SimpleNamespace(**row)
        return self.leads[row["id"]]

    async def get_lead_by_id(self, lead_id: str) -> Any:
        return self.leads.get(lead_id)

    async def get_lead_status(self, lead_id: str) -> Any:
        lead = self.leads.get(lead_id)
        return lead and lead.status


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _install(monkeypatch, _World())


def _install(monkeypatch: pytest.MonkeyPatch, world: _World) -> _World:
    for module in (grant, walker, definitions):
        patch_accessors(monkeypatch, module, world)
    monkeypatch.setattr(definitions, "_definitions", OrderedDict())
    monkeypatch.setattr(walker, "customer_has_event", world.customer_has_event)

    async def template(_id: str) -> Any:
        if world.meanwhile is not None:
            world.meanwhile()
        return SimpleNamespace(
            id="tpl-1", name="nudge", reseller_id="r1", merchant_id=None
        )

    async def config(_id: str) -> Any:
        return SimpleNamespace(initial_offset=0)

    async def stamp(_lead_id: str, _run_id: str) -> bool:
        return True

    monkeypatch.setattr(call_node, "get_template_by_id", template)
    monkeypatch.setattr(call_node, "get_call_execution_config_by_template_id", config)
    monkeypatch.setattr(call_node, "update_lead_enrollment_id", stamp)
    monkeypatch.setattr(call_node, "get_lead_by_id", world.get_lead_by_id)
    for module in (call_node, grant):
        monkeypatch.setattr(module, "get_lead_status", world.get_lead_status)
    monkeypatch.setattr(
        call_node, "create_lead_call_tracker", world.create_lead_call_tracker
    )
    return world


# --- the granted call ---------------------------------------------------------


async def test_a_granted_call_mints_one_backlog_lead_and_moves_the_run(
    world: _World,
) -> None:
    run = world.waiting()
    lead_id = _lead_id(run)

    assert await materialize_call(str(run.id), lead_id, NUMBER) == lead_id

    lead = world.leads[lead_id]
    assert lead.status == LeadCallStatus.BACKLOG
    assert lead.meta_data == {
        "workflow_id": str(run.workflow_id),
        "enrollment_id": str(run.id),
    }
    moved = world.run(run)
    assert moved.current_node == "after-call-1"
    assert moved.context["lead_call-1"] == lead_id
    assert moved.context["lead_visits_call-1"] == 1


async def test_a_second_grant_makes_no_second_lead_and_no_second_move(
    world: _World,
) -> None:
    run = world.waiting()
    lead_id = _lead_id(run)

    first = await materialize_call(str(run.id), lead_id, NUMBER)
    second = await materialize_call(str(run.id), lead_id, NUMBER)

    assert first == second == lead_id
    assert list(world.leads) == [lead_id]
    assert world.moves == 1


async def test_a_lead_left_by_a_crashed_grant_is_adopted(world: _World) -> None:
    run = world.waiting()
    lead_id = _lead_id(run)
    world.leads[lead_id] = SimpleNamespace(id=lead_id, status=LeadCallStatus.BACKLOG)

    assert await materialize_call(str(run.id), lead_id, NUMBER) == lead_id
    assert list(world.leads) == [lead_id]
    assert world.run(run).current_node == "after-call-1"


# --- the refusals ---------------------------------------------------------------


async def test_a_run_standing_on_another_square_is_refused(world: _World) -> None:
    run = world.waiting(current_node="after-call-1")

    assert await materialize_call(str(run.id), _lead_id(run), NUMBER) is Refusal.MOVED
    assert world.leads == {}


async def test_the_id_of_an_earlier_visit_is_refused(world: _World) -> None:
    """The run has placed visit 1 and now waits for visit 2."""
    run = world.waiting(context={"phone": "+919876543210", "lead_visits_call-1": 1})

    assert await materialize_call(str(run.id), _lead_id(run), NUMBER) is Refusal.MOVED
    assert world.leads == {} and world.moves == 0
    assert await materialize_call(str(run.id), _lead_id(run, 2), NUMBER) == _lead_id(
        run, 2
    )


async def test_an_unknown_or_ended_run_is_refused(world: _World) -> None:
    ended = world.waiting(status="exited", exit_reason="goal_met", wake_at=None)

    assert await materialize_call(str(uuid4()), "x", NUMBER) is Refusal.EXITED
    assert (
        await materialize_call(str(ended.id), _lead_id(ended), NUMBER) is Refusal.EXITED
    )


@pytest.mark.parametrize(
    "plan_status, refusal",
    [("paused", Refusal.PAUSED), ("archived", Refusal.EXITED), (None, Refusal.EXITED)],
)
async def test_a_plan_that_is_not_live_places_nothing(
    world: _World, plan_status: Optional[str], refusal: Refusal
) -> None:
    world.plan_status = plan_status
    run = world.waiting()

    assert await materialize_call(str(run.id), _lead_id(run), NUMBER) is refusal
    assert world.leads == {}
    assert world.run(run) == run  # still waiting for its line


async def test_a_run_whose_goal_is_met_exits_instead_of_calling(world: _World) -> None:
    world.goal_met = True
    run = world.waiting()

    assert await materialize_call(str(run.id), _lead_id(run), NUMBER) is Refusal.GOAL
    assert world.leads == {}
    assert world.run(run).status == "exited"


async def test_a_run_past_its_max_age_exits_instead_of_calling(world: _World) -> None:
    run = world.waiting(entered_at=datetime.now(timezone.utc) - timedelta(days=8))

    assert await materialize_call(str(run.id), _lead_id(run), NUMBER) is Refusal.EXITED
    assert world.leads == {}
    assert world.run(run).exit_reason == "timed_out"


async def test_a_run_at_its_daily_ceiling_places_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits = {"max_calls_per_day": 1, "timezone": "Asia/Kolkata"}
    world = _install(monkeypatch, _World({**PLAN, "exits": exits}))
    day = today_on(SimpleNamespace(**exits))
    run = world.waiting(
        context={"phone": "+919876543210", "calls_today": {"day": day, "n": 1}}
    )
    lead_id = _lead_id(run)

    assert await materialize_call(str(run.id), lead_id, NUMBER) is Refusal.CAPPED
    assert world.leads[lead_id].status == LeadCallStatus.FINISHED  # never dialled
    assert world.run(run).current_node == "after-call-1"


async def test_a_capped_grant_whose_move_is_lost_leaves_nothing_dialable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capped lead is inserted before the run moves. When that move is lost
    (the run's alarm moved under it) the next grant finds the id spent, counts
    the visit up and wakes the run: one lead, never dialled, and the run is not
    left waiting under a dead id."""
    exits = {"max_calls_per_day": 1, "timezone": "Asia/Kolkata"}
    world = _install(monkeypatch, _World({**PLAN, "exits": exits}))
    day = today_on(SimpleNamespace(**exits))
    run = world.waiting(
        context={"phone": "+919876543210", "calls_today": {"day": day, "n": 1}}
    )
    lead_id = _lead_id(run)

    def the_alarm_moves() -> None:
        world.meanwhile = None
        later = datetime.now(timezone.utc) + timedelta(days=1)
        world.runs[str(run.id)] = run.model_copy(update={"wake_at": later})

    world.meanwhile = the_alarm_moves

    assert await materialize_call(str(run.id), lead_id, NUMBER) is Refusal.ERROR
    assert await materialize_call(str(run.id), lead_id, NUMBER) is Refusal.MOVED
    assert [lead.status for lead in world.leads.values()] == [LeadCallStatus.FINISHED]
    woken = world.run(run)
    assert (woken.status, woken.current_node) == ("waiting", "call-1")
    assert woken.wake_at is not None and woken.wake_at <= datetime.now(timezone.utc)
    assert woken.context["lead_visits_call-1"] == 1


async def test_a_run_the_call_square_cannot_serve_is_parked(world: _World) -> None:
    run = world.waiting(context={})  # no phone

    assert await materialize_call(str(run.id), _lead_id(run), NUMBER) is Refusal.MOVED
    assert world.leads == {}
    assert world.run(run).status == "parked"


async def test_a_failure_is_an_answer_never_an_exception(world: _World) -> None:
    async def down(_ids: List[str]) -> List[EnrollmentRun]:
        raise ConnectionError("db is down")

    world.get_runs = down  # type: ignore[method-assign]

    assert await materialize_call("run", "lead", NUMBER) is Refusal.ERROR


# --- a run that moves while its lead is being minted ----------------------------


async def test_an_event_between_the_read_and_the_move_leaves_no_lead_row(
    world: _World,
) -> None:
    """The run moves first and the lead is inserted after, so a run an event
    moved meanwhile has no lead at all: nothing to withdraw, nothing to report."""
    run = world.waiting()
    lead_id = _lead_id(run)

    def an_event_moves_the_run() -> None:
        world.runs[str(run.id)] = run.model_copy(
            update={
                "current_node": "after-call-1",
                "wake_at": datetime.now(timezone.utc),
            }
        )

    world.meanwhile = an_event_moves_the_run

    assert await materialize_call(str(run.id), lead_id, NUMBER) is Refusal.MOVED
    assert world.leads == {}
    assert "lead_call-1" not in world.run(run).context


async def test_a_grant_that_died_after_the_move_is_finished_by_the_next_one(
    world: _World,
) -> None:
    """The run moved on carrying the lead id, and the insert never landed. The
    re-sent grant finds the id on the run and mints the lead; a third changes
    nothing."""
    run = world.waiting(
        current_node="after-call-1",
        context={"phone": "+919876543210", "lead_visits_call-1": 1},
    )
    lead_id = _lead_id(run)
    world.runs[str(run.id)] = run = run.model_copy(
        update={"context": {**run.context, "lead_call-1": lead_id}}
    )

    assert await materialize_call(str(run.id), lead_id, NUMBER) == lead_id
    assert world.leads[lead_id].status == LeadCallStatus.BACKLOG
    assert await materialize_call(str(run.id), lead_id, NUMBER) == lead_id
    assert list(world.leads) == [lead_id] and world.moves == 0


@pytest.mark.parametrize("row", [None, LeadCallStatus.BACKLOG])
async def test_a_grant_for_a_call_the_run_has_moved_past_is_refused(
    world: _World, row: Optional[LeadCallStatus]
) -> None:
    """The run placed visit 1, left the wait after it and is back on the call
    square waiting for visit 2. A late grant for visit 1 mints nothing, and a
    lead visit 1 did leave is not handed out as dialable."""
    run = world.waiting(context={"phone": "+919876543210", "lead_visits_call-1": 1})
    stale = _lead_id(run, 1)
    world.runs[str(run.id)] = run = run.model_copy(
        update={"context": {**run.context, "lead_call-1": stale}}
    )
    if row is not None:
        world.leads[stale] = SimpleNamespace(id=stale, status=row)

    assert await materialize_call(str(run.id), stale, NUMBER) is Refusal.MOVED
    assert list(world.leads) == ([] if row is None else [stale])
    assert world.moves == 0


async def test_a_spent_id_is_retired_and_the_run_woken_for_its_next(
    world: _World,
) -> None:
    """The id the run waits under already has a lead that can never be dialled.
    The visit is counted up and the run woken, so the walker queues its next
    id; nothing is placed on the dead one."""
    run = world.waiting()
    dead = _lead_id(run)
    world.leads[dead] = SimpleNamespace(id=dead, status=LeadCallStatus.FINISHED)

    assert await materialize_call(str(run.id), dead, NUMBER) is Refusal.MOVED

    still = world.run(run)
    assert still.current_node == "call-1"
    assert still.wake_at is not None and still.wake_at <= datetime.now(timezone.utc)
    assert "lead_call-1" not in still.context
    assert await calls_still_wanted([(str(run.id), _lead_id(run, 2))]) == {
        _lead_id(run, 2)
    }


# --- the payload ----------------------------------------------------------------

PLAYBOOK_PLAN: Dict[str, Any] = {
    **PLAN,
    "nodes": [{**PLAN["nodes"][0], "blocks": ["hook"]}, PLAN["nodes"][1]],
    "playbook": {
        "lines": {"hook_named": "aapka {product_name} cart me hai"},
        "blocks": {"hook": [{"say": "hook_named"}]},
        "transform": {
            "product_name": {"function": ["llm_call"], "params": {"prompt": "short"}}
        },
    },
}


async def test_a_slow_payload_is_built_from_the_raw_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _install(monkeypatch, _World(PLAYBOOK_PLAN))
    monkeypatch.setattr(grant, "PAYLOAD_BUDGET_SECONDS", 0.05)

    async def slow_llm(value: Any, prompt: str) -> str:
        await asyncio.sleep(5)
        return "S2"

    monkeypatch.setitem(TEMPLATE_FUNCTION_REGISTRY, "llm_call", slow_llm)
    run = world.waiting()
    lead_id = _lead_id(run)

    assert await materialize_call(str(run.id), lead_id, NUMBER) == lead_id
    assert world.leads[lead_id].payload["hook"] == "aapka vivo S2 5G 8GB cart me hai"


async def test_the_payload_is_the_runs_facts_never_its_bookkeeping(
    world: _World,
) -> None:
    run = world.waiting(
        context={
            "phone": "+919876543210",
            "product_name": "vivo S2",
            "lead_call-0": "an-older-lead",
            "calls_today": {"day": "2026-10-06", "n": 0},
            "source_event_id": str(uuid4()),
        }
    )
    lead_id = _lead_id(run)

    assert await materialize_call(str(run.id), lead_id, NUMBER) == lead_id
    assert world.leads[lead_id].payload == {
        "product_name": "vivo S2",
        "current_node": "call-1",
        "customer_mobile_number": "+919876543210",
    }


# --- what the repair jobs ask -------------------------------------------------


async def test_calls_still_wanted_returns_only_the_pairs_still_waiting(
    world: _World,
) -> None:
    waiting = world.waiting()
    moved = world.waiting(current_node="after-call-1")
    ended = world.waiting(status="exited", exit_reason="goal_met", wake_at=None)

    wanted = await calls_still_wanted(
        [
            (str(waiting.id), _lead_id(waiting)),
            (str(waiting.id), _lead_id(waiting, 2)),  # not the visit it waits for
            (str(moved.id), _lead_id(moved)),
            (str(ended.id), _lead_id(ended)),
            (str(uuid4()), "no-such-run"),
        ]
    )

    assert wanted == {_lead_id(waiting)}


def test_runs_are_read_by_id_in_one_statement() -> None:
    sql, params = get_runs_query(["a", "b"])
    assert "id = ANY($1::uuid[])" in sql
    assert params == [["a", "b"]]


async def test_the_lead_status_read_says_nothing_when_there_is_no_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every grant asks about a lead that is not there yet: no ERROR line."""
    rows: List[Dict[str, str]] = []
    said: List[Any] = []

    async def fetch(sql: str, values: List[Any]) -> List[Dict[str, str]]:
        assert 'SELECT "status"' in sql and values == ["lead-1"]
        return rows

    monkeypatch.setattr(leads_accessor, "run_parameterized_query", fetch)
    monkeypatch.setattr(leads_accessor.logger, "error", said.append)

    assert await leads_accessor.get_lead_status("lead-1") is None
    rows.append({"status": "BACKLOG"})
    assert await leads_accessor.get_lead_status("lead-1") is LeadCallStatus.BACKLOG
    assert said == []
