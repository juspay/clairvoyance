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

# The entry read's own columns: the document MINUS its playbook. The words a
# call says are read from the run's PINNED version at execute time
# (nodes/blocks.py, off definitions.py's cache) and never from here, but they
# are the bulk of a document — 835KB of 849KB on the plan that prompted this —
# so carrying them detoasts and re-parses a megabyte per attributed event to
# compare a topic. `playbook` is Optional with a None default and no validator
# reads it, so what arrives is a whole document for every field this path
# touches (test_workflow_queries pins both halves of that).
# `draft` is not read back either: it is the SAME document with its own
# playbook, and it is NULL only between a publish and the next draft save — so
# a plan open in the editor would hand this read the whole thing back, per
# attributed event, for as long as the edit lasts. Entry never reads it (only
# plans.py does, through get_workflow_query), so the column is answered with a
# literal rather than fetched.
_WORKFLOW_ENTRY_COLUMNS = (
    _WORKFLOW_SUMMARY_COLUMNS
    + ", definition - 'playbook' AS definition, NULL::jsonb AS draft"
)


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


def workflow_status_query(merchant_id: str, workflow_id: str) -> Tuple[str, List[Any]]:
    """The walker's liveness read: is this plan still one a token may move
    on? One word, on the primary key — NOT get_workflow_query, whose two
    documents the walker never opens. The run executes its PINNED version
    (definitions.py, cached by (workflow, version)), so the live document
    here is only ever asked for its status; carrying it detoasts the whole
    plan once per claimed run, and at the morning window that is every
    waiting run at once."""
    query = f"""
        SELECT status
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


def count_workflows_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """The merchant's plan count, for the list's pager (X-Total-Count)."""
    query = f"""
        SELECT count(*)::int AS total
        FROM {WORKFLOW_TABLE}
        WHERE merchant_id = $1
    """
    return query, [merchant_id]


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
    tenants' plans (nor validate them in Python), and it leaves the
    playbook in the database (_WORKFLOW_ENTRY_COLUMNS)."""
    query = f"""
        SELECT {_WORKFLOW_ENTRY_COLUMNS}
        FROM {WORKFLOW_TABLE}
        WHERE merchant_id = $1 AND status = 'live'
    """
    return query, [merchant_id]


def live_plan_versions_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """Entry's ROUTING read: which plans are live, and at what version —
    no documents. Entry asks one question per attributed event ("does any
    live door name this topic?"), and for most events the answer is no, so
    the document only has to exist once a door matches.

    Deliberately NOT cached: status moves under an operator (publish,
    pause, archive) and a new merchant's first plan must admit on its very
    next event, so freshness here is worth more than the row it costs.
    The document behind each (id, version) IS cached — see
    definitions.live_definition — because a version is immutable (064).
    """
    query = f"""
        SELECT {_WORKFLOW_SUMMARY_COLUMNS}
        FROM {WORKFLOW_TABLE}
        WHERE merchant_id = $1 AND status = 'live'
    """
    return query, [merchant_id]


def live_definition_query(
    merchant_id: str, workflow_id: str, version: int
) -> Tuple[str, List[Any]]:
    """One live plan's document AT A NAMED VERSION, minus its playbook —
    the cache-miss read behind live_definition.

    ``version`` is in the WHERE, not just the key: a publish between the
    routing read and this one bumps the row, and matching on the version
    we were asked for means the cache can never hold a document under a
    version that is not its own. No row is then the honest answer, and the
    caller skips this plan for this one event (the next event routes to
    the new version).

    Playbook stripped for the same reason live_plan_versions exists: the
    words a call says are read from the run's PINNED version at execute
    time (nodes/blocks.py, off definitions.py's own cache), never here.
    """
    query = f"""
        SELECT version, definition - 'playbook' AS definition
        FROM {WORKFLOW_TABLE}
        WHERE merchant_id = $1 AND id = $2 AND version = $3
          AND status = 'live'
    """
    return query, [merchant_id, workflow_id, version]


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
