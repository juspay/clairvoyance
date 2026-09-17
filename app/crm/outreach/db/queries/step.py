"""SQL builders for crm_workflow_step (T26) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2; the step table split out on its own
birth — T26's INSERT rides T20's UPDATE as a CTE arm, so the arm was born in
enrollment.py and moves here with the read. $1 placeholders only — every value
parameterized.
"""

from typing import Any, List, Tuple

from app.crm.outreach.db.queries.tables import STEP_TABLE

# --- the T26 flush (canon T26, migration 073) ------------------------------
#
# Every statement that MOVES a token carries the squares it left as a CTE
# arm. The INSERT selects FROM the UPDATE's own RETURNING, so it produces
# rows only when the UPDATE matched: a stale lease means no move AND no
# history, discarded together. A step row exists if and only if the move
# committed — no dual write, no drift, nothing to reconcile, no reaper.

_STEP_COLUMNS = """
    merchant_id, enrollment_id, workflow_id, workflow_version,
    node, node_type, arrived_at, left_at, arrived_by,
    outcome, next_node, attempts, last_error, dispatch_id, cut_short_by
"""

_STEP_RECORDSET = """
    node text, node_type text,
    arrived_at timestamptz, left_at timestamptz,
    arrived_by text, outcome text, next_node text,
    attempts smallint, last_error text, dispatch_id text, cut_short_by uuid
"""


def flush_arm(moved: str, steps_param: str, guard: str = "") -> str:
    """The INSERT arm, shared by every writer that moves a token. ``moved``
    names the CTE holding the UPDATE's RETURNING — the tenancy columns come
    off the row that actually moved, never off the caller's word for them
    (and the composite FK in 073 refuses the row if they ever disagreed).

    ``guard`` is how a writer that is NOT lease-conditional keeps its
    history honest: the walker's writes already match nothing when the run
    moved under them, but the goal-cancel is unconditional by law, so it
    checks the snapshot it described against the row it just ended."""
    return f"""
        INSERT INTO {STEP_TABLE} ({_STEP_COLUMNS})
        SELECT m.merchant_id, m.id, m.workflow_id, m.workflow_version,
               s.node, s.node_type, s.arrived_at, s.left_at, s.arrived_by,
               s.outcome, s.next_node, s.attempts, s.last_error, s.dispatch_id,
               s.cut_short_by
        FROM {moved} m,
             jsonb_to_recordset({steps_param}::jsonb) AS s ({_STEP_RECORDSET})
        {guard}
        RETURNING 1
    """


def run_steps_query(merchant_id: str, run_id: str, limit: int) -> Tuple[str, List[Any]]:
    """One run's CLOSED squares — the LATEST ``limit`` of them, newest
    first (canon T26) — the crm_workflow_step_run_ix read, exactly, scanned
    backwards. The accessor reverses them back to chronological.

    Why newest first: the route has no offset, so a limit is a CUT, and
    the question is which end falls off. Cutting the old end keeps the
    visible part contiguous with the open square steps.timeline() appends
    (law 3 — the present is always shown); ascending under a limit would
    show the FIRST squares, then the live one, with the middle missing
    and nothing saying so.

    The square the run stands on NOW is deliberately absent: it has not
    been left, so no row exists.

    merchant_id is a tenancy predicate, not the index's leading column —
    the index leads with enrollment_id because a FK column must lead its
    own index or the cascade delete seq-scans this table once per swept
    run. A uuid run id is already unique; the predicate is the paranoia."""
    query = f"""
        SELECT node, node_type, arrived_at, left_at, arrived_by,
               outcome, next_node, attempts, last_error, dispatch_id,
               cut_short_by, workflow_version
        FROM {STEP_TABLE}
        WHERE enrollment_id = $2 AND merchant_id = $1
        ORDER BY arrived_at DESC, id DESC
        LIMIT $3
    """
    return query, [merchant_id, run_id, limit]
