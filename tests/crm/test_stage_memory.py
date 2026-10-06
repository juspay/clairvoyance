"""Stage memory, and the stamps a call's rank is judged from.

A run that has queued a call stands on the wait that listens for THAT call's
report. A stage letter landing then (the customer finished KYC) is not that
square's answer, so it used to be dropped. On a plan that declares `priority`
it is now remembered: its facts are kept, the stamps move, and the call still
waiting is ranked again. The run is not woken and its timer does not move.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.nodes.call as call_node
from app.core import call_queue
from app.crm.outreach.db.queries.enrollment import remember_stage_facts_query
from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.schemas import EnrollmentRun, Workflow
from app.crm.record.schemas import RawEvent
from app.database.queries.breeze_buddy.lead_call_tracker import (
    update_waiting_lead_priority_query,
)
from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN
from tests.crm.test_call_priority import HOURS, NO_WINDOW, _plan

IST = ZoneInfo("Asia/Kolkata")
PLAN = _plan()
PLAIN_PLAN = {k: v for k, v in PLAN.items() if k != "priority"}

NIGHT = datetime(2026, 10, 9, 2, 0, tzinfo=IST)  # the offer that founded the run
KYC_AT = datetime(2026, 10, 9, 11, 0, tzinfo=IST)  # KYC done, inside the window
NOW = KYC_AT + timedelta(seconds=5)
STAMP = {"latest_topic": "LINE_KYC_COMPLETED", "latest_event_at": KYC_AT.isoformat()}
LIVE = {
    "rank": 1,
    "order": "first_ready",
    "event_ms": int(KYC_AT.timestamp() * 1000),
    "next_rank": 2,  # not called by closing: the KYC pile tomorrow
    "next_order": "newest_event",
}

needs_db = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run against Postgres"
)


def _event(
    topic: str = "LINE_KYC_COMPLETED",
    payload: Optional[Dict[str, Any]] = None,
    source: str = "flipkart",
    at: datetime = KYC_AT,
) -> RawEvent:
    return RawEvent(
        id="ev-9",
        merchant_id="m1",
        source=source,
        topic=topic,
        schema_version="1",
        external_id=f"{topic}:ev-9",
        payload=payload if payload is not None else {"stage": "kyc_done"},
        received_at=at,
        occurred_at=at,
    )


class _World:
    """The accessor slice the consumer touches, the lead row and the queue's
    hook: every write recorded."""

    def __init__(self, definition: Dict[str, Any], node: Optional[str]) -> None:
        self.flow = Workflow(
            id=uuid4(),
            merchant_id="m1",
            name="line nudge",
            status="live",
            version=1,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            definition=definition,
            draft=None,
        )
        self.node = node  # where her one open run stands; None = no run yet
        self.still_waiting = True  # is the call's lead still BACKLOG?
        self.resumes: List[Tuple[str, Dict[str, Any]]] = []
        self.refreshes: List[Tuple[str, Dict[str, Any]]] = []
        self.remembered: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
        self.enrolled: List[Dict[str, Any]] = []
        self.lead_writes: List[Tuple[str, Dict[str, Any]]] = []
        self.reranks: List[Tuple[Any, ...]] = []
        self.reranks_later: List[Dict[str, Any]] = []

    async def live_workflows(self, _merchant: str) -> List[Workflow]:
        return [self.flow] if self.node is None else []

    async def open_runs_for_customer(self, *_args: Any) -> List[EnrollmentRun]:
        if self.node is None:
            return []
        return [
            EnrollmentRun(
                id=uuid4(),
                merchant_id="m1",
                workflow_id=self.flow.id,
                workflow_version=1,
                customer_id=uuid4(),
                status="waiting",
                current_node=self.node,
                wake_at=NOW + timedelta(hours=20),
                entered_at=NIGHT,
                exited_at=None,
                exit_reason=None,
                context={
                    "phone": "+919876543210",
                    "latest_topic": "LINE_OFFERED",
                    "latest_event_at": NIGHT.isoformat(),
                    "lead_call-1": "lead-1",
                },
                enrollment_key="c-1",
                attempts=0,
                last_error=None,
            )
        ]

    async def get_definition(self, *_args: Any) -> Optional[Dict[str, Any]]:
        return self.flow.definition

    async def cancel_run(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    async def resume_run_by_id(
        self, _merchant: str, _run: str, node_id: str, patch: Dict[str, Any], *_: Any
    ) -> bool:
        if node_id != self.node:
            return False  # the statement's guard: only the square she stands on
        self.resumes.append((node_id, patch))
        return True

    async def refresh_run_facts(
        self, _merchant: str, _run: str, node_id: str, facts: Dict[str, Any], **_: Any
    ) -> bool:
        self.refreshes.append((node_id, facts))
        return True

    async def remember_stage_facts(
        self,
        _merchant: str,
        _run: str,
        node_id: str,
        heard_by: str,
        facts: Dict[str, Any],
        context_patch: Dict[str, Any],
    ) -> bool:
        self.remembered.append((node_id, heard_by, facts, context_patch))
        return True

    async def enrol(self, **kwargs: Any) -> object:
        self.enrolled.append(kwargs["context"])
        return object()

    async def update_waiting_lead_priority(
        self, lead_id: str, priority: Dict[str, Any]
    ) -> bool:
        self.lead_writes.append((lead_id, priority))
        return self.still_waiting

    async def rerank(self, *args: Any, **later: Any) -> None:
        self.reranks.append(args)  # app.core.call_queue.rerank_call's arguments
        self.reranks_later.append(later)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    """`world(definition, node)`: her one open run stands on `node`, the queue's
    hook is registered, and everything the consumer reaches is recorded."""

    def _install(
        definition: Dict[str, Any] = PLAN, node: Optional[str] = "after-call-1"
    ) -> _World:
        w = _World(definition, node)
        definitions._definitions.clear()
        monkeypatch.setattr(entry.workflow_accessor, "live_workflows", w.live_workflows)
        monkeypatch.setattr(
            definitions.version_accessor, "get_definition", w.get_definition
        )
        for name in (
            "open_runs_for_customer",
            "cancel_run",
            "resume_run_by_id",
            "refresh_run_facts",
            "remember_stage_facts",
        ):
            monkeypatch.setattr(entry.enrollment_accessor, name, getattr(w, name))
        monkeypatch.setattr(entry, "enrol", w.enrol)
        monkeypatch.setattr(
            call_node, "update_waiting_lead_priority", w.update_waiting_lead_priority
        )
        monkeypatch.setattr(call_node, "_now", lambda: NOW)
        monkeypatch.setattr(call_node, "rerank_call", w.rerank)
        return w

    return _install


def _consume(event: RawEvent, variables: Optional[Dict[str, Any]] = None) -> None:
    asyncio.run(consume_attributed_event(event, "c-1", {}, variables))


def test_stage_memory_updates_facts_without_waking(world) -> None:
    """KYC is finished while the run waits for its call's report. The letter's
    facts and the stamps are kept under the square that would have heard it;
    neither write that re-arms a run (the reply, the refresh) is made."""
    w = world()

    _consume(_event(), {"kyc_status": "done"})

    assert w.remembered == [
        (
            "after-call-1",
            "quiet",
            {"stage": "kyc_done", "kyc_status": "done"},
            {"latest_letter": "quiet", **STAMP},
        )
    ]
    assert w.resumes == [] and w.refreshes == []


def test_stage_memory_reranks_the_waiting_call(world) -> None:
    """Her offer was the night's (rank 3). KYC at 11:00 is inside today's
    window, so the call still waiting is a live customer's now: the lead row
    learns it, then the queue is told."""
    w = world()

    _consume(_event())

    assert w.lead_writes == [("lead-1", LIVE)]
    assert w.reranks == [("tpl-1", "lead-1", 1, "first_ready", KYC_AT)]


def test_the_rerank_says_what_a_live_call_falls_to_tomorrow(world) -> None:
    """She is live today; not called by closing she is KYC pile tomorrow. The
    queue is told both, so it can move her at the next opening."""
    w = world()

    _consume(_event())

    assert w.reranks_later == [{"next_rank": 2, "next_order": "newest_event"}]


def test_a_rerank_reads_the_templates_hours_only_when_the_plan_has_no_window(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today is the template's call hours: the re-rank reads that template's
    call config once. A plan that still names its own window reads nothing."""
    asked: List[str] = []

    async def config(template_id: str) -> Any:
        asked.append(template_id)
        return HOURS

    monkeypatch.setattr(call_node, "get_call_execution_config_by_template_id", config)

    w = world(_plan(priority=NO_WINDOW))
    _consume(_event())
    assert w.lead_writes == [("lead-1", LIVE)] and asked == ["tpl-1"]

    w = world()
    _consume(_event())
    assert w.lead_writes == [("lead-1", LIVE)] and asked == ["tpl-1"]


def test_with_no_hook_registered_only_the_lead_row_learns_the_rank(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world()
    monkeypatch.setattr(call_node, "rerank_call", call_queue.rerank_call)
    monkeypatch.setattr(call_queue, "_hooks", None)

    _consume(_event())

    assert w.lead_writes == [("lead-1", LIVE)]
    assert w.reranks == []


def test_a_call_no_longer_waiting_is_not_reranked(world) -> None:
    """The lead row takes the rank only while it is BACKLOG; a call already
    dialling or finished keeps its rank, and the queue is not told."""
    w = world()
    w.still_waiting = False

    _consume(_event())

    assert len(w.remembered) == 1 and len(w.lead_writes) == 1
    assert w.reranks == []


def test_a_failing_rank_change_never_fails_the_letter(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world()

    async def broken(*_args: Any) -> None:
        raise RuntimeError("the queue is away")

    monkeypatch.setattr(call_node, "rerank_call", broken)

    _consume(_event())

    assert len(w.remembered) == 1


def test_a_plan_with_no_priority_drops_the_letter_as_before(world) -> None:
    w = world(PLAIN_PLAN)

    _consume(_event())

    assert w.remembered == [] and w.refreshes == [] and w.lead_writes == []


def test_the_report_this_square_waits_for_is_a_reply_not_a_memory(world) -> None:
    """call.completed for THIS call is the square's own answer: the run is
    woken by the reply as always, unstamped (a call finishing is not the
    customer doing something), and nothing is remembered or ranked."""
    w = world()

    _consume(
        _event(
            "call.completed",
            {"lead_id": "lead-1", "outcome": "NO_ANSWER"},
            source="telephony",
        )
    )

    assert w.resumes == [
        ("after-call-1", {"reply_after-call-1": "NO_ANSWER", "cut_short_by": "ev-9"})
    ]
    assert w.remembered == [] and w.lead_writes == []


def test_a_reply_on_a_priority_plan_carries_the_stamps(world) -> None:
    w = world(node="quiet")

    _consume(_event())

    assert w.resumes == [
        (
            "quiet",
            {
                "reply_quiet": "LINE_KYC_COMPLETED",
                "cut_short_by": "ev-9",
                "latest_letter": "quiet",
                **STAMP,
            },
        )
    ]
    assert w.remembered == []  # the square took it


def test_a_letter_on_a_deaf_square_carries_the_stamps(world) -> None:
    """The run stands on the call square (it listens to nothing): the refresh
    takes the stamps at the top level, beside the facts."""
    w = world(node="call-1")

    _consume(_event())

    assert w.refreshes == [("call-1", {"stage": "kyc_done", **STAMP})]
    assert w.remembered == []


@pytest.mark.parametrize("node", ["after-call-1", "quiet", "call-1"])
def test_a_late_older_letter_never_moves_the_stamps_back(world, node: str) -> None:
    """A letter that HAPPENED before the one the run already carries, and only
    arrived late, leaves the stamps alone on every path (stage memory, a
    square's reply, a deaf square): a live call must not fall to the pile.
    Its facts are still kept."""
    w = world(node=node)

    _consume(_event(at=NIGHT - timedelta(hours=5)))

    patches = [p for *_, p in w.remembered] + [p for _, p in w.resumes + w.refreshes]
    assert patches  # the letter is still written
    assert not any("latest_topic" in p or "latest_event_at" in p for p in patches)
    if node == "after-call-1":
        assert w.remembered[0][2] == {"stage": "kyc_done"}
        # the call is ranked from the stamps the run keeps
        assert [p["event_ms"] for _, p in w.lead_writes] == [
            int(NIGHT.timestamp() * 1000)
        ]


def test_a_letter_of_the_same_moment_moves_the_stamps(world) -> None:
    """Equal times: the later arrival wins."""
    w = world(node="call-1")

    _consume(_event(at=NIGHT))

    assert w.refreshes[0][1]["latest_topic"] == "LINE_KYC_COMPLETED"


@pytest.mark.parametrize("hours, moved", [(-9, False), (1, True)])
def test_a_late_older_repeat_never_moves_the_open_runs_stamps_back(
    world, monkeypatch: pytest.MonkeyPatch, hours: int, moved: bool
) -> None:
    """The door's own topic arriving again is offered to the open run as a
    repeat, and a refresh merges those facts into the run. A repeat that
    HAPPENED before the letter the run already carries (KYC at 11:00) offers
    its facts without the stamps; one that happened after carries them."""
    w = world(node="after-call-1")
    offered: List[Dict[str, Any]] = []

    async def open_runs(*_args: Any) -> List[EnrollmentRun]:
        (run,) = await w.open_runs_for_customer()
        run.context.update(STAMP)
        return [run]

    async def live(_merchant: str) -> List[Workflow]:
        return [w.flow]

    async def refused(**_kwargs: Any) -> None:
        return None

    async def repeat(*args: Any) -> None:
        offered.append(args[-1])

    monkeypatch.setattr(entry.enrollment_accessor, "open_runs_for_customer", open_runs)
    monkeypatch.setattr(entry.workflow_accessor, "live_workflows", live)
    monkeypatch.setattr(entry, "enrol", refused)
    monkeypatch.setattr(entry, "apply_repeat", repeat)

    _consume(
        _event("LINE_OFFERED", {"offer": "5 lakh"}, at=KYC_AT + timedelta(hours=hours))
    )

    (facts,) = offered
    assert facts["offer"] == "5 lakh"  # the repeat's own facts are still offered
    assert ("latest_topic" in facts, "latest_event_at" in facts) == (moved, moved)


def test_enrolment_stamps_the_founding_letter_on_a_priority_plan_only(world) -> None:
    for definition, stamped in ((PLAN, True), (PLAIN_PLAN, False)):
        w = world(definition, node=None)

        _consume(_event("LINE_OFFERED", {"offer": "5 lakh"}, at=NIGHT))

        (context,) = w.enrolled
        assert context.get("latest_topic") == ("LINE_OFFERED" if stamped else None)
        assert context.get("latest_event_at") == (
            NIGHT.isoformat() if stamped else None
        )


def test_the_memory_statement_does_not_touch_the_timer() -> None:
    """The reply and the refresh both set wake_at = now() and forgive a parked
    run. This statement may do neither: the letter is not this square's."""
    sql, params = remember_stage_facts_query(
        "m1", "run-1", "after-call-1", "quiet", {"stage": "kyc"}, {"latest_topic": "X"}
    )
    assigned = sql.split("SET", 1)[1].split("WHERE", 1)[0]

    for column in ("wake_at", "status", "attempts", "last_error"):
        assert column not in assigned
    assert "merchant_id = $1 AND id = $2" in sql and "current_node = $3" in sql
    assert params == [
        "m1",
        "run-1",
        "after-call-1",
        json.dumps({"latest_topic": "X"}),
        "quiet",
        json.dumps({"stage": "kyc"}),
    ]


def test_the_lead_statement_touches_only_a_waiting_lead() -> None:
    sql, params = update_waiting_lead_priority_query("lead-1", LIVE)

    assert '"id" = $2 AND "status" = $3' in sql
    assert params == [json.dumps(LIVE), "lead-1", "BACKLOG"]


def test_the_lead_statement_leaves_a_held_lead_and_the_lock_clock_alone() -> None:
    """`updated_at` is the stale-lock clock (clean_stale_bb_locks_query), and a
    locked lead is one a dialler holds: the rank write touches neither."""
    sql, _ = update_waiting_lead_priority_query("lead-1", LIVE)

    assert '"is_locked" = FALSE' in sql
    assert "updated_at" not in sql


@needs_db
async def test_on_postgres_the_run_keeps_its_timer_and_learns_the_stage() -> None:
    """Against a TEMP table shaped like the columns the statement touches: a
    waiting run and a parked run on the square learn the letter and keep their
    timer and state; a run that has left the square is not touched."""
    import asyncpg

    wake = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)
    before = {"phone": "+91", "facts": {"quiet": {"stage": "offered"}}}
    rows = [
        (UUID(int=1), "waiting", "after-call-1", None, 2),
        (UUID(int=2), "parked", "after-call-1", "template not found", 3),
        (UUID(int=3), "waiting", "rest", None, 0),
    ]
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TEMP TABLE crm_workflow_enrollment (merchant_id text, id uuid,"
            " status text, current_node text, context jsonb, wake_at timestamptz,"
            " last_error text, attempts int)"
        )
        for run_id, status, node, error, attempts in rows:
            await conn.execute(
                "INSERT INTO crm_workflow_enrollment VALUES "
                "('m1', $1, $2, $3, $4::jsonb, $5, $6, $7)",
                run_id,
                status,
                node,
                json.dumps(before),
                wake,
                error,
                attempts,
            )
            sql, params = remember_stage_facts_query(
                "m1", str(run_id), "after-call-1", "quiet", {"stage": "kyc"}, STAMP
            )
            await conn.execute(sql, *params)
        after = await conn.fetch("SELECT * FROM crm_workflow_enrollment ORDER BY id")
    finally:
        await conn.close()

    assert [
        (r["id"], r["status"], r["current_node"], r["last_error"], r["attempts"])
        for r in after
    ] == rows
    assert all(r["wake_at"] == wake for r in after)
    learned = {"phone": "+91", "facts": {"quiet": {"stage": "kyc"}}, **STAMP}
    assert [json.loads(r["context"]) for r in after] == [learned, learned, before]


@needs_db
async def test_on_postgres_the_rank_is_merged_into_a_waiting_leads_meta() -> None:
    import asyncpg

    meta = {"workflow_id": "w", "enrollment_id": "r"}
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TEMP TABLE lead_call_tracker (id text, status text,"
            " meta_data jsonb, updated_at timestamptz,"
            " is_locked boolean DEFAULT FALSE)"
        )
        for lead_id, status in (("waiting", "BACKLOG"), ("dialling", "PROCESSING")):
            await conn.execute(
                "INSERT INTO lead_call_tracker VALUES ($1, $2, $3::jsonb, NULL)",
                lead_id,
                status,
                json.dumps(meta),
            )
            sql, params = update_waiting_lead_priority_query(lead_id, LIVE)
            await conn.execute(sql, *params)
        after = {
            r["id"]: json.loads(r["meta_data"])
            for r in await conn.fetch("SELECT id, meta_data FROM lead_call_tracker")
        }
    finally:
        await conn.close()

    assert after == {"waiting": {**meta, "priority": LIVE}, "dialling": meta}


@needs_db
async def test_on_postgres_a_held_lead_keeps_its_rank_and_no_clock_moves() -> None:
    """A BACKLOG lead a dialler holds (is_locked) keeps the rank it had; a free
    one takes the new rank. Neither `updated_at` moves: it is the lock's age."""
    import asyncpg

    then = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TEMP TABLE lead_call_tracker (id text, status text,"
            " meta_data jsonb, updated_at timestamptz, is_locked boolean)"
        )
        for lead_id, locked in (("free", False), ("held", True)):
            await conn.execute(
                "INSERT INTO lead_call_tracker VALUES"
                " ($1, 'BACKLOG', '{}'::jsonb, $2, $3)",
                lead_id,
                then,
                locked,
            )
            sql, params = update_waiting_lead_priority_query(lead_id, LIVE)
            await conn.execute(sql, *params)
        after = {
            r["id"]: (json.loads(r["meta_data"]), r["updated_at"])
            for r in await conn.fetch(
                "SELECT id, meta_data, updated_at FROM lead_call_tracker"
            )
        }
    finally:
        await conn.close()

    assert after == {"free": ({"priority": LIVE}, then), "held": ({}, then)}
