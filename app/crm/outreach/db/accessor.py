"""Outreach accessor — mechanical DB access ONLY (module rules §1).

Executes exactly one query builder per function. Admission guards, node
execution and publish laws live in the logic files (plans.py, enrol.py,
walker.py, entry.py). Functions taking a ``conn`` run inside the caller's
transaction; standalone single statements self-scope their own connection
— the same shape as every other module's accessor.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import asyncpg

from app.crm.outreach.db.decoder import (
    decode_run,
    decode_workflow,
    decode_workflow_summary,
)
from app.crm.outreach.db.queries import (
    admission_facts_query,
    advance_run_query,
    cancel_open_runs_query,
    claim_due_runs_query,
    exit_run_query,
    get_workflow_query,
    insert_enrollment_query,
    insert_workflow_query,
    list_runs_query,
    list_workflows_query,
    live_workflows_query,
    occupied_nodes_query,
    park_run_query,
    patch_open_run_query,
    publish_workflow_query,
    record_run_error_query,
    resume_run_on_event_query,
    resume_run_query,
    set_workflow_status_query,
    source_event_used_query,
    sweep_exited_runs_query,
    update_draft_query,
)
from app.crm.outreach.schemas import EnrollmentRun, Workflow, WorkflowSummary
from app.crm.shared.db import crm_connection


async def insert_workflow(
    merchant_id: str, name: str, draft: Dict[str, Any], created_by: Optional[str]
) -> Workflow:
    query, values = insert_workflow_query(merchant_id, name, draft, created_by)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    assert row is not None  # INSERT ... RETURNING always yields the row
    return decode_workflow(row)


async def update_draft(
    merchant_id: str, workflow_id: str, draft: Dict[str, Any]
) -> Optional[Workflow]:
    query, values = update_draft_query(merchant_id, workflow_id, draft)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_workflow(row) if row else None


async def get_workflow(merchant_id: str, workflow_id: str) -> Optional[Workflow]:
    query, values = get_workflow_query(merchant_id, workflow_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_workflow(row) if row else None


async def list_workflows(
    merchant_id: str, limit: int, offset: int
) -> List[WorkflowSummary]:
    query, values = list_workflows_query(merchant_id, limit, offset)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_workflow_summary(row) for row in rows]


async def workflow_for_publish(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str
) -> Optional[Workflow]:
    """Runs inside the caller's publish atom (conn param) — the
    validate-then-copy decision must read the same draft it publishes."""
    query, values = get_workflow_query(merchant_id, workflow_id)
    row = await conn.fetchrow(query, *values)
    return decode_workflow(row) if row else None


async def occupied_nodes(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str
) -> List[str]:
    query, values = occupied_nodes_query(merchant_id, workflow_id)
    rows = await conn.fetch(query, *values)
    return [row["current_node"] for row in rows]


async def apply_publish(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str
) -> Optional[Workflow]:
    query, values = publish_workflow_query(merchant_id, workflow_id)
    row = await conn.fetchrow(query, *values)
    return decode_workflow(row) if row else None


async def set_workflow_status(
    merchant_id: str, workflow_id: str, status: str
) -> Optional[Workflow]:
    query, values = set_workflow_status_query(merchant_id, workflow_id, status)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_workflow(row) if row else None


async def live_workflows(merchant_id: str) -> List[Workflow]:
    query, values = live_workflows_query(merchant_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_workflow(row) for row in rows]


async def admission_facts(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str, customer_id: str
) -> Dict[str, Any]:
    query, values = admission_facts_query(merchant_id, workflow_id, customer_id)
    row = await conn.fetchrow(query, *values)
    return {
        "runs": row["runs"] if row else 0,
        "latest_entered_at": row["latest_entered_at"] if row else None,
    }


async def source_event_used(
    conn: asyncpg.Connection,
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    source_event_id: str,
) -> bool:
    query, values = source_event_used_query(
        merchant_id, workflow_id, customer_id, source_event_id
    )
    row = await conn.fetchrow(query, *values)
    return bool(row["used"]) if row else False


async def insert_enrollment(
    conn: asyncpg.Connection,
    merchant_id: str,
    workflow_id: str,
    workflow_version: int,
    customer_id: str,
    current_node: str,
    wake_at: datetime,
    context: Dict[str, Any],
    enrollment_key: str,
) -> EnrollmentRun:
    query, values = insert_enrollment_query(
        merchant_id,
        workflow_id,
        workflow_version,
        customer_id,
        current_node,
        wake_at,
        context,
        enrollment_key,
    )
    row = await conn.fetchrow(query, *values)
    assert row is not None  # INSERT ... RETURNING always yields the row
    return decode_run(row)


async def claim_due_runs(limit: int, lease_seconds: int) -> List[EnrollmentRun]:
    """One statement — the lock, the lease push and the attempts count
    commit together; Postgres runs it atomically, no wrapper needed."""
    query, values = claim_due_runs_query(limit, lease_seconds)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]


async def advance_run(
    run_id: str, current_node: str, wake_at: datetime, context: Dict[str, Any]
) -> None:
    query, values = advance_run_query(run_id, current_node, wake_at, context)
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def exit_run(
    run_id: str,
    exit_reason: str,
    current_node: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    query, values = exit_run_query(run_id, exit_reason, current_node, context)
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def park_run(run_id: str, last_error: str) -> None:
    query, values = park_run_query(run_id, last_error)
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def record_run_error(run_id: str, last_error: str, retry_in_seconds: int) -> None:
    query, values = record_run_error_query(run_id, last_error, retry_in_seconds)
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def resume_run_on_event(
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    node_id: str,
    context_patch: Dict[str, Any],
) -> None:
    query, values = resume_run_on_event_query(
        merchant_id, workflow_id, customer_id, node_id, context_patch
    )
    async with crm_connection() as conn:
        await conn.execute(query, *values)


async def patch_open_run(
    merchant_id: str,
    workflow_id: str,
    enrollment_key: str,
    entry_node: str,
    event_id: str,
    patch: Dict[str, Any],
    accumulate: bool,
    max_field: Optional[str],
    max_value: Optional[float],
    debounce_minutes: float,
) -> bool:
    """True when an open run on the entry square took the repeat."""
    query, values = patch_open_run_query(
        merchant_id,
        workflow_id,
        enrollment_key,
        entry_node,
        event_id,
        patch,
        accumulate,
        max_field,
        max_value,
        debounce_minutes,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def cancel_open_runs(
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    exit_reason: str,
    occurred_at: Optional[datetime] = None,
) -> int:
    query, values = cancel_open_runs_query(
        merchant_id, workflow_id, customer_id, exit_reason, occurred_at
    )
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return len(rows)


async def list_runs(
    merchant_id: str,
    workflow_id: str,
    status: Optional[str],
    limit: int,
    offset: int,
) -> List[EnrollmentRun]:
    query, values = list_runs_query(merchant_id, workflow_id, status, limit, offset)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]


async def resume_run(
    merchant_id: str, workflow_id: str, run_id: str
) -> Optional[EnrollmentRun]:
    query, values = resume_run_query(merchant_id, workflow_id, run_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_run(row) if row else None


async def sweep_exited_runs(cutoff: datetime, batch: int) -> int:
    query, values = sweep_exited_runs_query(cutoff, batch)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return len(rows)
