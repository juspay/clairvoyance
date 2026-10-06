"""Mechanical DB access for the line-grant reads of crm_workflow_enrollment.
Self-scoped single statements (module rules §1).
"""

from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.decoders.enrollment import decode_run
from app.crm.outreach.db.queries.grant import (
    get_runs_query,
    parking_squares_query,
    waiting_runs_page_query,
)
from app.crm.outreach.schemas import EnrollmentRun
from app.crm.shared.db import crm_connection


async def get_runs(run_ids: List[str]) -> List[EnrollmentRun]:
    query, values = get_runs_query(run_ids)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]


async def parking_squares() -> Tuple[Any, ...]:
    """(merchants, plans, call squares); each None when no call square waits."""
    query, values = parking_squares_query()
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return tuple(rows[0])  # an aggregate: always one row


async def waiting_runs_page(
    merchants: List[Any],
    workflows: List[Any],
    nodes: List[Any],
    after: Optional[Tuple[Any, str]],
    limit: int,
) -> List[EnrollmentRun]:
    query, values = waiting_runs_page_query(merchants, workflows, nodes, after, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]
