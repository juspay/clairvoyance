"""SQL builders for the line-grant reads of crm_workflow_enrollment (T20).
$1 placeholders only.

Beside queries/enrollment.py rather than in it: these read runs by id alone,
with no merchant, because the dialler's grant carries only the run id.
"""

from typing import Any, List, Tuple

from app.crm.outreach.db.queries.enrollment import _RUN_COLUMNS
from app.crm.outreach.db.queries.tables import (
    ENROLLMENT_TABLE,
    VERSION_TABLE,
    WORKFLOW_TABLE,
)


def get_runs_query(run_ids: List[str]) -> Tuple[str, List[Any]]:
    """Runs by id, as they are NOW (the primary, never the replica): the
    grant decides on wake_at, and a stale row would decide wrong. Each row
    carries its plan's status: a paused or archived plan places no call."""
    query = f"""
        SELECT {_RUN_COLUMNS},
            (SELECT w.status FROM {WORKFLOW_TABLE} w
             WHERE w.merchant_id = e.merchant_id AND w.id = e.workflow_id) AS plan_status
        FROM {ENROLLMENT_TABLE} e
        WHERE id = ANY($1::uuid[])
    """
    return query, [run_ids]


def parked_call_squares_query() -> Tuple[str, List[Any]]:
    """The call squares that list topics, per live plan and version: where the
    runs a lost line queue forgot are parked (each run by its pinned version)."""
    query = f"""
        SELECT v.merchant_id, v.workflow_id, v.version, n->>'id' AS node
        FROM {WORKFLOW_TABLE} w
        JOIN {VERSION_TABLE} v ON v.merchant_id = w.merchant_id AND v.workflow_id = w.id,
             jsonb_array_elements(v.definition->'nodes') AS n
        WHERE w.status = 'live' AND n->>'type' = 'call'
          AND jsonb_array_length(COALESCE(n->'topics', '[]'::jsonb)) > 0
    """
    return query, []


def wake_parked_calls_batch_query(
    squares: List[Tuple[str, str, int, str]], after_id: str, limit: int
) -> Tuple[str, List[Any]]:
    """One batch of the runs parked on these squares wakes now, in id order
    after `after_id` (a woken run the walker leased again is not woken twice)."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE} SET wake_at = now()
        WHERE id IN (
            SELECT e.id FROM {ENROLLMENT_TABLE} e
            JOIN unnest($1::text[], $2::uuid[], $3::int[], $4::text[])
                 AS s(merchant_id, workflow_id, version, node)
              ON e.merchant_id = s.merchant_id AND e.workflow_id = s.workflow_id
             AND e.current_node = s.node AND e.workflow_version = s.version
            WHERE e.status = 'waiting' AND e.wake_at > now() AND e.id > $5::uuid
            ORDER BY e.id
            LIMIT $6
        )
        RETURNING id
    """
    columns = [list(c) for c in zip(*squares)] if squares else [[], [], [], []]
    return query, [*columns, after_id, limit]


def wake_runs_query(runs: List[Tuple[str, str]]) -> Tuple[str, List[Any]]:
    """These parked runs wake now, each only while it stands on the square it
    was read on (run id, node): a run a grant moved is left alone."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE} e
        SET wake_at = now()
        FROM unnest($1::uuid[], $2::text[]) AS r(id, node)
        WHERE e.id = r.id AND e.status = 'waiting' AND e.current_node = r.node
        RETURNING e.id
    """
    return query, [[r[0] for r in runs], [r[1] for r in runs]]
