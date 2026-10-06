"""A call square that lists topics waits on itself for a line.

The dialler queues the id the square WOULD mint and asks grant.py when it
holds a line; until then the run stands on the call square, listening. A
listed letter takes its own arrow and the ask is taken back; the unlabelled
arrow is taken only when the call is placed. A call square with no topics, or
a call no line queue takes, is exactly as it was.

The walker / lead fakes are test_grant.py's; the consumer's are
test_stage_memory.py's.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.nodes.call as call_node
import app.crm.outreach.walker as walker
from app.core import call_queue
from app.core.call_queue import CallRequest
from app.crm.outreach.grant import Refusal, materialize_call
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import EnrollmentRun
from app.schemas.breeze_buddy.core import LeadCallStatus
from tests.crm.test_call_priority import PRIORITY
from tests.crm.test_grant import _install, _lead_id, _World
from tests.crm.test_stage_memory import (  # noqa: F401  (a fixture)
    KYC_AT,
    _consume,
    _event,
    world as letters,
)

PLAN: Dict[str, Any] = {
    "entry": {"topic": "LINE_OFFERED", "reenter": True, "cooldown_hours": 0},
    "nodes": [
        {
            "id": "quiet",
            "type": "wait",
            "topics": ["LINE_OFFERED"],
            "key": "$topic",
            "minutes": 15,
        },
        {
            "id": "call-1",
            "type": "call",
            "template_id": "tpl-1",
            "topics": ["LINE_KYC_COMPLETED", "LINE_INITIATED"],
            "key": "$topic",
        },
        {"id": "after-call-1", "type": "wait", "minutes": 120},
        {"id": "listen", "type": "wait", "minutes": 60},
    ],
    "edges": [
        ["quiet", "call-1", "else"],
        ["call-1", "after-call-1"],
        ["call-1", "quiet", "LINE_KYC_COMPLETED"],
        ["call-1", "listen", "LINE_INITIATED"],
    ],
    "goal": {"topics": ["LINE_ACTIVE"]},
    "priority": PRIORITY,
}


class _Queue:
    """The dialler's three hooks, recorded."""

    def __init__(self, answer: Optional[bool] = True) -> None:
        self.answer = answer
        self.asked: List[CallRequest] = []
        self.withdrawn: List[Any] = []
        self.reranked: List[Any] = []

    async def queue(self, request: CallRequest) -> Optional[bool]:
        self.asked.append(request)
        return self.answer

    async def withdraw(self, template_id: str, lead_id: str) -> None:
        self.withdrawn.append((template_id, lead_id))

    async def rerank(self, *args: Any) -> None:
        self.reranked.append(args)


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _Queue:
    q = _Queue()
    monkeypatch.setattr(call_queue, "_hooks", (q.queue, q.withdraw, q.rerank))
    return q


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _install(monkeypatch, _World(PLAN))


async def _visit(world: _World, run: EnrollmentRun) -> EnrollmentRun:
    """One walker visit of the run as it stands; returns it as left."""
    current = world.run(run)
    definition = await definitions.definition_for(current)
    assert definition is not None and current.wake_at is not None
    await walker._advance(current, definition, current.wake_at)
    return world.run(run)


# --- arrival ---------------------------------------------------------------------


async def test_arrival_queues_the_call_and_the_run_waits_on_the_square(
    world: _World, queue: _Queue
) -> None:
    run = world.waiting(context={"phone": "+919876543210"})

    parked = await _visit(world, run)

    (asked,) = queue.asked
    assert (asked.lead_id, asked.template_id, asked.run_id) == (
        _lead_id(run),
        "tpl-1",
        str(run.id),
    )
    assert (asked.rank, asked.order) == (3, "newest_event")  # no letter today: pile
    assert world.leads == {}
    assert parked.status == "waiting" and parked.current_node == "call-1"
    assert parked.wake_at == run.entered_at + timedelta(days=7)  # the run's life
    assert "lead_visits_call-1" not in parked.context
    assert "_park_until" not in parked.context


async def test_a_live_call_says_what_it_falls_to_tomorrow(
    world: _World, queue: _Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(call_node, "_now", lambda: KYC_AT + timedelta(minutes=5))
    stamps = {
        "latest_topic": "LINE_KYC_COMPLETED",
        "latest_event_at": KYC_AT.isoformat(),
    }

    await _visit(world, world.waiting(context={"phone": "+919876543210", **stamps}))

    (asked,) = queue.asked
    assert (asked.rank, asked.next_rank, asked.next_order) == (1, 2, "newest_event")
    assert asked.event_at == KYC_AT


async def test_a_call_no_line_queue_takes_is_minted_at_once_as_before(
    world: _World, queue: _Queue
) -> None:
    queue.answer = None
    run = world.waiting()

    moved = await _visit(world, run)

    assert world.leads[_lead_id(run)].status == LeadCallStatus.BACKLOG
    assert moved.current_node == "after-call-1"  # by the unlabelled arrow


async def test_a_queue_that_failed_still_parks_the_run(
    world: _World, queue: _Queue
) -> None:
    queue.answer = False

    parked = await _visit(world, world.waiting())

    assert parked.current_node == "call-1" and world.leads == {}


async def test_a_call_square_with_no_topics_never_asks_the_queue(
    monkeypatch: pytest.MonkeyPatch, queue: _Queue
) -> None:
    nodes = [
        {k: v for k, v in n.items() if k not in ("topics", "key")}
        for n in PLAN["nodes"][1:3]
    ]
    plain = {**PLAN, "nodes": nodes, "edges": [["call-1", "after-call-1"]]}
    world = _install(monkeypatch, _World(plain))
    run = world.waiting()

    moved = await _visit(world, run)

    assert queue.asked == []
    assert list(world.leads) == [_lead_id(run)]
    assert moved.current_node == "after-call-1"


# --- leaving without a call ---------------------------------------------------------


async def test_a_listed_letter_takes_its_arrow_and_the_ask_is_taken_back(
    world: _World, queue: _Queue
) -> None:
    run = world.waiting(
        context={"phone": "+919876543210", "reply_call-1": "LINE_INITIATED"}
    )

    moved = await _visit(world, run)

    assert moved.current_node == "listen"
    assert queue.withdrawn == [("tpl-1", _lead_id(run))]
    assert queue.asked == [] and world.leads == {}
    assert "reply_call-1" not in moved.context


@pytest.mark.parametrize("ending", ["goal", "max_age", "ejected"])
async def test_a_run_that_ends_while_waiting_takes_its_ask_back(
    world: _World, queue: _Queue, ending: str
) -> None:
    run = world.waiting()
    if ending == "goal":
        world.goal_met = True
    elif ending == "max_age":
        run = world.waiting(entered_at=datetime.now(timezone.utc) - timedelta(days=8))
    else:
        world.plan_status = "archived"

    await walker.walk_run(run)

    assert world.run(run).status == "exited"
    assert queue.withdrawn == [("tpl-1", _lead_id(run))]


def test_the_goal_letter_takes_the_ask_back(
    letters, queue: _Queue, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    letters(PLAN, "call-1")

    async def ended(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(entry.enrollment_accessor, "cancel_run", ended)

    _consume(_event("LINE_ACTIVE"))

    assert [template for template, _ in queue.withdrawn] == ["tpl-1"]


# --- letters while it waits --------------------------------------------------------


def test_a_listed_letter_wakes_the_run_on_the_call_square(
    letters, queue: _Queue  # noqa: F811
) -> None:
    w = letters(PLAN, "call-1")

    _consume(_event("LINE_KYC_COMPLETED"))

    ((node, patch),) = w.resumes
    assert node == "call-1" and patch["reply_call-1"] == "LINE_KYC_COMPLETED"


def test_an_unlisted_letter_updates_facts_and_reranks_without_waking(
    letters, queue: _Queue  # noqa: F811
) -> None:
    """LINE_OFFERED is not on the call square's list. Its facts are kept, the
    run is not woken, and the waiting call (which has no lead row yet) is
    ranked again under its queued id: an event at 11:00 makes it live."""
    w = letters(PLAN, "call-1")

    _consume(_event("LINE_OFFERED"))

    assert len(w.remembered) == 1 and w.resumes == [] and w.refreshes == []
    assert w.lead_writes == []  # no row to write
    ((template, lead_id, rank, order, event_at),) = w.reranks  # the hook's arguments
    assert (template, rank, order, event_at) == ("tpl-1", 1, "first_ready", KYC_AT)
    assert lead_id != "lead-1" and len(lead_id) == 36
    # live today on an offer: the offer pile tomorrow
    assert w.reranks_later == [{"next_rank": 3, "next_order": "newest_event"}]


# --- the line is granted -------------------------------------------------------------


async def test_a_granted_call_is_due_now_and_takes_the_unlabelled_arrow(
    world: _World, queue: _Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def config(_id: str) -> Any:
        return SimpleNamespace(initial_offset=600)

    monkeypatch.setattr(call_node, "get_call_execution_config_by_template_id", config)
    run = world.waiting()
    parked = await _visit(world, run)
    assert parked.current_node == "call-1"
    lead_id = _lead_id(run)

    first = await materialize_call(str(run.id), lead_id, "number-1")
    second = await materialize_call(str(run.id), lead_id, "number-1")

    assert first == second == lead_id and list(world.leads) == [lead_id]
    lead = world.leads[lead_id]
    assert lead.status == LeadCallStatus.BACKLOG
    assert lead.next_attempt_at <= datetime.now(timezone.utc)  # the wait is over
    assert lead.meta_data["priority"]["rank"] == 3
    moved = world.run(run)
    assert moved.current_node == "after-call-1"
    assert moved.context["lead_visits_call-1"] == 1
    assert len(queue.asked) == 1  # the granted visit did not queue again


async def test_a_letter_that_lands_first_wins_over_the_grant(
    world: _World, queue: _Queue
) -> None:
    """The reply is on the run when the grant arrives: the grant's visit takes
    the letter's arrow, and no lead exists."""
    run = world.waiting(
        context={"phone": "+919876543210", "reply_call-1": "LINE_KYC_COMPLETED"}
    )

    answer = await materialize_call(str(run.id), _lead_id(run), "number-1")

    assert answer is Refusal.MOVED and world.leads == {}
    assert world.run(run).current_node == "quiet"


async def test_a_run_re_parked_under_the_grant_is_asked_again_not_dropped(
    world: _World, queue: _Queue
) -> None:
    """Something re-armed the waiting run while the grant ran (its alarm moved,
    nothing else): the grant's move misses, the run still waits for this very
    lead, so the answer is "ask again", never "moved"."""
    run = world.waiting()
    lead_id = _lead_id(run)

    def re_armed() -> None:
        world.runs[str(run.id)] = run.model_copy(
            update={"wake_at": datetime.now(timezone.utc)}
        )

    world.meanwhile = re_armed

    assert await materialize_call(str(run.id), lead_id, "number-1") is Refusal.ERROR
    assert world.leads == {}
    world.meanwhile = None
    assert await materialize_call(str(run.id), lead_id, "number-1") == lead_id


async def test_a_spent_id_is_queued_again_under_the_next_one(
    world: _World, queue: _Queue
) -> None:
    run = world.waiting()
    dead = _lead_id(run)
    world.leads[dead] = SimpleNamespace(id=dead, status=LeadCallStatus.FINISHED)

    assert await materialize_call(str(run.id), dead, "number-1") is Refusal.MOVED
    parked = await _visit(world, run)  # the grant woke it; the walker comes by

    assert [asked.lead_id for asked in queue.asked] == [_lead_id(run, 2)]
    assert parked.current_node == "call-1" and list(world.leads) == [dead]


# --- publish ------------------------------------------------------------------------


def test_publish_wants_an_arrow_per_listed_topic_and_one_for_the_placed_call() -> None:
    def problems(edges: List[List[str]], **call: Any) -> List[str]:
        nodes = [{**n, **call} if n["id"] == "call-1" else n for n in PLAN["nodes"]]
        return validate_definition({**PLAN, "nodes": nodes, "edges": edges})

    assert problems(PLAN["edges"]) == []
    no_topic_edge = [e for e in PLAN["edges"] if e[-1] != "LINE_INITIATED"]
    assert any("'LINE_INITIATED' but has no edge" in p for p in problems(no_topic_edge))
    no_placed_edge = [e for e in PLAN["edges"] if e != ["call-1", "after-call-1"]]
    assert any("needs one edge with no on" in p for p in problems(no_placed_edge))
    assert any("needs a key" in p for p in problems(PLAN["edges"], key=None))


# --- one dialable call at a time ---------------------------------------------------

TWO_CALLS: Dict[str, Any] = {
    **PLAN,
    "nodes": PLAN["nodes"] + [{**PLAN["nodes"][1], "id": "call-2"}],
    "edges": PLAN["edges"] + [["after-call-1", "call-2"], ["call-2", "listen"]],
}


def _second_lead_id(run: EnrollmentRun) -> str:
    return str(uuid5(NAMESPACE_URL, f"crm-workflow-lead:{run.id}:call-2:1"))


async def _timer_fires(world: _World, run: EnrollmentRun) -> EnrollmentRun:
    """The alarm of the square the run stands on comes due; one visit."""
    due = datetime.now(timezone.utc) - timedelta(seconds=1)
    world.runs[str(run.id)] = world.run(run).model_copy(update={"wake_at": due})
    return await _visit(world, run)


async def test_a_call_not_dialled_yet_keeps_its_run_on_the_wait_after_it(
    monkeypatch: pytest.MonkeyPatch, queue: _Queue
) -> None:
    """20:55 call 1 gets its line and its lead; the hours close and the dialler
    puts the lead off until morning. 22:55 the wait after call 1 comes due: the
    run stays on it (the timer starts over) and call 2 is not queued, so the
    customer never has two calls waiting. Once call 1 is dialled the same
    alarm moves the run on."""
    world = _install(monkeypatch, _World(TWO_CALLS))
    run = world.waiting(context={"phone": "+919876543210"})
    await _visit(world, run)
    first = _lead_id(run)
    assert await materialize_call(str(run.id), first, "number-1") == first

    held = await _timer_fires(world, run)

    assert held.current_node == "after-call-1"
    assert held.wake_at is not None
    assert held.wake_at > datetime.now(timezone.utc) + timedelta(minutes=119)
    assert [asked.lead_id for asked in queue.asked] == [first]

    world.leads[first].status = LeadCallStatus.FINISHED  # dialled, no report
    moved = await _timer_fires(world, run)

    assert moved.current_node == "call-2"
    assert [asked.lead_id for asked in queue.asked] == [first, _second_lead_id(run)]


async def test_a_grant_that_died_is_not_finished_once_the_run_moved_past_it(
    monkeypatch: pytest.MonkeyPatch, queue: _Queue
) -> None:
    """10:00:00 call 1 gets its line: the run moves to the wait after it and
    the grant dies before the insert. 12:00 that wait comes due with no lead to
    wait for, so the run queues call 2. A grant for call 1 that arrives now is
    refused and mints nothing: only call 2 can ever be dialled."""
    world = _install(monkeypatch, _World(TWO_CALLS))
    run = world.waiting(context={"phone": "+919876543210"})
    await _visit(world, run)
    first, second = _lead_id(run), _second_lead_id(run)
    insert = world.create_lead_call_tracker

    async def dies(**row: Any) -> Any:
        raise ConnectionError("the pod died before the insert")

    monkeypatch.setattr(call_node, "create_lead_call_tracker", dies)
    assert await materialize_call(str(run.id), first, "number-1") is Refusal.ERROR
    monkeypatch.setattr(call_node, "create_lead_call_tracker", insert)
    assert world.run(run).current_node == "after-call-1" and world.leads == {}

    assert (await _timer_fires(world, run)).current_node == "call-2"

    assert await materialize_call(str(run.id), first, "number-1") is Refusal.MOVED
    assert world.leads == {}
    assert await materialize_call(str(run.id), second, "number-1") == second
    assert list(world.leads) == [second]


async def test_a_letter_still_moves_a_run_whose_call_is_not_dialled_yet(
    monkeypatch: pytest.MonkeyPatch, queue: _Queue
) -> None:
    """Only the timer is held. A letter the wait listens for takes its arrow."""
    listening = {**PLAN["nodes"][2], "topics": ["LINE_INITIATED"], "key": "$topic"}
    plan = {
        **TWO_CALLS,
        "nodes": [
            n if n["id"] != "after-call-1" else listening for n in TWO_CALLS["nodes"]
        ],
        "edges": TWO_CALLS["edges"] + [["after-call-1", "listen", "LINE_INITIATED"]],
    }
    world = _install(monkeypatch, _World(plan))
    run = world.waiting(context={"phone": "+919876543210"})
    await _visit(world, run)
    assert await materialize_call(str(run.id), _lead_id(run), "number-1")
    answered = {**world.run(run).context, "reply_after-call-1": "LINE_INITIATED"}
    world.runs[str(run.id)] = world.run(run).model_copy(update={"context": answered})

    assert (await _timer_fires(world, run)).current_node == "listen"
