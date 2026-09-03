"""SQL builders for crm_workflow_version (T25, ADR 0023) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). $1 placeholders only — every value
parameterized.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import ENROLLMENT_TABLE, VERSION_TABLE


def insert_version_query(
    merchant_id: str,
    workflow_id: str,
    version: int,
    definition: Dict[str, Any],
    on_publish: str,
    published_by: Optional[str],
) -> Tuple[str, List[Any]]:
    """One immutable row per publish (ADR 0023, phase 11): the document
    that became live, under the mode it declared. No ON CONFLICT — a
    second row for the same version is a bug the unique index must
    surface, never a merge."""
    query = f"""
        INSERT INTO {VERSION_TABLE}
            (merchant_id, workflow_id, version, definition, on_publish, published_by)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        RETURNING id
    """
    return query, [
        merchant_id,
        workflow_id,
        version,
        json.dumps(definition),
        on_publish,
        published_by,
    ]


def get_definition_query(
    merchant_id: str, workflow_id: str, version: int
) -> Tuple[str, List[Any]]:
    """The pinned document, by the pin (phase 12's read)."""
    query = f"""
        SELECT definition
        FROM {VERSION_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2 AND version = $3
    """
    return query, [merchant_id, workflow_id, version]


def lock_template_shared_query(key: int) -> Tuple[str, List[Any]]:
    """The pinning side of the template lock (shared/locks.py): held
    SHARED for the rest of the transaction, so pinners never block one
    another and a retirement (EXCLUSIVE) waits for them to commit."""
    return "SELECT pg_advisory_xact_lock_shared($1::bigint)", [key]


def list_versions_query(merchant_id: str, workflow_id: str) -> Tuple[str, List[Any]]:
    """The versions list (rollout phase 14): every published document,
    newest first, with the open runs still pinned to it — what to migrate
    from. Both tables are outreach's."""
    query = f"""
        SELECT v.version, v.on_publish, v.published_by, v.published_at,
               (SELECT count(*) FROM {ENROLLMENT_TABLE} e
                 WHERE e.merchant_id = v.merchant_id
                   AND e.workflow_id = v.workflow_id
                   AND e.workflow_version = v.version
                   AND e.status <> 'exited') AS open_runs
        FROM {VERSION_TABLE} v
        WHERE v.merchant_id = $1 AND v.workflow_id = $2
        ORDER BY v.version DESC
    """
    return query, [merchant_id, workflow_id]
