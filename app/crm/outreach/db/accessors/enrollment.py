"""Mechanical DB access for crm_workflow_enrollment (T20) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). Two shapes, by signature:
a ``conn`` parameter runs inside the caller's atom; no parameter self-scopes
one statement (module rules §1).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.core.config.static import CRM_ANALYTICS_QUERY_TIMEOUT_SECONDS
from app.crm.outreach.db.decoders.enrollment import (
    decode_customer_run,
    decode_run,
    decode_run_row,
    decode_run_summary,
)
from app.crm.outreach.db.queries.enrollment import (
    admission_facts_query,
    advance_run_query,
    cancel_run_query,
    claim_due_runs_query,
    cold_pile_by_merchant_query,
    count_runs_query,
    customer_runs_query,
    due_cold_plans_query,
    exit_run_query,
    get_run_query,
    insert_enrollment_query,
    list_runs_query,
    occupied_nodes_on_version_query,
    occupied_nodes_query,
    open_by_node_query,
    open_runs_for_customer_query,
    park_run_query,
    patch_open_run_query,
    record_run_error_query,
    refresh_run_facts_query,
    repin_open_runs_query,
    repin_runs_on_version_query,
    resume_run_by_id_query,
    resume_run_query,
    run_endings_in_window_query,
    runs_per_day_query,
    runs_referencing_template_query,
    source_event_used_query,
    sweep_exited_runs_query,
    workflow_split_counts_query,
    workflow_summary_query,
)
from app.crm.outreach.schemas import (
    CustomerRun,
    EnrollmentRun,
    RunEnding,
    RunRow,
    WorkflowRunSummary,
)
from app.crm.shared.db import crm_connection


async def occupied_nodes(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str
) -> List[str]:
    query, values = occupied_nodes_query(merchant_id, workflow_id)
    rows = await conn.fetch(query, *values)
    return [row["current_node"] for row in rows]


async def repin_open_runs(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str, version: int
) -> int:
    """Runs inside the publish atom (conn param). Returns how many runs
    now execute the new version."""
    query, values = repin_open_runs_query(merchant_id, workflow_id, version)
    rows = await conn.fetch(query, *values)
    return len(rows)


async def occupied_nodes_on_version(
    conn: asyncpg.Connection, merchant_id: str, workflow_id: str, version: int
) -> List[str]:
    query, values = occupied_nodes_on_version_query(merchant_id, workflow_id, version)
    rows = await conn.fetch(query, *values)
    return [row["current_node"] for row in rows]


async def repin_runs_on_version(
    conn: asyncpg.Connection,
    merchant_id: str,
    workflow_id: str,
    from_version: int,
    to_version: int,
) -> int:
    query, values = repin_runs_on_version_query(
        merchant_id, workflow_id, from_version, to_version
    )
    rows = await conn.fetch(query, *values)
    return len(rows)


async def runs_referencing_template(merchant_id: str, channel: str, name: str) -> int:
    query, values = runs_referencing_template_query(merchant_id, channel, name)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return int(row["runs"]) if row is not None else 0


async def admission_facts(
    conn: asyncpg.Connection,
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    enrollment_key: Optional[str] = None,
) -> Dict[str, Any]:
    query, values = admission_facts_query(
        merchant_id, workflow_id, customer_id, enrollment_key
    )
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
    lane: str = "hot",
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
        lane,
    )
    row = await conn.fetchrow(query, *values)
    assert row is not None  # INSERT ... RETURNING always yields the row
    return decode_run(row)


async def claim_due_runs(
    limit: int,
    lease_seconds: int,
    lane: str = "hot",
    merchant_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> List[EnrollmentRun]:
    """One statement — the lock, the lease push and the attempts count
    commit together; Postgres runs it atomically, no wrapper needed. One
    lane per call; a cold claim names its plan (the room is per plan)."""
    query, values = claim_due_runs_query(
        limit, lease_seconds, lane, merchant_id, workflow_id
    )
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]


async def cold_pile_by_merchant() -> Dict[str, int]:
    """Per merchant: cold runs due and not yet claimed."""
    query, values = cold_pile_by_merchant_query()
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return {str(row["merchant_id"]): int(row["waiting"]) for row in rows}


async def due_cold_plans() -> List[Tuple[str, str]]:
    """(merchant_id, workflow_id) of every live plan with a cold run due."""
    query, values = due_cold_plans_query()
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [(str(row["merchant_id"]), str(row["workflow_id"])) for row in rows]


async def advance_run(
    run_id: str,
    current_node: str,
    wake_at: datetime,
    context: Dict[str, Any],
    leased_wake_at: datetime,
    node_arrived_at: Optional[datetime] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
    lane: Optional[str] = None,
) -> bool:
    """True when the row still carried the lease (the write landed, and
    the buffered squares landed with it). ``lane`` 'cold' when a window
    held the alarm; None keeps the run's lane."""
    query, values = advance_run_query(
        run_id,
        current_node,
        wake_at,
        context,
        leased_wake_at,
        node_arrived_at,
        steps,
        lane,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return _moved(row)


def _moved(row: Optional[Any]) -> bool:
    """The CAS answer, read off the flush statement (canon T26).

    A plain `UPDATE ... RETURNING` answered by returning no row. The flush
    statements end in a SELECT of two scalars, so they ALWAYS return one
    row and `row is not None` would read every stale lease as a win — the
    walker would stop deferring and start clobbering replies. The answer is
    the UPDATE's own id, which is NULL when nothing matched."""
    return row is not None and row["moved_id"] is not None


async def exit_run(
    run_id: str,
    exit_reason: str,
    leased_wake_at: datetime,
    current_node: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """True when the row still carried the lease (the write landed)."""
    query, values = exit_run_query(
        run_id, exit_reason, current_node, context, leased_wake_at, steps
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return _moved(row)


async def park_run(run_id: str, last_error: str, leased_wake_at: datetime) -> bool:
    """True when the row still carried the lease (the write landed)."""
    query, values = park_run_query(run_id, last_error, leased_wake_at)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def record_run_error(
    run_id: str, last_error: str, retry_in_seconds: int, leased_wake_at: datetime
) -> bool:
    """True when the row still carried the lease (the write landed)."""
    query, values = record_run_error_query(
        run_id, last_error, retry_in_seconds, leased_wake_at
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def get_run(
    merchant_id: str, workflow_id: str, run_id: str
) -> Optional[EnrollmentRun]:
    """One run by id, or None when it is not this merchant's or not this
    plan's."""
    query, values = get_run_query(merchant_id, workflow_id, run_id)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_run(row) if row else None


async def open_runs_for_customer(
    merchant_id: str, customer_id: str
) -> List[EnrollmentRun]:
    query, values = open_runs_for_customer_query(merchant_id, customer_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_run(row) for row in rows]


async def resume_run_by_id(
    merchant_id: str,
    run_id: str,
    node_id: str,
    context_patch: Dict[str, Any],
    facts: Optional[Dict[str, Any]] = None,
    lane: Optional[str] = None,
) -> bool:
    """True when the run was standing on the listening square (waiting or
    parked) and took the answer and the letter's facts. ``lane`` 'hot' for
    a merchant's letter; None (our own call report) keeps the run's."""
    query, values = resume_run_by_id_query(
        merchant_id, run_id, node_id, context_patch, facts, lane
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def refresh_run_facts(
    merchant_id: str,
    run_id: str,
    node_id: str,
    facts: Dict[str, Any],
    cut_short_by: Optional[str] = None,
    lane: Optional[str] = None,
) -> bool:
    """True when the run was standing (waiting or parked) on that
    non-listening square and took the letter's facts as its newest.
    ``lane`` as on the reply path."""
    query, values = refresh_run_facts_query(
        merchant_id, run_id, node_id, facts, cut_short_by, lane
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def cancel_run(
    merchant_id: str,
    run_id: str,
    exit_reason: str,
    occurred_at: Optional[datetime] = None,
    key: Optional[Tuple[str, str]] = None,
    context_patch: Optional[Dict[str, Any]] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """True when the run was open (and, keyed, still the one the letter
    is about) and ended — with its final square flushed (trap 3)."""
    query, values = cancel_run_query(
        merchant_id, run_id, exit_reason, occurred_at, key, context_patch, steps
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return _moved(row)


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
    anywhere: bool = False,
) -> bool:
    """True when an open run on the door's start square (or, with
    ``anywhere``, on any square) took the repeat."""
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
        anywhere,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return row is not None


async def list_runs(
    merchant_id: str,
    workflow_id: str,
    status: Optional[str],
    limit: int,
    offset: int,
    node: Optional[str] = None,
    version: Optional[int] = None,
    exit_reason: Optional[str] = None,
    search: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    anchor_entered_at: Optional[datetime] = None,
    anchor_id: Optional[str] = None,
) -> Tuple[List[RunRow], int]:
    """One page of runs and the filtered total. The total rides on the
    rows; an empty page (an offset past the last match) has none, so it
    is counted on its own — never reported as 0 for a list that is not."""
    query, values = list_runs_query(
        merchant_id,
        workflow_id,
        status,
        limit,
        offset,
        node,
        version,
        exit_reason,
        search,
        since,
        until,
        anchor_entered_at,
        anchor_id,
    )
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
        if rows:
            total = int(rows[0]["total"])
        else:
            count_query, count_values = count_runs_query(
                merchant_id,
                workflow_id,
                status,
                node,
                version,
                exit_reason,
                search,
                since,
                until,
                anchor_entered_at,
                anchor_id,
            )
            total = int(await conn.fetchval(count_query, *count_values) or 0)
    return [decode_run_row(row) for row in rows], total


async def run_endings_in_window(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> List[RunEnding]:
    """One RunEnding per run of the plan that entered in the window."""
    query, values = run_endings_in_window_query(merchant_id, workflow_id, since, until)
    async with crm_connection() as conn:
        # Bounded like the lead reads it feeds: a report the browser gave
        # up on must not keep running (asyncpg cancels on timeout).
        rows = await conn.fetch(
            query, *values, timeout=CRM_ANALYTICS_QUERY_TIMEOUT_SECONDS
        )
    return [
        RunEnding(
            str(row["id"]),
            str(row["enrollment_key"]),
            str(row["status"]),
            row["exit_reason"],
            row["exited_at"],
            row["entered_at"],
            row["current_node"],
        )
        for row in rows
    ]


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


async def workflow_summary(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
    tz: str = "Asia/Kolkata",
) -> WorkflowRunSummary:
    query, values = workflow_summary_query(merchant_id, workflow_id, since, until)
    # The arm counts are their own statement (enh A/04): a run with two
    # split squares expands to two rows there, which folded into the
    # aggregate above would count it twice. Both reads sit on one
    # connection and one window — read-only, so no atom is owed.
    split_query, split_values = workflow_split_counts_query(
        merchant_id, workflow_id, since, until
    )
    day_query, day_values = runs_per_day_query(
        merchant_id, workflow_id, since, until, tz
    )
    node_query, node_values = open_by_node_query(merchant_id, workflow_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
        split_rows = await conn.fetch(split_query, *split_values)
        day_rows = await conn.fetch(day_query, *day_values)
        node_rows = await conn.fetch(node_query, *node_values)
    return decode_run_summary(rows, split_rows, day_rows, node_rows)


async def customer_runs(
    merchant_id: str, customer_id: str, limit: int
) -> List[CustomerRun]:
    query, values = customer_runs_query(merchant_id, customer_id, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_customer_run(row) for row in rows]
