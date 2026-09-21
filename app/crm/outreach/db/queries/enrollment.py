"""SQL builders for crm_workflow_enrollment (T20) — one table, one file (module rules §1 at scale;
outreach took the shape 3 Sep 2026, structure PR 2). $1 placeholders only — every value
parameterized.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.outreach.db.queries.step import flush_arm
from app.crm.outreach.db.queries.tables import (
    ENROLLMENT_TABLE,
    VERSION_TABLE,
    WORKFLOW_TABLE,
)
from app.crm.outreach.schemas import SPLIT_PREFIX

_RUN_COLUMNS = """
    id, merchant_id, workflow_id, workflow_version, customer_id, status,
    current_node, wake_at, entered_at, exited_at, exit_reason, context,
    enrollment_key, attempts, last_error, node_arrived_at
"""

# --- the T26 flush (canon T26, migration 073) ------------------------------
#
# Every statement that MOVES a token composes step.flush_arm, which lives
# with ITS table: the INSERT selects FROM the UPDATE's own RETURNING, so a
# stale lease means no move AND no history, discarded together.


def repin_open_runs_query(
    merchant_id: str, workflow_id: str, version: int
) -> Tuple[str, List[Any]]:
    """migrate mode (ADR 0023): every open run of the plan now executes the
    version just published — inside the publish atom, after the stranding
    validator said the document keeps every occupied square. Exited runs
    keep the version they finished under: the audit fact."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET workflow_version = $3
        WHERE merchant_id = $1 AND workflow_id = $2 AND status <> 'exited'
        RETURNING id
    """
    return query, [merchant_id, workflow_id, version]


def occupied_nodes_on_version_query(
    merchant_id: str, workflow_id: str, version: int
) -> Tuple[str, List[Any]]:
    """migrate-forward's occupied-square read (rollout phase 14): the
    squares open runs pinned to THIS version stand on — the target must
    keep every one of them."""
    query = f"""
        SELECT DISTINCT current_node
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2 AND workflow_version = $3
          AND status <> 'exited'
    """
    return query, [merchant_id, workflow_id, version]


def repin_runs_on_version_query(
    merchant_id: str, workflow_id: str, from_version: int, to_version: int
) -> Tuple[str, List[Any]]:
    """migrate-forward (rollout phase 14): every open run pinned to
    from_version now executes to_version — after validate_migration said
    the target keeps every occupied square and the entry. Exited runs keep
    the version they finished under: the audit fact."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET workflow_version = $4
        WHERE merchant_id = $1 AND workflow_id = $2 AND workflow_version = $3
          AND status <> 'exited'
        RETURNING id
    """
    return query, [merchant_id, workflow_id, from_version, to_version]


def runs_referencing_template_query(
    merchant_id: str, channel: str, name: str
) -> Tuple[str, List[Any]]:
    """The template retirement guard's count (rollout phase 14): open runs
    whose PINNED document has a send node on this channel naming this
    template — judged by the version each run executes, never the live
    one. jsonb_array_elements over the document's nodes, so it needs no
    index while the table is small; index later if hot."""
    query = f"""
        SELECT count(*) AS runs
        FROM {ENROLLMENT_TABLE} e
        JOIN {VERSION_TABLE} v
          ON v.merchant_id = e.merchant_id
         AND v.workflow_id = e.workflow_id
         AND v.version = e.workflow_version
        WHERE e.merchant_id = $1 AND e.status <> 'exited'
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(v.definition->'nodes') AS node
              WHERE node->>'type' = 'send'
                AND node->>'channel' = $2
                AND node->>'template' = $3
          )
    """
    return query, [merchant_id, channel, name]


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
    means 'already in flow', never an error.

    node_arrived_at is stamped with the SAME now() that defaults
    entered_at (canon T26): no row is written here — the door's square has
    not been left — but the arrival has to be on the row before the first
    flush can date it. Equal to entered_at is also what lets
    steps.first_arrival read `door` with no stored flag, for exactly as
    long as the run has never moved."""
    query = f"""
        INSERT INTO {ENROLLMENT_TABLE}
            (merchant_id, workflow_id, workflow_version, customer_id,
             current_node, wake_at, context, enrollment_key,
             entered_at, node_arrived_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, now(), now())
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
    node_arrived_at: Optional[datetime] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Any]]:
    """A successful step: move the token, set its next alarm, reset the
    failure counter (only CONSECUTIVE failures park a run), and flush the
    squares this visit finished (canon T26).

    The lease is the generation: a write under a stale lease is a no-op.
    A reply or a repeat that landed mid-visit moved wake_at, so this
    UPDATE matches nothing and the walker defers instead of clobbering
    the answer with the timeout path (P1) — and the buffered history is
    discarded with the move it belonged to.

    ``node_arrived_at`` is COALESCEd, not assigned: a WINDOWED HOLD calls
    this with its OWN node id (trap 1), and restamping there would write a
    zero-length step and reset the clock — "waiting since Friday" would
    render as "waiting since 9am". The walker passes None for a hold, and
    an empty ``steps`` with it."""
    query = f"""
        WITH moved AS (
            UPDATE {ENROLLMENT_TABLE}
            SET current_node = $2, wake_at = $3, context = $4::jsonb,
                node_arrived_at = COALESCE($5::timestamptz, node_arrived_at),
                attempts = 0, last_error = NULL
            WHERE id = $1 AND status = 'waiting' AND wake_at = $6
            RETURNING id, merchant_id, workflow_id, workflow_version
        ), wrote AS ({flush_arm("moved", "$7")})
        SELECT (SELECT id FROM moved) AS moved_id,
               (SELECT count(*) FROM wrote) AS steps
    """
    return query, [
        run_id,
        current_node,
        wake_at,
        json.dumps(context),
        node_arrived_at,
        leased_wake_at,
        json.dumps(steps or []),
    ]


def exit_run_query(
    run_id: str,
    exit_reason: str,
    current_node: Optional[str],
    context: Optional[Dict[str, Any]],
    leased_wake_at: datetime,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Any]]:
    """The WALKER's exit (timed_out, goal_met, completed, ejected). An exit
    without a context KEEPS the row's context: the exited row's pointers
    (source_event_id above all) are what source_event_used reads to
    refuse a replayed entry event — wiping them would let the spine's
    at-least-once redelivery enrol a second run from a stale checkout.

    The lease is the generation: a write under a stale lease is a no-op
    (P1). The event side's exit is cancel_run_query, unconditional.

    The exit CLOSES the square the run was standing on, so ``steps``
    carries it (canon T26): a completed chain flushes its whole visit with
    next_node = NULL on the last row, while timed_out / ejected / a goal
    tier flush the single square the token never left."""
    query = f"""
        WITH moved AS (
            UPDATE {ENROLLMENT_TABLE}
            SET status = 'exited', exit_reason = $2, exited_at = now(),
                wake_at = NULL,
                current_node = COALESCE($3, current_node),
                context = COALESCE($4::jsonb, context)
            WHERE id = $1 AND status <> 'exited' AND wake_at = $5
            RETURNING id, merchant_id, workflow_id, workflow_version
        ), wrote AS ({flush_arm("moved", "$6")})
        SELECT (SELECT id FROM moved) AS moved_id,
               (SELECT count(*) FROM wrote) AS steps
    """
    return query, [
        run_id,
        exit_reason,
        current_node,
        None if context is None else json.dumps(context),
        leased_wake_at,
        json.dumps(steps or []),
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


def open_runs_for_customer_query(
    merchant_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    """The consumer's per-run read (rollout phase 13): this customer's
    open runs across every plan, so goals and listening are judged
    against each run's PINNED version. The customer index carries it; a
    customer with nothing open costs one empty indexed read."""
    query = f"""
        SELECT {_RUN_COLUMNS}
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND customer_id = $2 AND status <> 'exited'
        ORDER BY entered_at, id
    """
    return query, [merchant_id, customer_id]


def resume_run_by_id_query(
    merchant_id: str,
    run_id: str,
    node_id: str,
    context_patch: Dict[str, Any],
    facts: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Any]]:
    """W5: the reply reaches the token — by run id (phase 13), because
    the listening square is the RUN'S version's, and a sibling run on
    another version must never be woken by a square it does not have.
    Only a run still standing on that square is touched — a late or
    repeated reply changes nothing. The answer is recorded on the run
    BEFORE anything fires (canon T20), and wake_at = now() hands it to
    the walker. Event-side: unconditional (the walker's writes defer to
    it, phase 03).

    Phase 16: the letter's scalar facts land under context.facts.<square>
    — namespaced, so two stages' payloads never collide; a second letter
    on the same square replaces that square's facts (anything that is not
    an object under `facts` — a legacy scalar a producer once sent — is
    replaced, never concatenated into an array). And a PARKED run
    hears its square too: an event is evidence the customer moved, so a
    parked run that hears it is no longer stuck on the thing that parked
    it — it becomes waiting with its failure counter forgiven (the human
    resume's semantics, now event-driven) and, as for any reply, its
    last_error cleared: the letter IS the step that unstuck it."""
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET context = context || $4::jsonb
                || jsonb_build_object('facts',
                       CASE WHEN jsonb_typeof(context->'facts') = 'object' THEN context->'facts' ELSE '{{}}'::jsonb END
                       || jsonb_build_object($3::text, $5::jsonb)),
            wake_at = now(),
            last_error = NULL,
            status = 'waiting',
            attempts = CASE WHEN status = 'parked' THEN 0 ELSE attempts END
        WHERE merchant_id = $1 AND id = $2
          AND status IN ('waiting', 'parked') AND current_node = $3
        RETURNING id
    """
    return query, [
        merchant_id,
        run_id,
        node_id,
        json.dumps(context_patch),
        json.dumps(facts or {}),
    ]


def refresh_run_facts_query(
    merchant_id: str,
    run_id: str,
    node_id: str,
    facts: Dict[str, Any],
    cut_short_by: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """A letter that finds the run on a square that listens to NOTHING —
    the door's start square before the walker's first visit, or an
    immediate square a walk parked on — has no square to answer, but its
    facts are still the newest word. They merge at the top level (the
    founding letter's place) and wake_at = now(), so a visit already in
    flight is refused at its write (its lease no longer matches, phase 03)
    and redone on these facts. The latest letter decides, never an earlier
    one: two events two seconds apart act once, on the second. Only an
    open run still on that square is touched; parked is forgiven exactly
    as a reply forgives it.

    ``cut_short_by`` (canon T26) is its OWN parameter and merges at the TOP
    level, deliberately not folded into ``facts``: on the reply path the
    same-shaped dict becomes ``context.facts.<square>``, and ``run_facts``
    flattens that namespace into template variables — a marker that drifted
    in there would ride into a customer's message. It stays here."""
    marker = ""
    params: List[Any] = [merchant_id, run_id, node_id, json.dumps(facts)]
    if cut_short_by:
        marker = "|| jsonb_build_object('cut_short_by', $5::text)"
        params.append(cut_short_by)
    query = f"""
        UPDATE {ENROLLMENT_TABLE}
        SET context = context || $4::jsonb {marker},
            wake_at = now(),
            last_error = NULL,
            status = 'waiting',
            attempts = CASE WHEN status = 'parked' THEN 0 ELSE attempts END
        WHERE merchant_id = $1 AND id = $2
          AND status IN ('waiting', 'parked') AND current_node = $3
        RETURNING id
    """
    return query, params


def cancel_run_query(
    merchant_id: str,
    run_id: str,
    exit_reason: str,
    occurred_at: Optional[datetime] = None,
    key: Optional[Tuple[str, str]] = None,
    context_patch: Optional[Dict[str, Any]] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Any]]:
    """Goal-cancel, by run id (phase 13): the tier that matched is the
    RUN'S version's, so the write names the run — a v3 goal never touches
    a sibling run on v5. Open is waiting or parked (canon: the goal event
    resolves OPEN enrolments; a parked run she has already satisfied must
    not keep holding the open-run unique). Event-side: unconditional (the
    walker's writes defer to it, phase 03).

    Time-aware on the ENTRY EVENT (G7, phase 06): only a run whose
    founding letter happened before the goal event ends — its context
    carries that moment (entered_event_at), with the row's insert time as
    the fallback for runs written before the stamp — so a late-delivered
    earlier-stage letter cannot keep a run alive past a goal that truly
    happened after it, and a stale goal redelivered later cannot end a
    newer run. An unstamped goal (occurred_at NULL) ends the run.

    Keyed (phase 06): ``key = (run field, value)`` re-asserts in the
    statement what the consumer judged from the row it read — the run is
    still the one the letter is about.

    ``context_patch`` (phase 09) rides the same UPDATE — context ||
    $n::jsonb — so the run remembers which letter ended it and what it
    was worth (context.goal), for the summary's recovered revenue. Its
    placeholder follows the optional key.

    ``steps`` is trap 3 (canon T26): this is the ONE writer outside the
    walker that ends a run. No claim, no visit, no lease — so without a
    flush here EVERY CONVERTED RUN, the ones that matter most, would lose
    its final square.

    The closing row is guarded, the exit is NOT. Ending the run is
    unconditional by law (the walker defers to the event side), but the row
    describes a snapshot the caller read a moment earlier: a walker that
    advanced in between would already have closed that square, and writing
    it again would duplicate it and name a square the run no longer stands
    on. So the exit always lands and the row lands only while the snapshot
    is still true — a lost closing row in a rare race, never a wrong one,
    and never a customer who bought still being nudged."""
    params: List[Any] = [merchant_id, run_id, exit_reason, occurred_at]
    keyed = ""
    if key:
        keyed = "AND context->>$5 = $6"
        params.extend([key[0], key[1]])
    patched = ""
    if context_patch is not None:
        patched = f", context = context || ${len(params) + 1}::jsonb"
        params.append(json.dumps(context_patch))
    steps_param = f"${len(params) + 1}"
    params.append(json.dumps(steps or []))
    query = f"""
        WITH moved AS (
            UPDATE {ENROLLMENT_TABLE}
            SET status = 'exited', exit_reason = $3, exited_at = now(),
                wake_at = NULL{patched}
            WHERE merchant_id = $1 AND id = $2
              AND status <> 'exited'
              AND ($4::timestamptz IS NULL OR COALESCE((context->>'entered_event_at')::timestamptz, entered_at) < $4::timestamptz)
              {keyed}
            RETURNING id, merchant_id, workflow_id, workflow_version,
                      current_node, node_arrived_at
        ), wrote AS (
            {flush_arm(
                "moved",
                steps_param,
                guard=(
                    "WHERE m.current_node = s.node "
                    "AND m.node_arrived_at IS NOT DISTINCT FROM s.arrived_at"
                ),
            )}
        )
        SELECT (SELECT id FROM moved) AS moved_id,
               (SELECT count(*) FROM wrote) AS steps
    """
    return query, params


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
    anywhere: bool = False,
) -> Tuple[str, List[Any]]:
    """Repeat entries (modules/05 §Repeat entries): ONE idempotent UPDATE
    in the reply's shape (resume_run_by_id_query). Touches only a run still
    standing on the door's start square (status waiting, current_node =
    the start) — a run past it is never patched — unless the door says
    restart_on_repeat (phase 16, G8: ``anywhere``): then a repeat of the
    door's topic re-arms whichever square the run stands on, "KYC retried,
    the timer restarts". Found by enrollment_key so a keyed plan's order
    edit patches ITS order's run. The event marks itself used in
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
          AND status = 'waiting' AND ($11::boolean OR current_node = $4)
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
        anywhere,
    ]


def get_run_query(
    merchant_id: str, workflow_id: str, run_id: str
) -> Tuple[str, List[Any]]:
    """One run, by id — what the timeline read unions its open square from
    (canon T26, law 3). Tenancy and the plan are both predicates: the route
    names a workflow, and a run id from another plan is a 404, not a row."""
    query = f"""
        SELECT {_RUN_COLUMNS}
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2 AND id = $3
    """
    return query, [merchant_id, workflow_id, run_id]


def _like_pattern(text: str) -> str:
    """A LIKE pattern that matches ``text`` literally anywhere: the three
    LIKE metacharacters are escaped (ESCAPE '\\' in the statement)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# The runs table's filters, shared by the page read and the count read so
# the two can never disagree on what "matching" means. $1/$2 are the plan;
# $3–$10 the filters; $11/$12 the anchor (see list_runs_query). The page
# read's LIMIT/OFFSET come AFTER, as $13/$14, so the count read binds
# exactly $1–$12 and pads nothing.
_RUN_FILTERS = """
              AND ($3::text IS NULL OR status = $3::text)
              AND ($4::text IS NULL OR current_node = $4::text)
              AND ($5::int IS NULL OR workflow_version = $5::int)
              AND ($6::text IS NULL OR exit_reason = $6::text)
              AND ($7::text IS NULL
                   OR enrollment_key ILIKE $7::text ESCAPE '\\'
                   OR context->>'customer_name' ILIKE $7::text ESCAPE '\\'
                   OR ($8::text IS NOT NULL AND context->>'phone' LIKE $8::text))
              AND ($9::timestamptz IS NULL OR entered_at >= $9::timestamptz)
              AND ($10::timestamptz IS NULL OR entered_at < $10::timestamptz)
              AND ($11::timestamptz IS NULL
                   OR (entered_at, id) <= ($11::timestamptz, $12::uuid))
"""


def _run_filter_values(
    merchant_id: str,
    workflow_id: str,
    status: Optional[str],
    node: Optional[str],
    version: Optional[int],
    exit_reason: Optional[str],
    search: Optional[str],
    since: Optional[datetime],
    until: Optional[datetime],
    anchor_entered_at: Optional[datetime],
    anchor_id: Optional[str],
) -> List[Any]:
    """$1–$12: the plan, the filters and the anchor — the count read's whole
    list; the page read appends its LIMIT/OFFSET as $13/$14."""
    phone_digits = "".join(ch for ch in (search or "") if ch.isdigit())
    return [
        merchant_id,
        workflow_id,
        status,
        node,
        version,
        exit_reason,
        _like_pattern(search) if search else None,
        f"%{phone_digits}%" if len(phone_digits) >= 4 else None,
        since,
        until,
        anchor_entered_at,
        anchor_id,
    ]


def list_runs_query(
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
) -> Tuple[str, List[Any]]:
    """The ops read behind canon's 'last_error readable on the merchant's
    screen': a flow's runs, newest first, optionally narrowed (status —
    parked = the triage view —, the square it stands on, its version, how
    it ended, a search over key / customer name / phone digits, and a
    window of entered_at).

    The list is newest first and the plan keeps taking runs, so an offset
    alone drifts: a run that enters between two page reads shifts every
    row down one and the next page repeats a row. The ANCHOR fixes that:
    (anchor_entered_at, anchor_id) is the newest row the client saw on its
    first page, and every later page reads only rows at or before it in
    (entered_at, id) order — a keyset on the very fields the sort uses,
    with id as the tie-breaker — so what entered later cannot move the
    pages underneath the reader. Offset then pages inside that fixed set.

    Each row also says which of its key's runs of this plan it is ("Run 3
    of 3"): numbered over EVERY run the plan holds for that key, bound only
    by merchant and plan, so a filter never renumbers a run. The numbering
    is done for the PAGE's rows only — two correlated counts per row on
    the (merchant_id, workflow_id, enrollment_key, entered_at, id) index
    (075) — never as a window over the whole plan, which would sort every
    run the plan ever had on every page turn. Each row carries the filtered
    total (count(*) OVER ()), so a page and its "1–10 of 46" come from one
    statement; an empty page has no rows to carry it, and the accessor
    asks count_runs_query instead. A NULL filter is no filter."""
    query = f"""
        WITH page AS (
            SELECT {_RUN_COLUMNS}, count(*) OVER () AS total
            FROM {ENROLLMENT_TABLE}
            WHERE merchant_id = $1 AND workflow_id = $2
              {_RUN_FILTERS}
            ORDER BY entered_at DESC, id DESC
            LIMIT $13 OFFSET $14
        )
        SELECT p.*,
               (SELECT count(*) FROM {ENROLLMENT_TABLE} k
                WHERE k.merchant_id = $1 AND k.workflow_id = $2
                  AND k.enrollment_key = p.enrollment_key
                  AND (k.entered_at, k.id) <= (p.entered_at, p.id))::int AS run_number,
               (SELECT count(*) FROM {ENROLLMENT_TABLE} k
                WHERE k.merchant_id = $1 AND k.workflow_id = $2
                  AND k.enrollment_key = p.enrollment_key)::int AS runs_for_key
        FROM page p
        ORDER BY p.entered_at DESC, p.id DESC
    """
    return query, _run_filter_values(
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
    ) + [limit, offset]


def count_runs_query(
    merchant_id: str,
    workflow_id: str,
    status: Optional[str],
    node: Optional[str] = None,
    version: Optional[int] = None,
    exit_reason: Optional[str] = None,
    search: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    anchor_entered_at: Optional[datetime] = None,
    anchor_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """How many runs match list_runs_query's filters — the same fragment,
    no page. Read only when a page comes back empty (an offset past the
    last match), where count(*) OVER () has no row to ride on and the
    page's ``total`` would otherwise say 0 for a list that is not.
    Binds $1–$12 exactly: the same list the page read starts from."""
    query = f"""
        SELECT count(*)::int AS total
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2
          {_RUN_FILTERS}
    """
    return query, _run_filter_values(
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


def runs_per_day_query(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
    tz: str,
) -> Tuple[str, List[Any]]:
    """Runs that entered per calendar day in ``tz`` over the summary's
    window — the list sparkline. Only days with runs come back."""
    query = f"""
        SELECT (entered_at AT TIME ZONE $5)::date AS day, count(*)::int AS runs
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2
          AND ($3::timestamptz IS NULL OR entered_at >= $3::timestamptz)
          AND ($4::timestamptz IS NULL OR entered_at < $4::timestamptz)
        GROUP BY 1
        ORDER BY 1
    """
    return query, [merchant_id, workflow_id, since, until, tz]


def open_by_node_query(merchant_id: str, workflow_id: str) -> Tuple[str, List[Any]]:
    """Open runs (waiting + parked = not exited, the CHECK's third value)
    by the square they stand on now. `status <> 'exited'` is spelled the
    way the open-runs partial index (075) is, so the read walks only the
    open rows — a plan's history grows without bound, its open set does
    not."""
    query = f"""
        SELECT current_node, count(*)::int AS runs
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2
          AND status <> 'exited'
        GROUP BY current_node
    """
    return query, [merchant_id, workflow_id]


def run_endings_in_window_query(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> Tuple[str, List[Any]]:
    """Every run of a plan that ENTERED in the window, with how it stands
    and when it ended — the report's raw material.

    The id rides along because the split the report is about (did a
    conversation happen before this ended?) lives in the lead store, and
    the contract there takes ids; exited_at is the moment that split is
    judged against. Windowed on entered_at, never on exited_at: a report
    answers "of the people who came in during this window, what happened",
    so a run that entered inside it and ended after it still belongs to
    the window it entered.

    entered_at rides along too: a retry lead carries no enrollment_id
    (only the customer's request_id), so the lead read finds it by
    request_id inside the run's own lifetime. current_node names the
    square an open run stands on — the report's "still open, by square"."""
    query = f"""
        SELECT id, enrollment_key, status, exit_reason, exited_at,
               entered_at, current_node
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2
          AND ($3::timestamptz IS NULL OR entered_at >= $3::timestamptz)
          AND ($4::timestamptz IS NULL OR entered_at < $4::timestamptz)
    """
    return query, [merchant_id, workflow_id, since, until]


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


def workflow_summary_query(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> Tuple[str, List[Any]]:
    """The plan's report (rollout phase 09, G9) in ONE statement. Grouping
    sets give two kinds of row at once: one per (status, exit_reason) —
    the open counts and the exits by reason — and the () row for the whole
    window: total runs, the median minutes from entry to exit over the
    finished ones, and the recovered amount (context.goal.amount summed
    over goal_met rows, behind a numeric regex so a stray value can never
    break the read). GROUPING() tells the decoder which row is which. The
    window bounds entered_at; NULL bounds mean all time."""
    query = f"""
        SELECT status, exit_reason,
               GROUPING(status, exit_reason) AS grouping_level,
               count(*) AS runs,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM (exited_at - entered_at)) / 60.0
               ) FILTER (WHERE status = 'exited') AS median_minutes_to_exit,
               sum(CASE WHEN exit_reason = 'goal_met'
                         AND (context->'goal'->>'amount') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                        THEN (context->'goal'->>'amount')::numeric END) AS recovered_amount
        FROM {ENROLLMENT_TABLE}
        WHERE merchant_id = $1 AND workflow_id = $2
          AND ($3::timestamptz IS NULL OR entered_at >= $3::timestamptz)
          AND ($4::timestamptz IS NULL OR entered_at < $4::timestamptz)
        GROUP BY GROUPING SETS ((status, exit_reason), ())
    """
    return query, [merchant_id, workflow_id, since, until]


def workflow_split_counts_query(
    merchant_id: str,
    workflow_id: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> Tuple[str, List[Any]]:
    """Runs per arm of each split square (enh A/04), over the same window
    as the summary above.

    Its OWN statement rather than another grouping set, because a run with
    two split squares expands to two rows here — folded into the main
    aggregate that would count it twice and quietly inflate `runs`. The
    keys are discovered from the context (`split_<node>`), never from the
    document, so a report needs no version read and an arm recorded by a
    version since edited still counts.

    ``jsonb_each_text`` is safe on any context: a non-object column cannot
    occur (the column is written as an object and 058 defaults it to one),
    and a run with no split contributes no rows at all.
    """
    query = f"""
        SELECT fact.key AS arm_key, fact.value AS arm, count(*) AS runs
        FROM {ENROLLMENT_TABLE} e
        CROSS JOIN LATERAL jsonb_each_text(e.context) AS fact(key, value)
        WHERE e.merchant_id = $1 AND e.workflow_id = $2
          AND ($3::timestamptz IS NULL OR e.entered_at >= $3::timestamptz)
          AND ($4::timestamptz IS NULL OR e.entered_at < $4::timestamptz)
          AND fact.key LIKE $5
        GROUP BY fact.key, fact.value
    """
    return query, [merchant_id, workflow_id, since, until, f"{SPLIT_PREFIX}%"]


_RUN_COLUMNS_OF_E = ", ".join(f"e.{c.strip()}" for c in _RUN_COLUMNS.split(","))


def customer_runs_query(
    merchant_id: str, customer_id: str, limit: int
) -> Tuple[str, List[Any]]:
    """The customer's journey (rollout phase 09): her runs across EVERY
    plan in the order they began, each naming its plan — the loan funnel's
    journey view while it runs as clocks. Both tables are outreach's; the
    customer index carries the read."""
    query = f"""
        SELECT {_RUN_COLUMNS_OF_E}, w.name AS workflow_name
        FROM {ENROLLMENT_TABLE} e
        JOIN {WORKFLOW_TABLE} w
          ON w.merchant_id = e.merchant_id AND w.id = e.workflow_id
        WHERE e.merchant_id = $1 AND e.customer_id = $2
        ORDER BY e.entered_at, e.id
        LIMIT $3
    """
    return query, [merchant_id, customer_id, limit]


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
