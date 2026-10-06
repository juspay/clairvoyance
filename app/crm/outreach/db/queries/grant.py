"""SQL builders for the line-grant reads of crm_workflow_enrollment (T20).
$1 placeholders only.

Beside queries/enrollment.py rather than in it: these read runs by id alone,
with no merchant, because the dialler's grant carries only the run id.
"""

from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.queries.enrollment import _RUN_COLUMNS
from app.crm.outreach.db.queries.tables import (
    ENROLLMENT_TABLE,
    VERSION_TABLE,
    WORKFLOW_TABLE,
)


def get_runs_query(run_ids: List[str]) -> Tuple[str, List[Any]]:
    """Runs by id, as they are NOW (the primary, never the replica): the
    grant decides on wake_at, and a stale row would decide wrong."""
    query = f"""
        SELECT {_RUN_COLUMNS}
        FROM {ENROLLMENT_TABLE}
        WHERE id = ANY($1::uuid[])
    """
    return query, [run_ids]


def parking_squares_query() -> Tuple[str, List[Any]]:
    """Where a call can be waiting for a line: the merchants, plans and call
    squares that list topics, over every published version of the plans a
    token may move on. It opens each of those documents (never one per run),
    so it costs by the number of versions."""
    query = f"""
        SELECT array_agg(DISTINCT v.merchant_id) AS merchants,
               array_agg(DISTINCT v.workflow_id) AS workflows,
               array_agg(DISTINCT node->>'id') AS nodes
        FROM {WORKFLOW_TABLE} w
        JOIN {VERSION_TABLE} v
          ON v.merchant_id = w.merchant_id AND v.workflow_id = w.id
        CROSS JOIN LATERAL jsonb_array_elements(v.definition->'nodes') AS node
        WHERE w.status NOT IN ('paused', 'archived')
          AND node->>'type' = 'call'
          AND jsonb_typeof(node->'topics') = 'array'
          AND node->'topics'->0 IS NOT NULL
    """
    return query, []


def waiting_runs_page_query(
    merchants: List[Any],
    workflows: List[Any],
    nodes: List[Any],
    after: Optional[Tuple[Any, str]],
    limit: int,
) -> Tuple[str, List[Any]]:
    """Waiting runs on those squares, in (wake_at, id) order after the last
    one read: a run holding for a line keeps its wake_at, so the order holds
    from page to page. The three lists are a net, not the answer (the caller
    judges each run by its own pinned document).

    Always one walk of crm_workflow_enrollment_due_ix from the cursor, the
    net applied to the rows as they come. OFFSET 0 keeps it that: without it
    the planner, which guesses the three lists far too selective, fetches
    and sorts every run on the squares for a page (measured, 7 Oct 2026). The
    first page walks past the timers due sooner; a later page reads its own
    rows."""
    cursor = "AND (wake_at, id) > ($5::timestamptz, $6::uuid)" if after else ""
    query = f"""
        SELECT {_RUN_COLUMNS}
        FROM (
            SELECT {_RUN_COLUMNS}
            FROM {ENROLLMENT_TABLE}
            WHERE status = 'waiting' {cursor}
            ORDER BY wake_at, id
            OFFSET 0
        ) due
        WHERE merchant_id = ANY($1::text[])
          AND workflow_id = ANY($2::uuid[])
          AND current_node = ANY($3::text[])
        ORDER BY wake_at, id
        LIMIT $4
    """
    return query, [merchants, workflows, nodes, limit, *(after or ())]
