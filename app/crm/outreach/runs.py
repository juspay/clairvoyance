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
from typing import List, Optional

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
    RunStep,
    WorkflowRunSummary,
)

_LISTABLE_STATUSES = ("waiting", "parked", "exited")


async def list_runs(
    merchant_id: str,
    workflow_id: str,
    status: Optional[str],
    limit: int,
    offset: int,
) -> List[EnrollmentRun]:
    if status is not None and status not in _LISTABLE_STATUSES:
        raise ValueError(f"unknown run status: {status}")
    return await enrollment_accessor.list_runs(
        merchant_id, workflow_id, status, limit, offset
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
    return steps_log.timeline(run, closed, definition)


async def workflow_summary(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> WorkflowRunSummary:
    """The plan's report over a window of entered_at (rollout phase 09)."""
    return await enrollment_accessor.workflow_summary(
        merchant_id, workflow_id, since, until
    )


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
