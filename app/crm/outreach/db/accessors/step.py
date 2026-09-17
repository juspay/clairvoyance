"""Mechanical DB access for crm_workflow_step (T26) — one table, one file. The table is
append-only by trigger (migration 073) and written only by the statements
that move a token (queries/step.flush_arm); the timeline read is the ONE
consumer.
"""

from typing import List

from app.crm.outreach.db.decoders.step import decode_step
from app.crm.outreach.db.queries.step import run_steps_query
from app.crm.outreach.schemas import RunStep
from app.crm.shared.db import crm_connection


async def run_steps(merchant_id: str, run_id: str, limit: int) -> List[RunStep]:
    """One run's closed squares, oldest first — the LATEST ``limit`` of
    them: the statement scans the index newest-first (the cut lands at the
    OLD end, where a timeline reader expects it) and they are reversed
    here, so the caller always gets chronological order. The OPEN square
    is not here — steps.timeline() unions it in from the run row (canon
    T26, law 3)."""
    query, values = run_steps_query(merchant_id, run_id, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_step(row) for row in reversed(rows)]
