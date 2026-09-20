"""Run operations (W2/W3 operability) — the reads and the one human verb
canon T20 promises: runs are visible ("last_error readable on the
merchant's screen"), parked runs are "held for the merchant to see and
RESUME — errors never silently discard a run", and exited rows age out
("the retention sweep reads exited_at ... most of what keeps the hot
table small").

Trivial logic today — each function is one accessor call — but api.py
and workers.py cross through here, never db/ directly (the contracts
seam every module keeps).
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app.core.config.static import (
    CRM_RUN_RETENTION_DAYS,
    CRM_RUN_SWEEP_BATCH_SIZE,
)
from app.core.logger import logger
from app.crm.outreach import steps as steps_log
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    step as step_accessor,
)
from app.crm.outreach.definitions import definition_for
from app.crm.outreach.schemas import (
    CustomerRun,
    EnrollmentRun,
    RunCall,
    RunRow,
    RunStep,
)
from app.crm.record.contracts import event_topics
from app.database.accessor import (
    get_leads_by_enrollment_id,
)

_LISTABLE_STATUSES = ("waiting", "parked", "exited")


async def list_runs(
    merchant_id: str,
    workflow_id: str,
    status: Optional[str],
    limit: int,
    offset: int,
    node: Optional[str] = None,
    version: Optional[int] = None,
    exit_reason: Optional[str] = None,
    search: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    anchor: Optional[Tuple[datetime, str]] = None,
) -> Tuple[List[RunRow], int]:
    """One page of a plan's runs and how many match in all. ``anchor`` is
    the newest (entered_at, id) the client's first page saw: later pages
    read at or before it, so runs entering meanwhile cannot shift them."""
    if status is not None and status not in _LISTABLE_STATUSES:
        raise ValueError(f"unknown run status: {status}")
    search = (search or "").strip() or None
    return await enrollment_accessor.list_runs(
        merchant_id,
        workflow_id,
        status,
        limit,
        offset,
        node,
        version,
        exit_reason,
        search,
        since,
        until,
        anchor[0] if anchor else None,
        anchor[1] if anchor else None,
    )


async def resume_run(
    merchant_id: str, workflow_id: str, run_id: str
) -> Optional[EnrollmentRun]:
    """Revive one parked run: wake now, failure counter forgiven. Returns
    None when the run isn't parked (or isn't this merchant's) — the
    walker claims it on its next tick."""
    run = await enrollment_accessor.resume_run(merchant_id, workflow_id, run_id)
    if run:
        logger.info(f"run resumed by operator: {run_id} (merchant {merchant_id})")
    return run


async def run_steps(
    merchant_id: str, workflow_id: str, run_id: str, limit: int
) -> Optional[List[RunStep]]:
    """Where this run has BEEN, oldest first (canon T26) — the closed
    squares UNIONED with the one it stands on now (law 3: the past is N
    rows, the present is the run row). None when the run is not this
    merchant's or not this plan's, which the route turns into a 404.

    The pinned document is read for the open square's node TYPE — the
    closed rows carry their own, denormalised, so forty steps render
    without resolving forty documents. The read is guarded the way the
    walker guards it on the eject path: a version row that no longer
    validates must not turn a timeline into a 500 — the open square just
    renders with an empty type."""
    run = await enrollment_accessor.get_run(merchant_id, workflow_id, run_id)
    if run is None:
        return None
    # The open square counts against the caller's limit: timeline() appends
    # it, so reading `limit` closed rows would return limit + 1.
    open_square = run.status != "exited" and run.node_arrived_at is not None
    closed = await step_accessor.run_steps(
        merchant_id, run_id, max(0, limit - 1) if open_square else limit
    )
    try:
        definition = await definition_for(run)
    except Exception as e:
        logger.warning(
            f"run_steps: run {run_id} rendered without its open square's "
            f"type — definition v{run.workflow_version} unreadable: {e}"
        )
        definition = None
    steps = steps_log.timeline(run, closed, definition)
    return await name_letters(merchant_id, run, steps)


async def name_letters(
    merchant_id: str, run: EnrollmentRun, steps: List[RunStep]
) -> List[RunStep]:
    """Fill ``event_topic`` on the rows a letter explains: the door row
    (the founding letter, context.source_event_id), every row a letter cut
    short (cut_short_by), and the closing row of a run a goal ended
    (context.goal.topic, already a name). One read for all the ids."""
    goal = run.context.get("goal")
    founding = run.context.get("source_event_id")
    wanted = {str(s.cut_short_by) for s in steps if s.cut_short_by}
    if isinstance(founding, str):
        wanted.add(founding)
    topics = await event_topics(merchant_id, sorted(wanted)) if wanted else {}
    for i, step in enumerate(steps):
        if step.cut_short_by and str(step.cut_short_by) in topics:
            step.event_topic = topics[str(step.cut_short_by)]
        elif i == 0 and step.arrived_by == "door" and isinstance(founding, str):
            step.event_topic = topics.get(founding)
        elif (
            step.left_at is not None
            and step.next_node is None
            and isinstance(goal, dict)
            and step.outcome == run.exit_reason
            and run.exit_reason in ("goal_met", "withdrawn")
        ):
            step.event_topic = goal.get("topic")
    return steps


async def run_calls(
    merchant_id: str, workflow_id: str, run_id: str
) -> Optional[List[RunCall]]:
    """Every call the run placed, in the order they were queued. None when
    the run is not this plan's (or not this merchant's)."""
    run = await enrollment_accessor.get_run(merchant_id, workflow_id, run_id)
    if run is None:
        return None
    leads = await get_leads_by_enrollment_id(
        merchant_id, str(run.id), run.entered_at, run.exited_at
    )
    calls: List[RunCall] = []
    for lead in leads:
        payload = lead.payload or {}
        node = payload.get("current_node")
        duration = (
            int((lead.call_end_time - lead.call_initiated_time).total_seconds())
            if lead.call_end_time
            and lead.call_initiated_time
            and lead.call_end_time > lead.call_initiated_time
            else None
        )
        calls.append(
            RunCall(
                lead_id=lead.id,
                node=node if isinstance(node, str) else None,
                status=getattr(lead.status, "value", str(lead.status)),
                outcome=lead.outcome,
                next_attempt_at=lead.next_attempt_at,
                call_initiated_time=lead.call_initiated_time,
                call_end_time=lead.call_end_time,
                duration_seconds=duration,
                attempt_count=lead.attempt_count,
                template=lead.template,
                call_id=lead.call_id,
                cost=lead.cost,
            )
        )
    return calls


async def customer_runs(
    merchant_id: str, customer_id: str, limit: int
) -> List[CustomerRun]:
    """The customer's journey: her runs across every plan, in the order
    they began (rollout phase 09)."""
    return await enrollment_accessor.customer_runs(merchant_id, customer_id, limit)


async def run_retention_sweep_tick() -> None:
    """One pass of the retention sweep — housekeeping on the walker pod
    (workers.py runs it hourly beside the drain loop). Cheap
    when there is nothing to delete (one probe of the exited_at partial
    index); batched so a large backlog never holds a long lock —
    leftovers go next pass."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=CRM_RUN_RETENTION_DAYS)
        swept = await enrollment_accessor.sweep_exited_runs(
            cutoff, CRM_RUN_SWEEP_BATCH_SIZE
        )
        if swept:
            logger.info(
                f"run retention sweep: removed {swept} exited runs "
                f"older than {CRM_RUN_RETENTION_DAYS}d"
            )
    except Exception as e:
        # The sweep loop keeps calling us; a bad pass must not kill the pod.
        logger.error(f"run retention sweep failed: {e}")
