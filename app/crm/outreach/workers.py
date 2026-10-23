"""The walker role's and the broadcaster role's callables for the shared
drain loop (registered in app/crm/worker_main.py under CRM_ROLE=walker and
CRM_ROLE=broadcaster). Loop mechanics are the scaffold's; this module
supplies each claim and handler.

The broadcaster runs ONE loop with two jobs (design/worker-runtime.md: no
pod runs two loops): a due freeze first — an audience must be frozen before
it can be admitted — else one admission page. Its reaper (canon T18 col 11)
and its close job (phase 4: sent -> closed once the reporting window ends)
are housekeeping on the same claim, the retention sweep's pattern below.

The retention sweep (canon T20 exited_at: "exited rows age out") is
housekeeping on the same loop, not a second one: the claim callable runs
one sweep pass every CRM_RUN_SWEEP_INTERVAL_SECONDS before it claims. The table's owner keeps its own
table small, and the pod still runs exactly one loop.

The entry-rules CONSUMER is deliberately not here: a consumer is a function
the event worker's pass calls (entry.py), never a loop of ours.
"""

import time
from typing import List, Literal, NamedTuple

from app.core.config.static import CRM_RUN_SWEEP_INTERVAL_SECONDS
from app.crm.outreach import walker
from app.crm.outreach.admission import (
    admission_candidates,
    admit_page,
    release_stale_claims,
)
from app.crm.outreach.broadcasts import (
    claim_due_freezes,
    close_due_broadcasts,
    freeze_broadcast,
)
from app.crm.outreach.runs import run_retention_sweep_tick
from app.crm.outreach.schemas import Broadcast, EnrollmentRun
from app.crm.outreach.walker import walk_run

_last_sweep_at = float("-inf")
_last_reap_at = float("-inf")

#: How often the broadcaster's housekeeping runs: the reaper looks for
#: claims a dead pod left, and the close job for reports gone final.
REAP_INTERVAL_SECONDS = 60.0


async def claim_due_runs(batch: int) -> List[EnrollmentRun]:
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at >= CRM_RUN_SWEEP_INTERVAL_SECONDS:
        _last_sweep_at = now
        await run_retention_sweep_tick()
    return await walker.claim_due_runs(batch)


class BroadcasterJob(NamedTuple):
    """One unit of the broadcaster's work: freeze a broadcast's audience, or
    admit one page of it."""

    kind: Literal["freeze", "admit"]
    broadcast: Broadcast


async def claim_broadcaster_work(batch: int) -> List[BroadcasterJob]:
    """Due freezes first; when none, broadcasts with admission work."""
    global _last_reap_at
    now = time.monotonic()
    if now - _last_reap_at >= REAP_INTERVAL_SECONDS:
        _last_reap_at = now
        await release_stale_claims()
        await close_due_broadcasts()
    frozen = await claim_due_freezes(batch)
    if frozen:
        return [BroadcasterJob("freeze", broadcast) for broadcast in frozen]
    return [
        BroadcasterJob("admit", broadcast)
        for broadcast in await admission_candidates(batch)
    ]


async def do_broadcaster_work(job: BroadcasterJob) -> None:
    if job.kind == "freeze":
        await freeze_broadcast(job.broadcast)
    else:
        await admit_page(job.broadcast)


__all__ = [
    "claim_due_runs",
    "walk_run",
    "claim_broadcaster_work",
    "do_broadcaster_work",
]
