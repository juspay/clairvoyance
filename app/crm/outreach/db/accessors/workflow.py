"""Mechanical DB access for crm_workflow (T19) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). Two shapes, by signature:
a ``conn`` parameter runs inside the caller's atom; no parameter self-scopes
one statement (module rules §1).
"""

from typing import Any, Dict, List, Optional

import asyncpg

from app.crm.outreach.db.decoders.workflow import (
    decode_workflow,
    decode_workflow_summary,
)
from app.crm.outreach.db.queries.workflow import (
    get_workflow_query,
    insert_workflow_query,
    list_workflows_query,
    live_plans_naming_template_query,
    live_workflows_query,
    publish_workflow_query,
    set_workflow_status_query,
    update_draft_query,
)
from app.crm.outreach.schemas import (
    Workflow,
    WorkflowSummary,
)
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


async def live_plans_naming_template(merchant_id: str, channel: str, name: str) -> int:
    query, values = live_plans_naming_template_query(merchant_id, channel, name)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return int(row["plans"]) if row is not None else 0
