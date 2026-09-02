"""SQL builders for crm_workflow (T19) and crm_workflow_enrollment (T20).
$1 placeholders only — every value parameterized.

Two kinds of writer touch a run, and the law between them (P1, rollout
phase 03): WALKER writes (advance, exit, park, record error) are
compare-and-set on the leased wake_at — the claim pushed wake_at one
lease window and returned it, so that value is the generation the visit
holds; a write carrying a stale lease matches zero rows. EVENT-SIDE writes
(goal-cancel, a wait_event reply, a repeat patch) are unconditional and
always move wake_at — the event wins, the walker defers to its next wake.
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
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    enrollment_key: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Everything the admission guards need in one read: has she EVER run
    this flow (reenter), when did her latest run begin (cooldown).

    On a keyed plan (entry.key, canon T20 col 13: "one run per <field>")
    the history judged is that KEY's, not the customer's — "has this
    ORDER ever run" is what the author declared, so her second order has
    no history and is admitted (B2, rollout phase 02). The customer
    predicate stays beside the key for tenancy paranoia. Unkeyed plans
    keep the customer-wide read."""
    if enrollment_key is not None:
        query = f"""
            SELECT count(*) AS runs, max(entered_at) AS latest_entered_at
            FROM {ENROLLMENT_TABLE}
            WHERE merchant_id = $1 AND workflow_id = $2
              AND enrollment_key = $3 AND customer_id = $4
        """
        return query, [merchant_id, workflow_id, enrollment_key, customer_id]
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
    run_id: str,
    current_node: str,
    wake_at: datetime,
    context: Dict[str, Any],
    leased_wake_at: datetime,
) -> Tuple[str, List[Any]]:
    """A successful step: move the token, set its next alarm, reset the
    failure counter (only CONSECUTIVE failures park a run).

    The lease is the generation: a write under a stale lease is a no-op.
    A reply or a repeat that landed mid-visit moved wake_at, so this
    UPDATE matches nothing and the walker defers instead of clobbering
    the answer with the timeout path (P1)."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET current_node = $2, wake_at = $3, context = $4::jsonb,
            attempts = 0, last_error = NULL
        WHERE id = $1 AND status = 'waiting' AND wake_at = $5
        RETURNING id
    """
    return query, [run_id, current_node, wake_at, json.dumps(context), leased_wake_at]


def exit_run_query(
    run_id: str,
    exit_reason: str,
    current_node: Optional[str],
    context: Optional[Dict[str, Any]],
    leased_wake_at: datetime,
) -> Tuple[str, List[Any]]:
    """The WALKER's exit (timed_out, goal_met, completed, ejected). An exit
    without a context KEEPS the row's context: the exited row's pointers
    (source_event_id above all) are what source_event_used reads to
    refuse a replayed entry event — wiping them would let the spine's
    at-least-once redelivery enrol a second run from a stale checkout.

    The lease is the generation: a write under a stale lease is a no-op
    (P1). The event side's exit is cancel_open_runs_query, unconditional."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET status = 'exited', exit_reason = $2, exited_at = now(),
            wake_at = NULL,
            current_node = COALESCE($3, current_node),
            context = COALESCE($4::jsonb, context)
        WHERE id = $1 AND status <> 'exited' AND wake_at = $5
        RETURNING id
    """
    return query, [
        run_id,
        exit_reason,
        current_node,
        None if context is None else json.dumps(context),
        leased_wake_at,
    ]


def park_run_query(
    run_id: str, last_error: str, leased_wake_at: datetime
) -> Tuple[str, List[Any]]:
    """Errors park, never exit (canon) — held visible for the merchant,
    resumable by a human. The lease is the generation: a park under a
    stale lease is a no-op (P1) — the run that moved on will be judged
    again on its next claim."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET status = 'parked', wake_at = NULL, last_error = $2
        WHERE id = $1 AND status = 'waiting' AND wake_at = $3
        RETURNING id
    """
    return query, [run_id, last_error, leased_wake_at]


def record_run_error_query(
    run_id: str, last_error: str, retry_in_seconds: int, leased_wake_at: datetime
) -> Tuple[str, List[Any]]:
    """A transient failure: the retry timer is written into wake_at (canon
    T20: backoff with jitter), and the reason is kept for the screen. The
    lease is the generation: under a stale lease this is a no-op (P1) —
    the reply that moved the run already set its own, earlier alarm."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET last_error = $2,
            wake_at = now() + make_interval(secs => $3)
        WHERE id = $1 AND status = 'waiting' AND wake_at = $4
        RETURNING id
    """
    return query, [run_id, last_error, retry_in_seconds, leased_wake_at]


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


def patch_open_run_query(
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
) -> Tuple[str, List[Any]]:
    """Repeat entries (modules/05 §Repeat entries): ONE idempotent UPDATE
    in the resume_run_on_event shape. Touches only a run still standing on
    its first square (status waiting, current_node = the entry node) — a
    run past it is never patched. Found by enrollment_key so a keyed plan's
    order edit patches ITS order's run. The event marks itself used in
    context.repeat_event_ids, so a redelivered repeat matches zero rows and
    the alarm cannot slide twice for one letter.

    The facts win unconditionally (refresh_latest), only when the new value
    beats the stored one (refresh_max — compared here, in the statement, a
    non-numeric stored value never blocks a numeric win), or are appended
    under repeat_items (accumulate).

    Two guards folded in when #1041 was carried (rollout phase 00):
      * the run's OWN founding event is never a repeat — a redelivered copy
        is refused by source_event_used, lands here, and is not yet in
        repeat_event_ids, so without the IS DISTINCT FROM predicate it would
        overwrite newer facts with the first snapshot and restart the alarm;
      * debounce > 0 may only EXTEND the window: GREATEST(wake_at, now()+N),
        because now()+N alone pulls the alarm EARLIER whenever the debounce
        is shorter than the entry wait still remaining."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET context = (
                CASE
                    WHEN $7::boolean THEN
                        context || jsonb_build_object(
                            'repeat_items',
                            COALESCE(context->'repeat_items', '[]'::jsonb)
                                || jsonb_build_array($6::jsonb),
                            'repeat_count',
                            COALESCE(jsonb_array_length(context->'repeat_items'), 0) + 2
                        )
                    WHEN $8::text IS NULL THEN context || $6::jsonb
                    WHEN $9::float8 > COALESCE(
                        CASE WHEN (context->>$8::text) ~ '^-?[0-9]+(\\.[0-9]+)?$'
                             THEN (context->>$8::text)::float8 END,
                        '-Infinity'::float8)
                        THEN context || $6::jsonb
                    ELSE context
                END
            ) || jsonb_build_object(
                'repeat_event_ids',
                COALESCE(context->'repeat_event_ids', '[]'::jsonb)
                    || jsonb_build_array(to_jsonb($5::text))
            ),
            wake_at = CASE WHEN $10::float8 > 0
                           THEN GREATEST(wake_at, now() + make_interval(secs => $10::float8 * 60))
                           ELSE wake_at END,
            last_error = NULL
        WHERE merchant_id = $1 AND workflow_id = $2::uuid AND enrollment_key = $3
          AND status = 'waiting' AND current_node = $4
          AND NOT (COALESCE(context->'repeat_event_ids', '[]'::jsonb) ? $5::text)
          AND context->>'source_event_id' IS DISTINCT FROM $5::text
        RETURNING id
    """
    return query, [
        merchant_id,
        workflow_id,
        enrollment_key,
        entry_node,
        event_id,
        json.dumps(patch),
        accumulate,
        max_field,
        max_value,
        debounce_minutes,
    ]


def cancel_open_runs_query(
    merchant_id: str,
    workflow_id: str,
    customer_id: str,
    exit_reason: str,
    occurred_at: Optional[datetime] = None,
    key: Optional[Tuple[str, str]] = None,
) -> Tuple[str, List[Any]]:
    """Goal-cancel (canon: the goal event resolves OPEN enrolments — open
    is waiting or parked; a parked run she has already satisfied must not
    keep holding the open-run unique) — and the same shape ejects runs
    when a flow is archived. Event-side: unconditional (the walker's
    writes defer to it, phase 03).

    Time-aware on the ENTRY EVENT (G7, phase 06): only runs whose
    founding letter happened before the goal event count — the run's
    context carries that moment (entered_event_at), with the row's
    insert time as the fallback for runs written before the stamp — so a
    late-delivered earlier-stage letter cannot keep a run alive past a
    goal that truly happened after it, and a stale goal redelivered
    later cannot end a newer run. An unstamped goal (occurred_at NULL)
    keeps today's behaviour and ends every open run.

    Keyed (phase 06): ``key = (run field, value)`` ends only the run the
    letter is about (context cart_token = the order's cart_token); the
    unkeyed form is byte-identical to before."""
    keyed = "AND context->>$6 = $7" if key else ""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET status = 'exited', exit_reason = $4, exited_at = now(),
            wake_at = NULL
        WHERE merchant_id = $1 AND workflow_id = $2 AND customer_id = $3
          AND status <> 'exited'
          AND ($5::timestamptz IS NULL OR COALESCE((context->>'entered_event_at')::timestamptz, entered_at) < $5::timestamptz)
          {keyed}
        RETURNING id
    """
    params: List[Any] = [
        merchant_id,
        workflow_id,
        customer_id,
        exit_reason,
        occurred_at,
    ]
    if key:
        params.extend([key[0], key[1]])
    return query, params


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
