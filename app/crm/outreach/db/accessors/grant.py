"""Mechanical DB access for the line-grant reads of crm_workflow_enrollment.
Self-scoped single statements (module rules §1).
"""

from typing import List

from app.crm.outreach.db.decoders.enrollment import decode_run
from app.crm.outreach.db.queries.grant import (
    get_runs_query,
    wake_runs_query,
)
from app.crm.outreach.schemas import EnrollmentRun
from app.crm.shared.db import crm_connection


async def get_runs(run_ids: List[str]) -> List[EnrollmentRun]:
    query, values = get_runs_query(run_ids)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]


async def wake_runs(run_ids: List[str]) -> int:
    query, values = wake_runs_query(run_ids)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return len(rows)
