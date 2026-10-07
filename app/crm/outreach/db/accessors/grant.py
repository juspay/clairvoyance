"""Mechanical DB access for the line-grant reads of crm_workflow_enrollment.
Self-scoped single statements (module rules §1).
"""

from typing import List, Optional, Tuple

from app.crm.outreach.db.decoders.enrollment import decode_run
from app.crm.outreach.db.queries.grant import (
    get_runs_query,
    parked_call_squares_query,
    wake_parked_calls_batch_query,
    wake_runs_query,
)
from app.crm.outreach.schemas import EnrollmentRun
from app.crm.shared.db import crm_connection


async def get_runs(run_ids: List[str]) -> List[EnrollmentRun]:
    query, values = get_runs_query(run_ids)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]


async def get_run(run_id: str) -> Tuple[Optional[EnrollmentRun], Optional[str]]:
    """The run and its plan's status, in one read."""
    query, values = get_runs_query([run_id])
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return (decode_run(row), row["plan_status"]) if row else (None, None)


async def wake_runs(runs: List[Tuple[str, str]]) -> int:
    query, values = wake_runs_query(runs)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return len(rows)


# A heal wakes at most this many runs per statement.
WAKE_BATCH = 10_000


async def wake_parked_calls_after_loss() -> int:
    """Batch loop: the squares once, then WAKE_BATCH runs per statement."""
    query, values = parked_call_squares_query()
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    squares = [
        (r["merchant_id"], r["workflow_id"], r["version"], r["node"]) for r in rows
    ]
    woken, after = 0, "00000000-0000-0000-0000-000000000000"
    while squares:
        query, values = wake_parked_calls_batch_query(squares, after, WAKE_BATCH)
        async with crm_connection() as conn:
            ids = sorted(str(r["id"]) for r in await conn.fetch(query, *values))
        woken += len(ids)
        if len(ids) < WAKE_BATCH:
            break
        after = ids[-1]
    return woken
