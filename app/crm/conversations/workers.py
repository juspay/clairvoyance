"""The inbox's drain loop, hosted on the walker role (worker_main): every
CRM_INBOX_SWEEP_SECONDS one tick runs the sweeps (sweeps.py). A claim that
returns nothing between ticks keeps the scaffold's idle cadence."""

import time
from typing import List

from app.core.config.static import CRM_INBOX_SWEEP_SECONDS, CRM_WORKER_BATCH
from app.crm.conversations.sweeps import run_sweeps

#: The one item a due tick yields.
TICK = "inbox-sweep"

_last_tick = 0.0


async def claim_inbox_tick(batch: int) -> List[str]:
    """A tick when the sweep interval has passed in this process, else none."""
    global _last_tick
    now = time.monotonic()
    if now - _last_tick < CRM_INBOX_SWEEP_SECONDS:
        return []
    _last_tick = now
    return [TICK]


async def run_inbox_tick(_tick: str) -> None:
    await run_sweeps(CRM_WORKER_BATCH)
