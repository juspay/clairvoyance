"""SQL builders for crm_workflow (T19) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). $1 placeholders only — every value
parameterized.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import WORKFLOW_TABLE

_WORKFLOW_SUMMARY_COLUMNS = """
    id, merchant_id, name, status, version, created_by,
    created_at, updated_at
"""


# Detail adds the two documents — the list never fetches them.
_WORKFLOW_COLUMNS = _WORKFLOW_SUMMARY_COLUMNS + ", definition, draft"


def insert_workflow_query(
    merchant_id: str, name: str, draft: Dict[str, Any], created_by: Optional[str]
) -> Tuple[str, List[Any]]:
    """A new plan is born as a draft — the walker cannot see it until
    publish copies draft -> definition."""
    query = f"""
        INSERT INTO {WORKFLOW_TABLE} (merchant_id, name, draft, created_by)
        VALUES ($1, $2, $3::jsonb, $4)
        RETURNING {_WORKFLOW_COLUMNS}
    """
    return query, [merchant_id, name, json.dumps(draft), created_by]


def update_draft_query(
    merchant_id: str, workflow_id: str, draft: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {WORKFLOW_TABLE}
        SET draft = $3::jsonb, updated_at = now()
        WHERE merchant_id = $1 AND id = $2
        RETURNING {_WORKFLOW_COLUMNS}
    """
    return query, [merchant_id, workflow_id, json.dumps(draft)]


def get_workflow_query(merchant_id: str, workflow_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_WORKFLOW_COLUMNS}
        FROM {WORKFLOW_TABLE}
        WHERE merchant_id = $1 AND id = $2
    """
    return query, [merchant_id, workflow_id]


def list_workflows_query(
    merchant_id: str, limit: int, offset: int
) -> Tuple[str, List[Any]]:
    """entry_topic is the FIRST door's topic (a single-object entry, or
    door 0 of a list): the summary's seen-this-week count is the first
    door's; a multi-door plan's other doors are not summed here."""
    query = f"""
        SELECT {_WORKFLOW_SUMMARY_COLUMNS},
               COALESCE(
                   COALESCE(definition, draft) -> 'entry' ->> 'topic',
                   COALESCE(definition, draft) -> 'entry' -> 0 ->> 'topic'
               ) AS entry_topic
        FROM {WORKFLOW_TABLE}
        WHERE merchant_id = $1
        ORDER BY created_at DESC, id DESC
        LIMIT $2 OFFSET $3
    """
    return query, [merchant_id, limit, offset]


def publish_workflow_query(merchant_id: str, workflow_id: str) -> Tuple[str, List[Any]]:
    """Publish = copy draft -> definition, bump version (the audit stamp),
    go/stay live. Runs inside the publish atom AFTER the validator said
    yes — the WHERE re-checks a draft still exists so a racing publish
    cannot double-bump."""
    query = f"""
        UPDATE {WORKFLOW_TABLE}
        SET definition = draft,
            draft = NULL,
            version = version + 1,
            status = CASE WHEN status = 'draft' THEN 'live' ELSE status END,
            updated_at = now()
        WHERE merchant_id = $1 AND id = $2 AND draft IS NOT NULL
        RETURNING {_WORKFLOW_COLUMNS}
    """
    return query, [merchant_id, workflow_id]


def set_workflow_status_query(
    merchant_id: str, workflow_id: str, status: str
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {WORKFLOW_TABLE}
        SET status = $3, updated_at = now()
        WHERE merchant_id = $1 AND id = $2 AND status <> 'archived'
        RETURNING {_WORKFLOW_COLUMNS}
    """
    return query, [merchant_id, workflow_id, status]


def live_workflows_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """The entry-rule processor's read: this merchant's live plans, on
    the (merchant_id, status) index — the read runs once per attributed
    event inside the event worker's pass, so it must never scan other
    tenants' plans (nor validate them in Python)."""
    query = f"""
        SELECT {_WORKFLOW_COLUMNS}
        FROM {WORKFLOW_TABLE}
        WHERE merchant_id = $1 AND status = 'live'
    """
    return query, [merchant_id]


def live_plans_naming_template_query(
    merchant_id: str, channel: str, name: str
) -> Tuple[str, List[Any]]:
    """The retirement guard's second count (rollout phase 14): plans that
    are live, or paused and able to go live, whose LATEST document has a
    send node on this channel naming this template — their next entrant
    would be pinned to a withdrawn template just as an open run is."""
    query = f"""
        SELECT count(*) AS plans
        FROM {WORKFLOW_TABLE} w
        WHERE w.merchant_id = $1 AND w.status IN ('live', 'paused')
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(w.definition->'nodes') AS node
              WHERE node->>'type' = 'send'
                AND node->>'channel' = $2
                AND node->>'template' = $3
          )
    """
    return query, [merchant_id, channel, name]
