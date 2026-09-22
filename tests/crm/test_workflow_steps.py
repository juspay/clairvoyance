"""Where a run has BEEN (canon T26, migration 073).

The failure mode this design has is a flush honoured at some call sites and
not others, so these tests pin the sites and the three traps rather than the
happy path alone:

  trap 1  a windowed hold re-arms the SAME square — no row, no restamp, or
          "waiting since Friday" renders as "waiting since 9am"
  trap 2  ruled: a park is visible only while it is happening
  trap 3  the goal-cancel is the one run-ending writer outside the walker,
          so it is the one that silently loses every CONVERTED run's last
          square

And the property the whole shape rests on: the INSERT selects FROM the
UPDATE's own RETURNING, so a step row exists if and only if the move
committed.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest

import app.crm.outreach.definitions as definitions
import app.crm.outreach.entry as entry
import app.crm.outreach.walker as walker
from app.crm.outreach.db.queries.enrollment import (
    advance_run_query,
    cancel_run_query,
    exit_run_query,
)
from app.crm.outreach.nodes.context import CUT_SHORT_BY_KEY, OUTCOME_KEY
from app.crm.outreach.schemas import (
    EnrollmentRun,
    RunStep,
    Workflow,
    WorkflowDefinition,
)
from app.crm.outreach.steps import (
    ARRIVED_BY_DOOR,
    ARRIVED_BY_LETTER,
    ARRIVED_BY_TIMER,
    ARRIVED_BY_WALK,
    as_rows,
    first_arrival,
    step,
    timeline,
)
from app.crm.record.schemas import RawEvent
from tests.crm.doubles import patch_accessors

NOW = datetime(2026, 9, 17, 9, 41, 7, tzinfo=timezone.utc)
LEASE = NOW + timedelta(seconds=300)
ARRIVED = NOW - timedelta(minutes=41)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: Optional[timezone] = None) -> datetime:  # type: ignore[override]
        return NOW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(walker, "datetime", _Frozen)


# The corpus's own example: one claim, four squares, one row. `hear` is
# answered 41 minutes after she arrived on it; the chain then runs a
# condition and a split before stopping on a 24h wait.
_BOARD: Dict[str, Any] = {
    "entry": {"topic": "cod.asked"},
    "nodes": [
        {
            "id": "hear",
            "type": "wait",  # a wait with topics is the listening wait (17 Sep 2026)
            "minutes": 120,
            "topics": ["button.reply"],
            "key": "button_id",
        },
        {
            "id": "value",
            "type": "condition",
            "rules": [
                {
                    "on": "big",
                    "if": [{"field": "context.cart_value", "op": ">=", "value": 5000}],
                }
            ],
        },
        {
            "id": "arm",
            "type": "split",
            "arms": [{"on": "discount", "percent": 50}, {"on": "plain", "percent": 50}],
        },
        {"id": "settle", "type": "wait", "minutes": 1440},
    ],
    "edges": [
        ["hear", "value", "yes"],
        ["hear", "settle", "timeout"],
        ["value", "arm", "big"],
        ["value", "arm", "else"],
        ["arm", "settle", "discount"],
        ["arm", "settle", "plain"],
    ],
    "goal": {"topics": ["order.placed"]},
}

# Trap 1's real shape: a WINDOWED square. Its timer has ended, but the
# calling hours are shut, so the walker re-arms the run on the square it is
# already standing on — advance_run with its own node id.
_HOLD: Dict[str, Any] = {
    "entry": {"topic": "cod.asked"},
    "nodes": [
        {
            "id": "ring",
            "type": "wait",
            "minutes": 1,
            "window": {"opens": "09:00", "closes": "21:00", "timezone": "Asia/Kolkata"},
        },
        {"id": "settle", "type": "wait", "minutes": 1440},
    ],
    "edges": [["ring", "settle"]],
    "goal": {"topics": ["order.placed"]},
}


def _run(
    node: str = "hear",
    context: Optional[Dict[str, Any]] = None,
    node_arrived_at: Optional[datetime] = ARRIVED,
    attempts: int = 1,
    status: str = "waiting",
) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status=status,
        current_node=node,
        wake_at=LEASE,
        entered_at=ARRIVED - timedelta(days=1),
        exited_at=None,
        exit_reason=None,
        context=context if context is not None else {},
        enrollment_key="c-1",
        attempts=attempts,
        last_error=None,
        node_arrived_at=node_arrived_at,
    )


class _Writes:
    def __init__(self, definition: Dict[str, Any], matched: bool = True) -> None:
        self.definition = definition
        self.matched = matched
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
        self.flushes: List[Dict[str, Any]] = []

    async def get_workflow(self, merchant_id: str, workflow_id: str) -> Workflow:
        return Workflow(
            id=uuid4(),
            merchant_id=merchant_id,
            name="plan",
            status="live",
            version=1,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            definition=self.definition,
            draft=None,
        )

    async def get_definition(self, *args: Any) -> Dict[str, Any]:
        return self.definition

    async def advance_run(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("advance", args))
        self.flushes.append(kwargs)
        return self.matched

    async def exit_run(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("exit", args))
        self.flushes.append(kwargs)
        return self.matched

    async def park_run(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("park", args))
        return self.matched

    async def record_run_error(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(("retry", args))
        return self.matched


@pytest.fixture
def no_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(walker, "customer_has_event", never)


def _walk(monkeypatch: pytest.MonkeyPatch, writes: _Writes, run: EnrollmentRun) -> None:
    patch_accessors(monkeypatch, walker, writes)
    patch_accessors(monkeypatch, definitions, writes)
    definitions._definitions.clear()
    asyncio.run(
        walker._advance(
            run, WorkflowDefinition.model_validate(writes.definition), LEASE
        )
    )


# --- the flush rides the CAS -----------------------------------------------


def test_the_insert_selects_from_the_update_so_a_stale_lease_writes_no_history() -> (
    None
):
    """The whole design in one property: no move, no row. The INSERT's FROM
    is the UPDATE's own RETURNING, so there is no dual write to reconcile
    and nothing to sweep when a lease turns out to be stale.

    These assertions pin the statement's SHAPE — substrings cannot fail for
    the reason the property would; the property itself is Postgres CTE
    semantics."""
    for sql in (
        advance_run_query("r-1", "settle", NOW, {}, LEASE, NOW, [])[0],
        exit_run_query("r-1", "completed", None, None, LEASE, [])[0],
        cancel_run_query("m1", "r-1", "goal_met", steps=[])[0],
    ):
        assert "WITH moved AS (" in sql
        assert "INSERT INTO crm_workflow_step" in sql
        assert "FROM moved m," in sql, sql
        # The CAS answer is the UPDATE's id, not "a row came back": these
        # statements always return exactly one row.
        assert "SELECT (SELECT id FROM moved) AS moved_id" in sql


def test_the_flush_never_names_its_own_tenancy() -> None:
    """merchant_id, workflow_id and the version come off the row that
    actually moved — a caller cannot file a step under another tenant."""
    sql, _ = advance_run_query("r-1", "settle", NOW, {}, LEASE, NOW, [])
    assert "SELECT m.merchant_id, m.id, m.workflow_id, m.workflow_version" in sql


# --- one visit, several squares, one write ---------------------------------


def test_four_squares_in_one_visit_flush_as_three_closed_rows(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """The corpus's example. Before T26 this visit told the database one
    sentence ("she is on settle now") and three squares left no trace."""
    writes = _Writes(_BOARD)
    run = _run(context={"reply_hear": "yes", "cart_value": 6240})
    _walk(monkeypatch, writes, run)

    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "settle"
    rows = writes.flushes[0]["steps"]
    assert [r["node"] for r in rows] == ["hear", "value", "arm"]
    assert [r["next_node"] for r in rows] == ["value", "arm", "settle"]
    # Exactly one write for the whole chain.
    assert len(writes.calls) == 1


def test_the_branch_each_square_took_reaches_disk(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """`without_reply` deletes the answer before the write, so a condition's
    branch is recorded NOWHERE else — "why did this customer get the
    discount version?" is unanswerable without this column."""
    writes = _Writes(_BOARD)
    _walk(monkeypatch, writes, _run(context={"reply_hear": "yes", "cart_value": 6240}))
    rows = {r["node"]: r["outcome"] for r in writes.flushes[0]["steps"]}
    assert rows["hear"] == "yes"
    assert rows["value"] == "big"
    assert rows["arm"] in ("discount", "plain")


def test_the_first_square_is_dated_from_the_run_and_the_chain_from_each_other(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """node_arrived_at is the ONLY place the current square's arrival lives
    until its step closes; every square after it arrives as the one before
    it leaves, so the timeline has no gaps."""
    writes = _Writes(_BOARD)
    _walk(monkeypatch, writes, _run(context={"reply_hear": "yes", "cart_value": 10}))
    rows = writes.flushes[0]["steps"]
    assert rows[0]["arrived_at"] == ARRIVED.isoformat()
    assert rows[1]["arrived_at"] == rows[0]["left_at"]
    assert rows[2]["arrived_at"] == rows[1]["left_at"]
    # And the new square's arrival is restamped to the moment it landed —
    # a real timestamp on the run row, the same instant the last closed row
    # carries as ISO text in the flush payload.
    assert writes.flushes[0]["node_arrived_at"].isoformat() == rows[2]["left_at"]


def test_only_the_claimed_square_carries_the_visits_attempts(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """attempts is "how many claims this square cost". Squares chained
    behind it cost part of that same claim."""
    writes = _Writes(_BOARD)
    _walk(
        monkeypatch,
        writes,
        _run(context={"reply_hear": "yes", "cart_value": 10}, attempts=4),
    )
    assert [r["attempts"] for r in writes.flushes[0]["steps"]] == [4, 1, 1]


# --- trap 1: the windowed hold ---------------------------------------------


def test_a_windowed_hold_writes_no_step_and_does_not_restamp_the_arrival(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """Trap 1, at the site that really does it: a windowed square whose
    hours are shut calls advance_run with its OWN node id (walker's hold).

    The token has not left, so there is nothing to close — and restamping
    would make "waiting since Friday" render as "waiting since 9am" every
    morning the window re-armed. NOW is 09:41 UTC, which is 15:11 IST and
    inside 09:00-21:00, so the test pins the CLOSED case explicitly."""
    writes = _Writes(_HOLD)
    run = _run(node="ring")
    shut = datetime(2026, 9, 17, 22, 0, tzinfo=timezone.utc)  # 03:30 IST

    class _Shut(datetime):
        @classmethod
        def now(cls, tz: Optional[timezone] = None) -> datetime:  # type: ignore[override]
            return shut

    monkeypatch.setattr(walker, "datetime", _Shut)
    _walk(monkeypatch, writes, run)

    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "ring"  # the SAME square
    assert writes.flushes[0]["steps"] == []
    assert writes.flushes[0]["node_arrived_at"] is None


def test_advancing_onto_a_square_whose_window_is_shut_is_a_real_move(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """NOT the hold — window.alarm already folds the next opening into the
    alarm, so chaining onto a shut square takes the ordinary advance path.
    The token really moves, so the square it left is closed and the arrival
    IS restamped. Pinned because the two paths look alike from the outside
    and only one of them may write a row."""
    board = {
        **_HOLD,
        "nodes": [
            {"id": "gate", "type": "condition", "rules": []},
            *_HOLD["nodes"],
        ],
        "edges": [["gate", "ring", "else"], ["ring", "settle"]],
    }
    writes = _Writes(board)
    shut = datetime(2026, 9, 17, 22, 0, tzinfo=timezone.utc)

    class _Shut(datetime):
        @classmethod
        def now(cls, tz: Optional[timezone] = None) -> datetime:  # type: ignore[override]
            return shut

    monkeypatch.setattr(walker, "datetime", _Shut)
    _walk(monkeypatch, writes, _run(node="gate"))

    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "ring"
    assert [r["node"] for r in writes.flushes[0]["steps"]] == ["gate"]
    assert writes.flushes[0]["node_arrived_at"] is not None


def test_a_hold_keeps_the_arrival_because_the_column_is_coalesced() -> None:
    """The None above has to MEAN "keep what is there" in the statement, or
    the hold nulls the only arrival stamp the run has."""
    sql, params = advance_run_query("r-1", "ring", NOW, {}, LEASE, None, [])
    assert "node_arrived_at = COALESCE($5::timestamptz, node_arrived_at)" in sql
    assert params[4] is None


# --- trap 3: the goal-cancel, outside the walker ----------------------------


def test_the_goal_cancel_closes_the_final_square_of_a_converted_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trap 3: no claim, no visit, no lease — and it is the exit that every
    CONVERTED run takes, so a missing flush here loses exactly the runs a
    merchant cares most about."""
    seen: Dict[str, Any] = {}

    class _Spine:
        async def cancel_run(self, *args: Any, **kwargs: Any) -> bool:
            seen["steps"] = kwargs.get("steps")
            return True

    patch_accessors(monkeypatch, entry, _Spine())
    stale_wake = str(uuid4())  # a wake letter still waiting to be flushed
    run = _run(node="hear", context={CUT_SHORT_BY_KEY: stale_wake})
    definition = WorkflowDefinition.model_validate(_BOARD)

    letter = RawEvent(
        id=str(uuid4()),
        merchant_id="m1",
        source="shopify",
        topic="order.placed",
        schema_version="1",
        external_id="o-9",
        payload={},
        received_at=NOW,
        occurred_at=NOW,
    )

    assert asyncio.run(entry._end_on_goal(run, definition, letter, None)) is True
    ((row,),) = (seen["steps"],)
    assert row["node"] == "hear" and row["outcome"] == "goal_met"
    assert row["next_node"] is None  # the run ended here
    assert row["arrived_at"] == ARRIVED.isoformat()
    # The letter that cut the square short is the one in hand — never NULL,
    # never the stale wake lying in context.
    assert row["cut_short_by"] == str(letter.id)


# --- honest nulls, and how a square came to be walked -----------------------


def test_a_run_from_before_073_loses_one_row_rather_than_inventing_a_time(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """No arrival stamp, no honest arrived_at. Dating it to entered_at would
    be a lie on any screen that says "waiting since" — so that ONE row is
    dropped and every square after it is exact."""
    writes = _Writes(_BOARD)
    _walk(
        monkeypatch,
        writes,
        _run(context={"reply_hear": "yes", "cart_value": 10}, node_arrived_at=None),
    )
    rows = writes.flushes[0]["steps"]
    assert [r["node"] for r in rows] == ["value", "arm"]


LETTER_ID = "8f14e45f-ce5c-4a1b-9b2d-0d2f9a7c1e33"


def test_a_letter_that_beat_the_alarm_is_named_on_the_square_it_cut_short(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """entry.py leaves the letter's id behind when it re-arms a run in
    place; the flush that follows records it, and reads the visit as
    arrived_by = letter rather than the plausible-looking "timer"."""
    writes = _Writes(_BOARD)
    _walk(
        monkeypatch,
        writes,
        _run(
            context={"reply_hear": "yes", "cart_value": 10, "cut_short_by": LETTER_ID}
        ),
    )
    rows = writes.flushes[0]["steps"]
    assert rows[0]["cut_short_by"] == LETTER_ID
    assert rows[0]["arrived_by"] == ARRIVED_BY_LETTER
    # The letter cut ONE square short, not the chain behind it.
    assert [r["cut_short_by"] for r in rows[1:]] == [None, None]
    assert [r["arrived_by"] for r in rows[1:]] == [ARRIVED_BY_WALK, ARRIVED_BY_WALK]


def test_the_letter_pointer_is_not_written_back_onto_the_run(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """Popped, not read: left in the context it would age into a later visit
    and credit the wrong square — the same reason a branching square's reply
    is cleared on the way out."""
    writes = _Writes(_BOARD)
    _walk(
        monkeypatch,
        writes,
        _run(
            context={"reply_hear": "yes", "cart_value": 10, "cut_short_by": LETTER_ID}
        ),
    )
    ((_, args),) = writes.calls
    assert "cut_short_by" not in args[3]


def test_door_needs_no_stored_flag() -> None:
    """A run that has never moved has node_arrived_at == entered_at, which
    is exactly as long as "the door put it here" is true."""
    born = _run()
    born = born.model_copy(update={"node_arrived_at": born.entered_at})
    assert first_arrival(born, None) == ARRIVED_BY_DOOR
    assert first_arrival(_run(), None) == ARRIVED_BY_TIMER


def test_door_outranks_the_letter_that_cut_the_first_square_short() -> None:
    """A door may start a run on a LISTENING square (phase 15), so a letter
    can cut the run's very first square short. Answering `letter` there
    threw away the one fact true exactly once per run — and said nothing
    the row did not already say, since cut_short_by names that letter on
    the same row."""
    born = _run()
    born = born.model_copy(update={"node_arrived_at": born.entered_at})
    assert first_arrival(born, LETTER_ID) == ARRIVED_BY_DOOR
    # and a square the run walked onto still reads the letter
    assert first_arrival(_run(), LETTER_ID) == ARRIVED_BY_LETTER


# --- law 3: every read is a union ------------------------------------------


def _closed(node: str) -> RunStep:
    return RunStep(
        node=node,
        node_type="wait",
        arrived_at=ARRIVED,
        left_at=NOW,
        arrived_by=ARRIVED_BY_TIMER,
    )


def test_the_timeline_appends_the_square_the_run_is_standing_on() -> None:
    """Law 3. Only CLOSED steps are written, so a reader that forgets the
    union shows a timeline exactly one square short of the truth."""
    run = _run(node="settle")
    rows = timeline(run, [_closed("hear")], WorkflowDefinition.model_validate(_BOARD))
    assert [r.node for r in rows] == ["hear", "settle"]
    assert rows[-1].left_at is None  # still here
    assert rows[-1].node_type == "wait"
    assert rows[-1].arrived_at == ARRIVED


def test_an_exited_run_has_no_open_square() -> None:
    run = _run(node="settle", status="exited")
    assert [r.node for r in timeline(run, [_closed("hear")])] == ["hear"]


def test_a_run_without_an_arrival_stamp_contributes_no_open_square() -> None:
    """Same honesty as the flush: no stamp, nothing truthful to say about
    when it got there."""
    run = _run(node="settle", node_arrived_at=None)
    assert [r.node for r in timeline(run, [_closed("hear")])] == ["hear"]


def test_a_skewed_clock_never_fails_the_move_it_rides_on() -> None:
    """073's CHECK (left_at >= arrived_at) must not be reachable from the
    flush: the arrival is stamped by whichever pod last advanced the run
    and the exit by this one, so a skewed clock can invert them. A zero
    length step is visibly odd; a failed INSERT would fail the ADVANCE,
    retry the visit and park a run over a wall clock."""
    node = WorkflowDefinition.model_validate(_BOARD).nodes[0]
    ((row,),) = (
        as_rows(
            step(
                node,
                NOW,
                NOW - timedelta(minutes=5),  # "left" before it arrived
                ARRIVED_BY_TIMER,
                None,
                "settle",
                _run(),
                None,
                first=True,
            )
        ),
    )
    assert row["left_at"] == row["arrived_at"] == NOW.isoformat()


def test_a_hold_gives_the_letter_back_rather_than_spending_it(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """The hold is reached almost only by a RE-ARM — a letter on a deaf
    square, a repeat, a retry, an operator resume — landing in closed hours.
    In the letter's case the pointer must survive the hold: the square it
    woke is still the square the token stands on, and the visit that finally
    closes it owes the row. Spending it here would leave that row saying
    `timer`."""
    writes = _Writes(_HOLD)
    shut = datetime(2026, 9, 17, 22, 0, tzinfo=timezone.utc)

    class _Shut(datetime):
        @classmethod
        def now(cls, tz: Optional[timezone] = None) -> datetime:  # type: ignore[override]
            return shut

    monkeypatch.setattr(walker, "datetime", _Shut)
    _walk(monkeypatch, writes, _run(node="ring", context={"cut_short_by": LETTER_ID}))

    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "ring"
    assert writes.flushes[0]["steps"] == []
    # Written back, not consumed.
    assert args[3]["cut_short_by"] == LETTER_ID


# --- review fixes (17 Sep 2026) --------------------------------------------


def test_the_goal_cancel_still_ends_the_run_but_guards_only_its_row() -> None:
    """A walker can advance a run between the consumer's read and the
    unconditional goal-cancel. Ending the run must stay unconditional — the
    event side wins by law, and a customer who just bought must never keep
    being nudged — but the closing row describes a snapshot taken a moment
    earlier, so it lands only while that snapshot is still true. Otherwise
    the advance has already closed that square and this would duplicate it
    under a square the run no longer stands on."""
    sql, _ = cancel_run_query("m1", "r-1", "goal_met", steps=[{"node": "hear"}])
    # the exit: no snapshot predicate anywhere in the UPDATE
    update = sql.split("RETURNING id, merchant_id")[0]
    assert "current_node =" not in update and "node_arrived_at" not in update
    # the row: guarded
    assert "WHERE m.current_node = s.node" in sql
    assert "m.node_arrived_at IS NOT DISTINCT FROM s.arrived_at" in sql


def test_the_walkers_own_exits_need_no_such_guard() -> None:
    """They are lease-conditional already: a run that moved under them
    matches nothing, so the history goes with the move it belonged to."""
    for sql in (
        advance_run_query("r-1", "settle", NOW, {}, LEASE, NOW, [])[0],
        exit_run_query("r-1", "completed", None, None, LEASE, [])[0],
    ):
        assert "WHERE m.current_node" not in sql
        assert "AND wake_at = " in sql


def test_the_open_square_counts_against_the_callers_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeline() appends the open square, so reading `limit` closed rows
    would hand back limit + 1."""
    import app.crm.outreach.runs as runs

    asked: List[int] = []

    class _Reads:
        async def get_run(self, *a: Any) -> EnrollmentRun:
            return _run(node="settle")

        async def run_steps(self, m: str, r: str, limit: int) -> List[RunStep]:
            asked.append(limit)
            return []

        async def get_definition(self, *a: Any) -> Optional[Dict[str, Any]]:
            return None

    patch_accessors(monkeypatch, runs, _Reads())
    patch_accessors(monkeypatch, definitions, _Reads())
    definitions._definitions.clear()
    rows = asyncio.run(runs.run_steps("m1", "w1", "r1", 200))
    assert asked == [199] and len(rows or []) == 1

    # An exited run has no open square, so it keeps the whole limit.
    class _Exited(_Reads):
        async def get_run(self, *a: Any) -> EnrollmentRun:
            return _run(node="settle", status="exited")

    asked.clear()
    patch_accessors(monkeypatch, runs, _Exited())
    asyncio.run(runs.run_steps("m1", "w1", "r1", 200))
    assert asked == [200]


def test_an_unreadable_document_never_blocks_an_eject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before T26 the eject read no document at all. A version row that no
    longer validates must release a run from an archived plan, not park it:
    the closing row is skipped and the exit proceeds."""
    writes = _Writes(_BOARD)
    run = _run(node="hear")

    async def archived(*a: Any, **k: Any) -> Workflow:
        # Built here, not read back through the fake: patch_accessors has
        # already pointed walker.workflow_accessor at `writes`, so calling
        # its get_workflow from inside its own replacement recurses.
        return Workflow(
            id=uuid4(),
            merchant_id="m1",
            name="plan",
            status="archived",
            version=1,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
            definition=_BOARD,
            draft=None,
        )

    async def boom(*a: Any, **k: Any) -> Any:
        raise ValueError("definition shape invalid")

    patch_accessors(monkeypatch, walker, writes)
    monkeypatch.setattr(walker.workflow_accessor, "get_workflow", archived)
    monkeypatch.setattr(walker, "definition_for", boom)
    asyncio.run(walker.walk_run(run))

    ((verb, args),) = writes.calls
    assert verb == "exit" and args[1] == "ejected"
    assert writes.flushes[0]["steps"] == []


def test_a_call_square_records_the_lead_it_dispatched(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """(node_type, dispatch_id) is the provenance pair: node_type says which
    table, dispatch_id says which row. A call hands a lead to buddy's
    dispatch machine and returns; this is the id of that lead."""
    board = {
        **_BOARD,
        "nodes": [
            {"id": "ring", "type": "call", "template_id": "t-1"},
            {"id": "settle", "type": "wait", "minutes": 1440},
        ],
        "edges": [["ring", "settle"]],
    }
    lead = "0f2e9a1c-dead-4beef-8aaa-000000000001"

    async def execute(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        return {f"lead_{node.id}": lead, f"lead_visits_{node.id}": 1}

    monkeypatch.setitem(
        walker.NODE_TYPES,
        "call",
        walker.NODE_TYPES["call"].__class__(
            validate=walker.NODE_TYPES["call"].validate, execute=execute, is_wait=False
        ),
    )
    writes = _Writes(board)
    _walk(monkeypatch, writes, _run(node="ring"))

    ((row,),) = (writes.flushes[0]["steps"],)
    assert row["node_type"] == "call" and row["dispatch_id"] == lead


def test_a_square_that_dispatches_nothing_records_nothing(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """A wait, a condition and a split hand nothing to anyone — NULL there
    means "this square dispatches", not "we lost the id"."""
    writes = _Writes(_BOARD)
    _walk(monkeypatch, writes, _run(context={"reply_hear": "yes", "cart_value": 10}))
    assert [r["dispatch_id"] for r in writes.flushes[0]["steps"]] == [None, None, None]


def test_the_id_comes_from_this_visits_patch_not_the_run_context(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """A revisit mints a fresh lead (uuid5 run:node:visit). Reading the
    merged context would report the PREVIOUS visit's id for the new row —
    so the id is taken from the patch execute() just returned."""
    board = {
        **_BOARD,
        "nodes": [
            {"id": "ring", "type": "call", "template_id": "t-1"},
            {"id": "settle", "type": "wait", "minutes": 1440},
        ],
        "edges": [["ring", "settle"]],
    }
    fresh = "0f2e9a1c-dead-4beef-8aaa-000000000002"

    async def execute(run: Any, node: Any, definition: Any) -> Dict[str, Any]:
        return {f"lead_{node.id}": fresh, f"lead_visits_{node.id}": 2}

    monkeypatch.setitem(
        walker.NODE_TYPES,
        "call",
        walker.NODE_TYPES["call"].__class__(
            validate=walker.NODE_TYPES["call"].validate, execute=execute, is_wait=False
        ),
    )
    writes = _Writes(board)
    # the run already carries the FIRST visit's lead under the same key
    stale = "0f2e9a1c-dead-4beef-8aaa-000000000001"
    _walk(monkeypatch, writes, _run(node="ring", context={"lead_ring": stale}))

    ((row,),) = (writes.flushes[0]["steps"],)
    assert row["dispatch_id"] == fresh


# --- phase 20: a capped call square walks on and says why -------------------


def test_a_capped_call_square_walks_on_and_says_why_on_its_row(
    monkeypatch: pytest.MonkeyPatch, no_goal: None
) -> None:
    """The REAL call word, not a double: at the plan's ceiling it reaches no
    accessor, so it can run under the walker as it is. The token takes the
    plain arrow; the row says `max_calls` with nothing dispatched; the trail
    word is popped before the context write and NOTHING is persisted."""
    board = {
        **_BOARD,
        "nodes": [
            {"id": "ring", "type": "call", "template_id": "t-1"},
            {"id": "settle", "type": "wait", "minutes": 1440},
        ],
        "edges": [["ring", "settle"]],
        "exits": {"max_calls_per_day": 1, "timezone": "Asia/Kolkata"},
    }
    from app.crm.outreach.ceiling import CALLS_TODAY_KEY, today_on
    from app.crm.outreach.schemas import WorkflowExits

    today = today_on(WorkflowExits(max_calls_per_day=1, timezone="Asia/Kolkata"))
    writes = _Writes(board)
    _walk(
        monkeypatch,
        writes,
        _run(
            node="ring",
            context={"lead_visits_ring": 1, CALLS_TODAY_KEY: {"day": today, "n": 1}},
        ),
    )

    ((verb, args),) = writes.calls
    assert verb == "advance" and args[1] == "settle"
    persisted = args[3]
    assert "max_calls_reached" not in persisted, "the answer is computed, never stored"
    assert OUTCOME_KEY not in persisted
    assert persisted["lead_visits_ring"] == 1, "the id counter is untouched"
    assert persisted[CALLS_TODAY_KEY]["n"] == 1, "the ledger means calls PLACED"

    ((row,),) = (writes.flushes[0]["steps"],)
    assert row["node_type"] == "call"
    assert row["outcome"] == "max_calls"
    assert row["dispatch_id"] is None
    assert row["next_node"] == "settle"
