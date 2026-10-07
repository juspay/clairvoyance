"""SQL builders for the line-grant reads of crm_workflow_enrollment (T20).
$1 placeholders only.

Beside queries/enrollment.py rather than in it: these read runs by id alone,
with no merchant, because the dialler's grant carries only the run id.
"""

from typing import Any, List, Tuple

from app.crm.outreach.db.queries.enrollment import _RUN_COLUMNS
from app.crm.outreach.db.queries.tables import (
    ENROLLMENT_TABLE,
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


def wake_runs_query(run_ids: List[str]) -> Tuple[str, List[Any]]:
    """These parked runs wake now; a run already due (or leased) is left alone."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET wake_at = now()
        WHERE id = ANY($1::uuid[]) AND status = 'waiting' AND wake_at > now()
        RETURNING id
    """
    return query, [run_ids]
