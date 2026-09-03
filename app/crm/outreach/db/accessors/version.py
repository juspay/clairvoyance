"""Mechanical DB access for crm_workflow_version (T25, ADR 0023) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). Two shapes, by signature:
a ``conn`` parameter runs inside the caller's atom; no parameter self-scopes
one statement (module rules §1).
"""

from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.crm.outreach.db.decoders.version import (
    decode_version,
)
from app.crm.outreach.db.queries.version import (
    get_definition_query,
    insert_version_query,
    list_versions_query,
    lock_template_shared_query,
)
from app.crm.outreach.schemas import (
    WorkflowVersion,
)
from app.crm.shared.db import crm_connection
from app.crm.shared.decode import jsonb_value as decode_jsonb
from app.crm.shared.locks import template_lock_key


async def insert_version(
    conn: asyncpg.Connection,
    merchant_id: str,
    workflow_id: str,
    version: int,
    definition: Dict[str, Any],
    on_publish: str,
    published_by: Optional[str],
) -> None:
    """Runs inside the publish atom (conn param): the row shares the
    publish's fate."""
    query, values = insert_version_query(
        merchant_id, workflow_id, version, definition, on_publish, published_by
    )
    await conn.execute(query, *values)


async def get_definition(
    merchant_id: str, workflow_id: str, version: int
) -> Optional[Dict[str, Any]]:
    """The pinned document, or None when no such version row exists."""
    query, values = get_definition_query(merchant_id, workflow_id, version)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    if row is None:
        return None
    definition = decode_jsonb(row["definition"])
    return definition if isinstance(definition, dict) else None


async def lock_templates_shared(
    conn: asyncpg.Connection, merchant_id: str, templates: List[Tuple[str, str]]
) -> None:
    """Inside the caller's atom: take the template lock SHARED for every
    (channel, name) the document being pinned sends. In a stable order,
    so two pinners of the same document never wait on each other's second
    key (they never do anyway — shared locks do not conflict — but a
    stable order is free)."""
    for channel, name in sorted(set(templates)):
        query, values = lock_template_shared_query(
            template_lock_key(merchant_id, channel, name)
        )
        await conn.execute(query, *values)


async def pinned_definition(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str, version: int
) -> Optional[Dict[str, Any]]:
    """The pinned document inside the caller's atom (conn param) —
    migrate-forward validates the exact documents it moves runs between."""
    query, values = get_definition_query(merchant_id, workflow_id, version)
    row = await conn.fetchrow(query, *values)
    if row is None:
        return None
    definition = decode_jsonb(row["definition"])
    return definition if isinstance(definition, dict) else None


async def list_versions(merchant_id: str, workflow_id: str) -> List[WorkflowVersion]:
    query, values = list_versions_query(merchant_id, workflow_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_version(row) for row in rows]
