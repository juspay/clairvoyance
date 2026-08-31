"""The walker role's two callables for the shared drain loop (registered
in app/crm/worker_main.py under CRM_ROLE=walker). Loop mechanics are the
scaffold's; this module supplies the claim (the wake_at lease push, canon
T20) and the handler (walker.py).

The retention sweep (canon T20 exited_at: "exited rows age out") is
housekeeping on the same loop, not a second one: the claim callable runs
one sweep pass every CRM_RUN_SWEEP_INTERVAL_SECONDS before it claims. The table's owner keeps its own
table small, and the pod still runs exactly one loop.

The entry-rules CONSUMER is deliberately not here: a consumer is a function
the event worker's pass calls (entry.py), never a loop of ours.
"""

import time
from typing import List

from app.core.config.static import CRM_RUN_SWEEP_INTERVAL_SECONDS
from app.crm.outreach import walker
from app.crm.outreach.runs import run_retention_sweep_tick
from app.crm.outreach.schemas import EnrollmentRun
from app.crm.outreach.walker import walk_run

_last_sweep_at = float("-inf")


async def claim_due_runs(batch: int) -> List[EnrollmentRun]:
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at >= CRM_RUN_SWEEP_INTERVAL_SECONDS:
        _last_sweep_at = now
        await run_retention_sweep_tick()
    return await walker.claim_due_runs(batch)


__all__ = ["claim_due_runs", "walk_run"]
