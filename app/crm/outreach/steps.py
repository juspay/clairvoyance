"""Where a run has BEEN (canon T26) — the trail behind the token.

T20 carries the token's POSITION. This file carries the record of the
squares it has left: the buffer the walker fills as it walks, the
vocabulary of how a square came to be walked, and the UNION that every
reader owes (law 3 — the past is N closed rows, the present is the run row
plus ``node_arrived_at``; a consumer that forgets it shows a timeline one
square short of the truth).

Pure, and deliberately so: the walker builds the buffer in memory and hands
it to the statement that moves the run, so the rows commit with the move or
not at all. Nothing here reads the database, and nothing on the hot path
reads crm_workflow_step (law 1).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from app.core.logger import logger
from app.crm.outreach.nodes.context import CUT_SHORT_BY_KEY
from app.crm.outreach.schemas import (
    EnrollmentRun,
    RunStep,
    WorkflowDefinition,
    WorkflowNode,
)

# How a square came to be walked. Vocabulary in code, never a CHECK.
#
#   door    the run was enrolled onto it — its very first square
#   timer   its own alarm came due and the walker claimed it
#   letter  an event woke it before the alarm (entry.py re-arms in place)
#   walk    the square before it, in the same visit, chained into it
ARRIVED_BY_DOOR = "door"
ARRIVED_BY_TIMER = "timer"
ARRIVED_BY_LETTER = "letter"
ARRIVED_BY_WALK = "walk"
ARRIVED_BY = (
    ARRIVED_BY_DOOR,
    ARRIVED_BY_TIMER,
    ARRIVED_BY_LETTER,
    ARRIVED_BY_WALK,
)


@dataclass(frozen=True)
class StepRecord:
    """One CLOSED square, buffered in memory until the visit's one write.

    Never the square the run stands on: that lives on the run row, which is
    what removes the update path entirely (law 2 — rows are never edited).
    """

    node: str
    node_type: str
    arrived_at: datetime
    left_at: datetime
    arrived_by: str
    outcome: Optional[str] = None
    next_node: Optional[str] = None
    attempts: int = 1
    last_error: Optional[str] = None
    dispatch_id: Optional[str] = None
    cut_short_by: Optional[str] = None


def as_rows(steps: Iterable[StepRecord]) -> List[Dict[str, Any]]:
    """PURE: the buffer as the rows ``jsonb_to_recordset`` reads in the
    flush. Timestamps go as ISO text — the column list in the statement
    names them ``timestamptz``, so Postgres parses them back.

    The db layer never sees ``StepRecord``: it takes these dicts the same
    way it takes a run's context."""
    return [
        {
            "node": step.node,
            "node_type": step.node_type,
            "arrived_at": step.arrived_at.isoformat(),
            "left_at": step.left_at.isoformat(),
            "arrived_by": step.arrived_by,
            "outcome": step.outcome,
            "next_node": step.next_node,
            "attempts": step.attempts,
            "last_error": step.last_error,
            "dispatch_id": step.dispatch_id,
            "cut_short_by": _as_uuid(step.cut_short_by),
        }
        for step in steps
    ]


def _as_uuid(value: Optional[str]) -> Optional[str]:
    """PURE: the pointer, or None when it is not one.

    ``cut_short_by`` is a uuid column, and the flush rides the SAME
    statement that moves the token — so a value Postgres cannot cast would
    fail the INSERT, fail the move, and retry the whole visit until the run
    parked. The record must never be the thing that stops a run (law 1's
    spirit: nothing on the hot path depends on the trail). Today every
    letter's id IS a uuid — crm_event_raw.id, migration 051 — so this only
    ever fires on drift, and drops the pointer rather than the run."""
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        logger.warning(f"T26: dropping non-uuid cut_short_by {value!r}")
        return None


def first_arrival(run: EnrollmentRun, cut_short_by: Optional[str]) -> str:
    """PURE: how the FIRST square of this visit came to be walked. Every
    square after it in the same chain arrived by ``walk``.

    ``door`` is decided FIRST, and beats ``letter`` on purpose. A door may
    start a run on any square (phase 15), including a listening one, so a
    letter can cut that very first square short — and answering ``letter``
    there would lose the only fact that is true exactly once per run. It
    loses nothing to rank them this way: the letter is still named, on the
    same row, in ``cut_short_by``. The other order threw ``door`` away and
    said in two columns what one already said.

    ``door`` needs no stored flag: enrol stamps ``node_arrived_at`` in the
    same statement that defaults ``entered_at``, so the two are equal for
    exactly as long as the run has never moved. A run that later returns to
    its start square has a different arrival time and is not re-read as a
    door.

    ``letter`` then beats the clock, for the same reason ``door`` beats it:
    when entry.py woke the run in place it left the letter's id behind
    (nodes/context.CUT_SHORT_BY_KEY), and that is the honest reason the
    visit is happening at all. Recording ``timer`` would be a
    plausible-looking lie, which is the failure mode T26 exists to avoid.

    What is deliberately NOT here: the letter that STARTED the run. It is a
    run-level fact, not a step-level one — the same id on every row of the
    trail — and it already lives in exactly one place, ``context
    .source_event_id``, which is also what source_event_used() dedupes a
    replayed entry event against. Law 3 means every reader already holds
    the run row to append the open square, so reading it costs nothing;
    stamping it on ``cut_short_by`` would instead claim the founding letter
    ENDED the door square, which it did not."""
    if run.node_arrived_at is not None and run.node_arrived_at == run.entered_at:
        return ARRIVED_BY_DOOR
    if cut_short_by:
        return ARRIVED_BY_LETTER
    return ARRIVED_BY_TIMER


def step(
    node: WorkflowNode,
    arrived_at: Optional[datetime],
    left_at: datetime,
    arrived_by: str,
    outcome: Optional[str],
    next_node: Optional[str],
    run: EnrollmentRun,
    cut_short_by: Optional[str],
    first: bool,
    dispatch_id: Optional[str] = None,
) -> List[StepRecord]:
    """PURE: one CLOSED square, as a zero-or-one element buffer.

    Zero when the run pre-dates migration 073 and has no arrival stamp for
    the square it was standing on: that ONE row is dropped rather than
    dated to ``entered_at``, which would be a lie on any screen that says
    "waiting since". Every square after it in a chain has a real arrival.

    ``attempts`` and ``last_error`` belong to the FIRST square of a visit —
    the one the claim was about. The same statement that writes the row
    zeroes them on the run, so the number here is exactly how many claims
    this square cost; squares chained behind it cost part of that same
    claim, so they record one. The known gap is a park: both resume paths
    zero the counter before any row exists, so a parked-then-resumed square
    flushes as one clean attempt (ruled 17 Sep 2026 — a park is visible
    while it is happening, on the run row, not afterwards)."""
    if arrived_at is None:
        return []
    if left_at < arrived_at:
        # 073's CHECK (left_at >= arrived_at) is a FORMAT law and worth
        # keeping — but it must never be the thing that fails a MOVE. The
        # arrival was stamped by whichever pod last advanced this run and
        # the exit by this one, so a badly-skewed clock can invert them.
        # Clamped to a zero-length step, which is visibly odd; raising the
        # INSERT would fail the advance, retry the visit, and eventually
        # park a run over a wall clock. Same rule as _as_uuid: the trail
        # never stops the token.
        logger.warning(
            f"T26: {node.id} left ({left_at}) before it arrived ({arrived_at}) "
            f"— clock skew between walkers; recording a zero-length step"
        )
        left_at = arrived_at
    return [
        StepRecord(
            node=node.id,
            node_type=node.type,
            arrived_at=arrived_at,
            left_at=left_at,
            arrived_by=arrived_by,
            outcome=outcome,
            next_node=next_node,
            attempts=max(run.attempts, 1) if first else 1,
            last_error=run.last_error if first else None,
            dispatch_id=dispatch_id,
            cut_short_by=cut_short_by if first else None,
        )
    ]


def closing(
    run: EnrollmentRun,
    definition: Optional[WorkflowDefinition],
    outcome: str,
    now: Optional[datetime] = None,
    cut_short_by: Optional[str] = None,
) -> List[StepRecord]:
    """PURE: the single square a run was standing on when something ended
    it without a walk — timed_out, ejected, or a goal tier (the walker's
    three), and the goal-cancel on the event side, which is trap 3: the one
    writer outside the walker that ends a run, and therefore the one that
    would silently lose the final square of every CONVERTED run.

    No execute ran and no edge was taken, so ``next_node`` is NULL: the run
    ended here.

    ``cut_short_by`` is the letter that ENDED the square. An ender that
    holds it passes it — the goal-cancel passes the event in hand, because
    that letter is what cut the square short: reading only the context
    would file a NULL, or worse a STALE wake letter still waiting to be
    flushed. The walker's three exits name no letter and keep the context
    read."""
    node = None
    if definition is not None:
        node = next((n for n in definition.nodes if n.id == run.current_node), None)
    if node is None:
        return []
    pointer = cut_short_by or run.context.get(CUT_SHORT_BY_KEY)
    return step(
        node,
        run.node_arrived_at,
        now or datetime.now(timezone.utc),
        first_arrival(run, str(pointer) if pointer else None),
        outcome,
        None,
        run,
        str(pointer) if pointer else None,
        first=True,
    )


def timeline(
    run: EnrollmentRun,
    closed: Iterable[RunStep],
    definition: Optional[WorkflowDefinition] = None,
) -> List[RunStep]:
    """The union law (law 3): the closed rows, then the square the run is
    standing on right now.

    The open square is not in the table — only closed steps are written —
    so every reader has to append it or show a timeline one square short.
    ``left_at is None`` is what says "still here"; its ``attempts`` and
    ``last_error`` come off the run row, where they are live.

    An exited run has no open square. Neither has a run that pre-dates
    migration 073 and has not moved since: without an arrival stamp there
    is nothing honest to say about when it got there, so it is left out
    rather than dated to ``entered_at``.

    The open square's ``arrived_by`` is how the token physically GOT here —
    ``door`` for a run that has never moved, else ``walk``. When it closes,
    the flush records what finally moved it (``timer`` or ``letter``), so
    the two can differ by design.
    """
    steps = list(closed)
    if run.status == "exited" or run.node_arrived_at is None:
        return steps
    node_type = ""
    if definition is not None:
        node = next((n for n in definition.nodes if n.id == run.current_node), None)
        if node is not None:
            node_type = node.type
    steps.append(
        RunStep(
            node=run.current_node,
            node_type=node_type,
            arrived_at=run.node_arrived_at,
            left_at=None,
            arrived_by=(
                ARRIVED_BY_DOOR
                if run.node_arrived_at == run.entered_at
                else ARRIVED_BY_WALK
            ),
            attempts=run.attempts,
            last_error=run.last_error,
            workflow_version=run.workflow_version,
        )
    )
    return steps
