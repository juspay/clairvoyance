"""Query-builder laws for the outreach module: $1 params only, tenancy
predicates present, the claim's lease semantics, idempotent stamps."""

import json
from datetime import datetime, timezone

from app.crm.outreach.db.queries import (
    admission_facts_query,
    advance_run_query,
    cancel_run_query,
    claim_due_runs_query,
    exit_run_query,
    insert_enrollment_query,
    live_workflows_query,
    open_runs_for_customer_query,
    park_run_query,
    publish_workflow_query,
    record_run_error_query,
    resume_run_by_id_query,
    source_event_used_query,
)
from app.crm.record.db.queries import customer_has_event_query

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


def test_claim_is_lease_and_attempt_in_one_statement() -> None:
    sql, params = claim_due_runs_query(25, 300)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "make_interval(secs => $2)" in sql
    assert "attempts = attempts + 1" in sql
    assert "e.status = 'waiting' AND e.wake_at <= now()" in sql
    assert params == [25, 300]


def test_enrollment_insert_binds_everything_positionally() -> None:
    sql, params = insert_enrollment_query(
        "m1", "wf-1", 3, "c-1", "wait-30m", NOW, {"phone": "+91"}, "c-1"
    )
    assert "$8" in sql and "$9" not in sql
    assert params[0] == "m1"  # merchant first — tenancy reads first
    assert json.loads(params[6]) == {"phone": "+91"}


def test_goal_cancel_ends_every_open_run_including_parked() -> None:
    sql, params = cancel_run_query("m1", "run-1", "goal_met")
    assert "status <> 'exited'" in sql and "status = 'waiting'" not in sql
    assert params[0] == "m1"


def test_goal_cancel_and_reply_name_the_run_they_are_about() -> None:
    """Phase 13: the tier or listening node that matched is the RUN'S
    version's, so the write names the run — merchant first, then id — and
    a sibling run on another version is never touched."""
    sql, params = cancel_run_query("m1", "run-1", "goal_met")
    assert "WHERE merchant_id = $1 AND id = $2" in sql
    assert params[:2] == ["m1", "run-1"]
    sql, params = resume_run_by_id_query("m1", "run-1", "ask", {"reply_ask": "YES"})
    assert "WHERE merchant_id = $1 AND id = $2" in sql
    assert "status = 'waiting' AND current_node = $3" in sql
    assert "RETURNING id" in sql
    assert params == ["m1", "run-1", "ask", json.dumps({"reply_ask": "YES"})]


def test_open_runs_for_customer_is_merchant_first_and_open_only() -> None:
    sql, params = open_runs_for_customer_query("m1", "c-1")
    assert "WHERE merchant_id = $1 AND customer_id = $2" in sql
    assert "status <> 'exited'" in sql
    assert params == ["m1", "c-1"]


def test_goal_cancel_is_time_aware_on_the_entry_event_and_null_safe() -> None:
    """G7 (rollout phase 06): 'after the run began' means after the ENTRY
    EVENT happened, not after the row was inserted — a late-delivered
    earlier-stage letter must not keep a run alive past a goal that truly
    happened after it. entered_at stays the fallback for older rows."""
    sql, params = cancel_run_query("m1", "run-1", "goal_met", NOW)
    assert (
        "COALESCE((context->>'entered_event_at')::timestamptz, entered_at) "
        "< $4::timestamptz" in sql
    )
    assert "$4::timestamptz IS NULL OR" in sql
    assert params[3] == NOW
    _, params = cancel_run_query("m1", "run-1", "goal_met")
    assert params[3] is None  # unstamped goal keeps today's behaviour


def test_goal_cancel_can_be_keyed_to_the_run_it_is_about() -> None:
    """A keyed tier ends only the run whose context field equals the
    letter's payload field (cart_token = cart_token); the unkeyed form is
    byte-identical to before."""
    sql, params = cancel_run_query(
        "m1", "run-1", "goal_met", NOW, key=("cart_token", "chk-88412")
    )
    assert "AND context->>$5 = $6" in sql
    assert params[4:] == ["cart_token", "chk-88412"]
    sql, params = cancel_run_query("m1", "run-1", "converted_elsewhere")
    assert "$5" not in sql and len(params) == 4


def test_goal_recheck_can_be_keyed_to_the_run_it_is_about() -> None:
    sql, params = customer_has_event_query(
        "m1", "c-1", ["orders/create"], NOW, where=("cart_token", "chk-88412")
    )
    assert "AND payload->>$5 = $6" in sql
    assert params == ["m1", "c-1", ["orders/create"], NOW, "cart_token", "chk-88412"]
    sql, params = customer_has_event_query("m1", "c-1", ["orders/create"], NOW)
    assert "$5" not in sql and len(params) == 4


def test_live_workflows_read_is_merchant_scoped() -> None:
    sql, params = live_workflows_query("m1")
    assert "merchant_id = $1 AND status = 'live'" in sql
    assert params == ["m1"]


def test_publish_requires_a_draft_to_exist() -> None:
    sql, _ = publish_workflow_query("m1", "wf-1")
    assert "draft IS NOT NULL" in sql
    assert "version = version + 1" in sql


def test_park_only_moves_waiting_runs() -> None:
    sql, _ = park_run_query("en-1", "boom", NOW)
    assert "status = 'waiting'" in sql and "'parked'" in sql


def test_exit_never_reexits() -> None:
    sql, _ = exit_run_query("en-1", "completed", None, {}, NOW)
    assert "status <> 'exited'" in sql


def test_exit_without_context_keeps_the_rows_pointers() -> None:
    sql, params = exit_run_query("en-1", "goal_met", None, None, NOW)
    assert "context = COALESCE($4::jsonb, context)" in sql
    assert params[3] is None  # NULL -> COALESCE keeps source_event_id
    _, params = exit_run_query("en-1", "completed", "n1", {"reply_x": "YES"}, NOW)
    assert json.loads(params[3]) == {"reply_x": "YES"}


def test_walker_writes_are_conditional_on_the_leased_wake_at() -> None:
    """P1 (rollout phase 03): the claim's wake_at is the generation token.
    Every event-side writer (a reply, a repeat patch) moves wake_at, so a
    walker write under a stale lease matches zero rows instead of
    clobbering the reply — and RETURNING id is how the walker learns it."""
    leased = NOW
    for sql, params, placeholder in (
        (*advance_run_query("r-1", "wait-1d", NOW, {"k": 1}, leased), "$5"),
        (*exit_run_query("r-1", "completed", None, None, leased), "$5"),
        (*park_run_query("r-1", "boom", leased), "$3"),
        (*record_run_error_query("r-1", "boom", 600, leased), "$4"),
    ):
        assert f"AND wake_at = {placeholder}" in sql, sql
        assert "RETURNING id" in sql, sql
        assert params[-1] is leased


def test_event_side_writes_stay_unconditional() -> None:
    """The reply and the goal-cancel are the event side: they always win
    over a walker mid-visit (the walker defers on its CAS miss)."""
    sql, _ = cancel_run_query("m1", "run-1", "goal_met")
    assert "AND wake_at =" not in sql
    sql, _ = resume_run_by_id_query("m1", "run-1", "ask", {"reply_ask": "YES"})
    assert "AND wake_at =" not in sql


def test_admission_and_source_reads_are_merchant_first() -> None:
    for sql, params in (
        admission_facts_query("m1", "wf-1", "c-1"),
        source_event_used_query("m1", "wf-1", "c-1", "e-1"),
    ):
        assert "merchant_id = $1" in sql
        assert params[0] == "m1"


def test_admission_facts_scope_to_the_key_on_keyed_plans() -> None:
    """B2 (rollout phase 02): entry.key says runs are per <field>, so the
    history the reenter/cooldown guards judge is that KEY's, not the
    customer's whole history — otherwise her second order is refused as a
    re-entry. The customer predicate stays, for tenancy paranoia."""
    sql, params = admission_facts_query("m1", "wf-1", "c-1", enrollment_key="ORD-2")
    assert "enrollment_key = $3" in sql and "customer_id = $4" in sql
    assert params == ["m1", "wf-1", "ORD-2", "c-1"]
    # the unkeyed form is untouched
    sql, params = admission_facts_query("m1", "wf-1", "c-1")
    assert "enrollment_key" not in sql and "customer_id = $3" in sql
    assert params == ["m1", "wf-1", "c-1"]


def test_claim_skips_paused_plans_and_counts_the_claim() -> None:
    sql, values = claim_due_runs_query(50, 300)
    assert "w.status = 'paused'" in sql and "NOT EXISTS" in sql
    assert "attempts = attempts + 1" in sql
    assert values == [50, 300]


def test_transient_error_writes_the_retry_into_wake_at() -> None:
    sql, values = record_run_error_query("r-1", "boom", 600, NOW)
    assert "wake_at = now() + make_interval(secs => $3)" in sql
    assert values == ["r-1", "boom", 600, NOW]


def test_goal_recheck_survives_a_null_occurred_at() -> None:
    sql, params = customer_has_event_query("m1", "c-1", ["order.placed"], NOW)
    assert "COALESCE(occurred_at, received_at) > $4" in sql
    assert params == ["m1", "c-1", ["order.placed"], NOW]
