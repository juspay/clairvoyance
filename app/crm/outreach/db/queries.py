"""SQL builders for crm_workflow (T19) and crm_workflow_enrollment (T20).
$1 placeholders only — every value parameterized.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

WORKFLOW_TABLE = "crm_workflow"
ENROLLMENT_TABLE = "crm_workflow_enrollment"
_WORKFLOW_SUMMARY_COLUMNS = """
    id, merchant_id, name, status, version, created_by,
    created_at, updated_at
"""

# Detail adds the two documents — the list never fetches them.
_WORKFLOW_COLUMNS = _WORKFLOW_SUMMARY_COLUMNS + ", definition, draft"

_RUN_COLUMNS = """
    id, merchant_id, workflow_id, workflow_version, customer_id, status,
    current_node, wake_at, entered_at, exited_at, exit_reason, context,
    enrollment_key, attempts, last_error
"""


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
    query = f"""
        SELECT {_WORKFLOW_SUMMARY_COLUMNS}
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


def occupied_nodes_query(merchant_id: str, workflow_id: str) -> Tuple[str, List[Any]]:
    """The publish validator's occupied-square read: node ids that waiting
    or parked runs currently stand on — deleting one strands its tokens."""
    query = f"""
        SELECT DISTINCT current_node
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2 AND status <> 'exited'
    """
    return query, [merchant_id, workflow_id]


def insert_enrollment_query(
    merchant_id: str,
    workflow_id: str,
    workflow_version: int,
    customer_id: str,
    current_node: str,
    wake_at: datetime,
    context: Dict[str, Any],
    enrollment_key: str,
) -> Tuple[str, List[Any]]:
    """The token is born. The partial unique (merchant, workflow, key)
    WHERE not exited absorbs the enrol race — a UniqueViolation here
    means 'already in flow', never an error."""
    query = f"""
        INSERT INTO {ENROLLMENT_TABLE}
            (merchant_id, workflow_id, workflow_version, customer_id,
             current_node, wake_at, context, enrollment_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
        RETURNING {_RUN_COLUMNS}
    """
    return query, [
        merchant_id,
        workflow_id,
        workflow_version,
        customer_id,
        current_node,
        wake_at,
        json.dumps(context),
        enrollment_key,
    ]


def admission_facts_query(
    merchant_id: str, workflow_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    """Everything the admission guards need in one read: has she EVER run
    this flow (reenter), when did her latest run begin (cooldown)."""
    query = f"""
        SELECT count(*) AS runs, max(entered_at) AS latest_entered_at
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2 AND customer_id = $3
    """
    return query, [merchant_id, workflow_id, customer_id]


def source_event_used_query(
    merchant_id: str, workflow_id: str, customer_id: str, source_event_id: str
) -> Tuple[str, List[Any]]:
    """Per-event idempotency for the entry processor: the window scan may
    hand us the same event twice (at-least-once, by design); a run born
    from it already existing — open OR exited — means skip."""
    query = f"""
        SELECT EXISTS (
            SELECT 1 FROM {ENROLLMENT_TABLE}
            WHERE merchant_id = $1 AND workflow_id = $2 AND customer_id = $3
              AND context->>'source_event_id' = $4
        ) AS used
    """
    return query, [merchant_id, workflow_id, customer_id, source_event_id]


def claim_due_runs_query(limit: int, lease_seconds: int) -> Tuple[str, List[Any]]:
    """The walker's claim — canon T20: wake_at is the timer AND the lease.
    One statement: lock due tokens (SKIP LOCKED — replicas never collide),
    push wake_at one lease window (a dead worker's row self-heals when the
    clock passes again; no reaper), and count the claim against the run
    (attempts++ BY the claim — a poison run that crashes its worker counts
    against itself). A paused plan's rows are skipped, not claimed (canon
    T19: "the sweeper skips its rows"), so a pause never burns attempts."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET wake_at = now() + make_interval(secs => $2),
            attempts = attempts + 1
        WHERE id IN (
            SELECT e.id FROM {ENROLLMENT_TABLE} e
            WHERE e.status = 'waiting' AND e.wake_at <= now()
              AND NOT EXISTS (
                  SELECT 1 FROM {WORKFLOW_TABLE} w
                  WHERE w.merchant_id = e.merchant_id AND w.id = e.workflow_id
                    AND w.status = 'paused'
              )
            ORDER BY wake_at, id
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING {_RUN_COLUMNS}
    """
    return query, [limit, lease_seconds]


def advance_run_query(
    run_id: str, current_node: str, wake_at: datetime, context: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """A successful step: move the token, set its next alarm, reset the
    failure counter (only CONSECUTIVE failures park a run)."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET current_node = $2, wake_at = $3, context = $4::jsonb,
            attempts = 0, last_error = NULL
        WHERE id = $1 AND status = 'waiting'
    """
    return query, [run_id, current_node, wake_at, json.dumps(context)]


def exit_run_query(
    run_id: str,
    exit_reason: str,
    current_node: Optional[str],
    context: Optional[Dict[str, Any]],
) -> Tuple[str, List[Any]]:
    """An exit without a context KEEPS the row's context: the exited row's
    pointers (source_event_id above all) are what source_event_used reads
    to refuse a replayed entry event — wiping them would let the spine's
    at-least-once redelivery enrol a second run from a stale checkout."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET status = 'exited', exit_reason = $2, exited_at = now(),
            wake_at = NULL,
            current_node = COALESCE($3, current_node),
            context = COALESCE($4::jsonb, context)
        WHERE id = $1 AND status <> 'exited'
    """
    return query, [
        run_id,
        exit_reason,
        current_node,
        None if context is None else json.dumps(context),
    ]


def park_run_query(run_id: str, last_error: str) -> Tuple[str, List[Any]]:
    """Errors park, never exit (canon) — held visible for the merchant,
    resumable by a human."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET status = 'parked', wake_at = NULL, last_error = $2
        WHERE id = $1 AND status = 'waiting'
    """
    return query, [run_id, last_error]


def record_run_error_query(
    run_id: str, last_error: str, retry_in_seconds: int
) -> Tuple[str, List[Any]]:
    """A transient failure: the retry timer is written into wake_at (canon
    T20: backoff with jitter), and the reason is kept for the screen."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET last_error = $2,
            wake_at = now() + make_interval(secs => $3)
        WHERE id = $1 AND status = 'waiting'
    """
    return query, [run_id, last_error, retry_in_seconds]


def resume_run_on_event_query(
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    node_id: str,
    context_patch: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """W5: the reply reaches the token. Only a run still standing on the
    listening square is touched — a late or repeated reply changes
    nothing. The answer is recorded on the run BEFORE anything fires
    (canon T20), and wake_at = now() hands it to the walker."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET context = context || $5::jsonb, wake_at = now(), last_error = NULL
        WHERE merchant_id = $1 AND workflow_id = $2 AND customer_id = $3
          AND status = 'waiting' AND current_node = $4
    """
    return query, [
        merchant_id,
        workflow_id,
        customer_id,
        node_id,
        json.dumps(context_patch),
    ]


def cancel_open_runs_query(
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    exit_reason: str,
    occurred_at: Optional[datetime] = None,
) -> Tuple[str, List[Any]]:
    """Goal-cancel (canon: the goal event resolves OPEN enrolments — open
    is waiting or parked; a parked run she has already satisfied must not
    keep holding the open-run unique) — and the same shape ejects runs
    when a flow is archived. Time-aware: only runs that began before the
    goal event count, so a stale goal event redelivered later cannot end
    a newer run; an unstamped event (occurred_at NULL) keeps today's
    behaviour and ends every open run."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET status = 'exited', exit_reason = $4, exited_at = now(),
            wake_at = NULL
        WHERE merchant_id = $1 AND workflow_id = $2 AND customer_id = $3
          AND status <> 'exited'
          AND ($5::timestamptz IS NULL OR entered_at < $5::timestamptz)
        RETURNING id
    """
    return query, [merchant_id, workflow_id, customer_id, exit_reason, occurred_at]


def list_runs_query(
    merchant_id: str,
    workflow_id: str,
    status: Optional[str],
    limit: int,
    offset: int,
) -> Tuple[str, List[Any]]:
    """The ops read behind canon's 'last_error readable on the merchant's
    screen': a flow's runs, newest first, optionally one status
    (parked = the triage view)."""
    status_predicate = "AND status = $3" if status else ""
    limit_params = ("$4", "$5") if status else ("$3", "$4")
    query = f"""
        SELECT {_RUN_COLUMNS}
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2 {status_predicate}
        ORDER BY entered_at DESC, id DESC
        LIMIT {limit_params[0]} OFFSET {limit_params[1]}
    """
    params: List[Any] = [merchant_id, workflow_id]
    if status:
        params.append(status)
    params.extend([limit, offset])
    return query, params


def resume_run_query(
    merchant_id: str, workflow_id: str, run_id: str
) -> Tuple[str, List[Any]]:
    """Canon T20: parked runs are 'held for the merchant to see and
    resume'. Resume = wake now, failure counter forgiven (only
    CONSECUTIVE failures park); last_error stays visible until the next
    successful step clears it — the human deserves to see what they
    fixed. Only a parked run resumes; racing resumes are idempotent."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET status = 'waiting', wake_at = now(), attempts = 0
        WHERE merchant_id = $1 AND workflow_id = $2 AND id = $3
          AND status = 'parked'
        RETURNING {_RUN_COLUMNS}
    """
    return query, [merchant_id, workflow_id, run_id]


def sweep_exited_runs_query(cutoff: datetime, batch: int) -> Tuple[str, List[Any]]:
    """Canon T20 exited_at: 'the death clock — the retention sweep reads
    it: exited rows age out on a partial index over exited_at, which is
    most of what keeps the hot table small.' Batched so one sweep never
    holds a long lock; housekeeping on the owner module's own table, all
    tenants (the partition-drop shape, row-level)."""
    query = f"""
        DELETE FROM {ENROLLMENT_TABLE}
        WHERE id IN (
            SELECT id FROM {ENROLLMENT_TABLE}
            WHERE status = 'exited' AND exited_at < $1
            ORDER BY exited_at
            LIMIT $2
        )
        RETURNING id
    """
    return query, [cutoff, batch]
