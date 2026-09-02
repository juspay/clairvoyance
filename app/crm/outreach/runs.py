"""Run operations (W2/W3 operability) — the reads and the one human verb
canon T20 promises: runs are visible ("last_error readable on the
merchant's screen"), parked runs are "held for the merchant to see and
RESUME — errors never silently discard a run", and exited rows age out
("the retention sweep reads exited_at ... most of what keeps the hot
table small").

What you DO to a run, one accessor call apiece; what a pile of runs
MEANS is counts.py. api.py and workers.py cross through this file, never
db/ directly (the contracts seam every module keeps).
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.core.config.static import (
    CRM_RUN_RETENTION_DAYS,
    CRM_RUN_SWEEP_BATCH_SIZE,
)
from app.core.logger import logger
from app.crm.outreach.db import accessor
from app.crm.outreach.schemas import EnrollmentRun

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
    return await accessor.list_runs(merchant_id, workflow_id, status, limit, offset)


async def resume_run(
    merchant_id: str, workflow_id: str, run_id: str
) -> Optional[EnrollmentRun]:
    """Revive one parked run: wake now, failure counter forgiven. Returns
    None when the run isn't parked (or isn't this merchant's) — the
    walker claims it on its next tick."""
    run = await accessor.resume_run(merchant_id, workflow_id, run_id)
    if run:
        logger.info(f"run resumed by operator: {run_id} (merchant {merchant_id})")
    return run


async def run_retention_sweep_tick() -> None:
    """One pass of the retention sweep — housekeeping on the walker pod
    (workers.py runs it hourly beside the drain loop). Cheap
    when there is nothing to delete (one probe of the exited_at partial
    index); batched so a large backlog never holds a long lock —
    leftovers go next pass."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=CRM_RUN_RETENTION_DAYS)
        swept = await accessor.sweep_exited_runs(cutoff, CRM_RUN_SWEEP_BATCH_SIZE)
        if swept:
            logger.info(
                f"run retention sweep: removed {swept} exited runs "
                f"older than {CRM_RUN_RETENTION_DAYS}d"
            )
    except Exception as e:
        # The sweep loop keeps calling us; a bad pass must not kill the pod.
        logger.error(f"run retention sweep failed: {e}")
