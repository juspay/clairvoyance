"""The dispatcher's retry policy, its plumbing, its SQL builders, and the DDL."""

import re
from datetime import datetime, timezone
from pathlib import Path

from app.core.config.static import _positive_float, _positive_int
from app.crm.connectivity import dispatch
from app.crm.connectivity.db.decoder import (
    _load_variables,
    decode_queued_message,
)
from app.crm.connectivity.db.queries import (
    CLAIMED_COLUMNS,
    apply_outcome_query,
    claim_queued_messages_query,
    requeue_stale_claims_query,
)
from app.crm.connectivity.dispatch import (
    REASON_ATTEMPTS_EXHAUSTED,
    REASON_PROVIDER_REJECTED,
    REASON_SEND_ERROR,
    RETRY_MAX_SECONDS,
    STATUS_ACCEPTED,
    STATUS_DEAD,
    STATUS_FAILED,
    STATUS_QUEUED,
    _jittered,
    backoff_seconds,
    plan_for_outcome,
    sample_ids,
)
from app.crm.connectivity.schemas import QueuedMessage, SendOutcome
from scripts.check_crm_boundaries import TABLE_OWNERS

MIGRATION = Path("app/database/migrations/056_create_crm_message.sql")


def _ddl() -> str:
    """The migration with comment prose stripped: a structural assertion that
    passes on the paragraph explaining an absence proves nothing."""
    return "\n".join(
        line
        for line in MIGRATION.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


# --- the retry policy -------------------------------------------------------


def test_accepted_records_the_provider_id_and_stamps_sent() -> None:
    plan = plan_for_outcome(
        SendOutcome(status="accepted", provider_message_id="wamid.X"), 1, 3
    )
    assert plan.status == STATUS_ACCEPTED
    assert plan.provider_message_id == "wamid.X"
    assert plan.mark_sent is True
    assert plan.reason is None


def test_retryable_failure_goes_back_on_the_queue() -> None:
    plan = plan_for_outcome(
        SendOutcome(status="failed", reason="rate_limited", retryable=True), 1, 3
    )
    assert plan.status == STATUS_QUEUED
    assert plan.reason == "rate_limited"
    assert plan.mark_sent is False


def test_retryable_failure_goes_dead_on_the_last_attempt() -> None:
    plan = plan_for_outcome(
        SendOutcome(status="failed", reason="rate_limited", retryable=True), 3, 3
    )
    # 'dead' (we stopped trying), not 'failed' (the wire said no).
    assert plan.status == STATUS_DEAD
    assert plan.reason == REASON_ATTEMPTS_EXHAUSTED


def test_permanent_failure_never_retries_however_many_attempts_remain() -> None:
    plan = plan_for_outcome(
        SendOutcome(status="failed", reason="template_not_approved"), 1, 99
    )
    assert plan.status == STATUS_FAILED
    assert plan.reason == "template_not_approved"


def test_failure_without_a_reason_still_records_one() -> None:
    plan = plan_for_outcome(SendOutcome(status="failed"), 1, 3)
    assert plan.reason == REASON_PROVIDER_REJECTED


def test_a_send_that_never_reports_cannot_be_marked_sent() -> None:
    for outcome in (
        SendOutcome(status="failed", reason="boom", retryable=True),
        SendOutcome(status="failed", reason="boom"),
    ):
        assert plan_for_outcome(outcome, 1, 3).mark_sent is False


# --- the plumbing never raises -----------------------------------------------
# The policy (plan_for_outcome) is pure and pinned above; these pin the paths
# AROUND it — the never-raises contract that keeps a claimed batch alive.


def _message(attempt: int = 1) -> QueuedMessage:
    return QueuedMessage(
        id="m-1",
        merchant_id="m-1000",
        customer_id="c-1",
        channel="whatsapp",
        sent_to_address="+919812345678",
        source_kind="broadcast",
        purpose_key="utility.order_update",
        dedupe_key="k-1",
        attempt=attempt,
        next_attempt_at=datetime.now(timezone.utc),
    )


async def test_an_accepted_send_records_the_outcome(monkeypatch) -> None:
    written = {}

    async def fake_send(message):
        written["sent"] = message.id
        return SendOutcome(status="accepted", provider_message_id="wamid.T")

    async def record_outcome(message_id, status, reason, pmid, mark_sent, retry):
        written.update(status=status, mark_sent=mark_sent)
        return True

    monkeypatch.setattr(dispatch, "send", fake_send)
    monkeypatch.setattr(dispatch.accessor, "apply_outcome", record_outcome)
    await dispatch._dispatch_one(_message(), 3)
    assert written["sent"] == "m-1"
    assert written["status"] == STATUS_ACCEPTED
    assert written["mark_sent"] is True


async def test_a_raising_send_becomes_a_retryable_send_error(monkeypatch) -> None:
    written = {}

    async def broken_send(message):
        raise RuntimeError("wire fell over")

    async def record_outcome(message_id, status, reason, pmid, mark_sent, retry):
        written.update(status=status, reason=reason, retry=retry)
        return True

    monkeypatch.setattr(dispatch, "send", broken_send)
    monkeypatch.setattr(dispatch.accessor, "apply_outcome", record_outcome)
    await dispatch._dispatch_one(_message(attempt=1), 3)
    # We don't know whether the provider saw it, so it requeues with a delay.
    assert written["status"] == STATUS_QUEUED
    assert written["reason"] == REASON_SEND_ERROR
    assert written["retry"] is not None and written["retry"] >= 1


async def test_a_reclaimed_row_discards_the_late_outcome(monkeypatch) -> None:
    calls = []

    async def fake_send(message):
        return SendOutcome(status="accepted", provider_message_id="wamid.L")

    async def reclaimed(message_id, status, reason, pmid, mark_sent, retry):
        calls.append(status)
        return False  # the sweep already handed this row to another worker

    monkeypatch.setattr(dispatch, "send", fake_send)
    monkeypatch.setattr(dispatch.accessor, "apply_outcome", reclaimed)
    await dispatch._dispatch_one(_message(), 3)
    # Discarded means one write attempt, no retry, no raise — their row now.
    assert calls == [STATUS_ACCEPTED]


def test_jitter_stays_in_bounds_and_never_reaches_zero() -> None:
    assert _jittered(None) is None
    for _ in range(500):
        spread = _jittered(30)
        assert spread is not None and 24 <= spread <= 36  # +-20% of 30
        # An instant retry is the one thing jitter must never produce.
        floor = _jittered(1)
        assert floor is not None and floor >= 1


# --- the SQL builders -------------------------------------------------------


def test_claim_is_race_safe_across_pods() -> None:
    query, values = claim_queued_messages_query(25)
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "status = 'queued'" in query
    assert "LIMIT $1" in query
    assert values == [25]


# --- log lines stay bounded --------------------------------------------------


def test_sample_ids_lists_everything_when_it_fits() -> None:
    assert sample_ids(["a", "b"], limit=10) == "a, b"
    assert sample_ids([], limit=10) == ""


def test_sample_ids_truncates_and_says_how_many_it_hid() -> None:
    # Batch size is operator-tunable and the stale sweep is unbounded, so an
    # uncapped join could emit a multi-megabyte log record.
    out = sample_ids([f"id-{n}" for n in range(1000)], limit=3)
    assert out == "id-0, id-1, id-2 … +997 more"
    assert len(out) < 200


# --- the worker role --------------------------------------------------------
# Loop mechanics (pacing, backoff, heartbeat, per-row isolation) belong to
# the shared scaffold and are pinned in test_event_worker.py; here we pin
# only that the dispatcher is wired into it.


def test_dispatcher_is_a_registered_worker_role() -> None:
    # start_worker_role raises on an unregistered role, so without this entry
    # a CRM_ROLE=dispatcher pod crashes at boot.
    from app.crm.worker_main import ROLES

    assert "dispatcher" in ROLES


def test_contracts_export_the_scaffold_pair() -> None:
    # worker_main may import only the module's contracts — the claim/handle
    # pair must be reachable there, not by deep import.
    from app.crm.connectivity import contracts

    assert callable(contracts.claim_sends)
    assert callable(contracts.dispatch_send)


# --- the dials read from the environment -------------------------------------


def test_a_bad_dial_falls_back_instead_of_stopping_the_pod(monkeypatch) -> None:
    # Lenient by choice: raising here would fail the pod at import, and a
    # mistyped retry dial should not be able to do that.
    for bad in ("", "fast", "1.5", "0", "-1"):
        monkeypatch.setenv("DIAL", bad)
        assert _positive_int("DIAL", 25) == 25
    monkeypatch.delenv("DIAL")
    assert _positive_int("DIAL", 25) == 25
    monkeypatch.setenv("DIAL", "50")
    assert _positive_int("DIAL", 25) == 50


def test_a_bad_float_dial_falls_back_too(monkeypatch) -> None:
    # Guards CRM_WORKER_INTERVAL/HEARTBEAT. 'inf' is the dangerous one: it
    # parses, it compares > 0, and an infinite poll interval is a worker that
    # sleeps forever after its first empty poll.
    for bad in ("", "slow", "0", "-0.5", "inf", "-inf", "nan"):
        monkeypatch.setenv("DIAL", bad)
        assert _positive_float("DIAL", 1.0) == 1.0
    monkeypatch.delenv("DIAL")
    assert _positive_float("DIAL", 1.0) == 1.0
    monkeypatch.setenv("DIAL", "0.25")
    assert _positive_float("DIAL", 1.0) == 0.25


def test_retry_waits_longer_each_attempt_then_caps() -> None:
    # Retrying a rate-limited send instantly is the one response guaranteed
    # to make it fail again.
    assert backoff_seconds(1, 30, RETRY_MAX_SECONDS) == 30
    assert backoff_seconds(2, 30, RETRY_MAX_SECONDS) == 60
    assert backoff_seconds(3, 30, RETRY_MAX_SECONDS) == 120
    # The ceiling only binds if MAX_ATTEMPTS is raised well past its default.
    assert backoff_seconds(9, 30, RETRY_MAX_SECONDS) == RETRY_MAX_SECONDS


def test_requeue_carries_a_delay_and_terminal_outcomes_do_not() -> None:
    requeued = plan_for_outcome(
        SendOutcome(status="failed", reason="131049", retryable=True), 1, 3
    )
    assert requeued.status == STATUS_QUEUED
    assert requeued.retry_after_seconds and requeued.retry_after_seconds > 0

    for terminal in (
        plan_for_outcome(SendOutcome(status="accepted"), 1, 3),
        plan_for_outcome(SendOutcome(status="failed", reason="bad_template"), 1, 3),
        plan_for_outcome(
            SendOutcome(status="failed", reason="131049", retryable=True), 3, 3
        ),
    ):
        assert terminal.retry_after_seconds is None


def test_claim_returns_when_rows_came_due_for_the_lag_metric() -> None:
    # The lag log line reads next_attempt_at off the claimed row; dropping it
    # from CLAIMED_COLUMNS would fail the decoder on every claimed batch.
    assert "next_attempt_at" in CLAIMED_COLUMNS


def test_queue_filters_and_orders_by_when_a_row_is_due() -> None:
    # Ordering by created_at would send a requeued row to the FRONT on its
    # original timestamp, retrying it ahead of every fresh message.
    query, _ = claim_queued_messages_query(25)
    assert "next_attempt_at <= now()" in query
    assert "ORDER BY next_attempt_at" in query
    assert "ORDER BY created_at" not in query


def test_only_a_requeue_moves_the_next_attempt_time() -> None:
    query, values = apply_outcome_query(
        "m-1", "queued", "rate_limited", None, False, 30
    )
    assert "make_interval(secs => $6::int)" in query
    assert values[5] == 30


def test_claim_burns_an_attempt_so_a_dead_worker_cannot_loop_forever() -> None:
    query, _ = claim_queued_messages_query(10)
    assert "attempt = attempt + 1" in query


def test_outcome_write_is_guarded_by_the_claim() -> None:
    query, values = apply_outcome_query("m-1", "accepted", None, "wamid.X", True, None)
    assert "status = 'sending'" in query
    assert values == ["m-1", "accepted", None, "wamid.X", True, None]


def test_outcome_write_never_erases_a_known_provider_id() -> None:
    query, _ = apply_outcome_query("m-1", "queued", "rate_limited", None, False, 30)
    assert "COALESCE($4, provider_message_id)" in query


def test_stale_sweep_targets_only_expired_claims() -> None:
    query, values = requeue_stale_claims_query(5)
    assert "status = 'sending'" in query
    assert "make_interval(mins => $1::int)" in query
    assert values == [5]


def test_builders_parameterize_every_value() -> None:
    # A mismatch is an unbound $n or, worse, a value that got interpolated.
    for query, values in (
        claim_queued_messages_query(25),
        requeue_stale_claims_query(5),
        apply_outcome_query("m-1", "failed", "nope", None, False, None),
    ):
        placeholders = set(re.findall(r"\$(\d+)", query))
        assert placeholders == {str(n) for n in range(1, len(values) + 1)}


# --- the decoder can never stop the queue -----------------------------------


def test_load_variables_is_total() -> None:
    # A raise here would strand a whole claimed batch in 'sending', forever.
    cases = [
        ('{"name":"Priya"}', {"name": "Priya"}),  # the live path
        ("{}", {}),
        ('{"a":{"b":1}}', {"a": {"b": 1}}),
        ("42", {}),
        ("[1,2]", {}),
        ("null", {}),
        ('"hi"', {}),
        ("not json at all", {}),
        ("", {}),
        (None, {}),
        ({"a": 1}, {"a": 1}),  # if a jsonb codec is ever registered
        (42, {}),
        (3.14, {}),
        (True, {}),
        (["a", "b"], {}),
    ]
    for value, expected in cases:
        assert _load_variables(value) == expected, value


def test_load_variables_refuses_to_invent_variables() -> None:
    # dict([["a", 1]]) == {"a": 1} would post a variable nobody wrote.
    assert _load_variables([["a", 1]]) == {}
    assert _load_variables([("a", 1)]) == {}


def test_decoded_variables_do_not_alias_the_row() -> None:
    row = {"a": 1}
    decoded = _load_variables(row)
    decoded["b"] = 2
    assert row == {"a": 1}


# --- the table --------------------------------------------------------------


def test_manifest_is_owned_by_connectivity() -> None:
    assert TABLE_OWNERS["crm_message"] == "connectivity"


def test_dedupe_key_is_mandatory_and_unique_per_merchant() -> None:
    # The only thing between a crash-retry and a duplicate send, so it covers
    # every row: NOT NULL with no default, and a total (not partial) unique.
    sql = _ddl()
    assert "dedupe_key          text NOT NULL" in sql
    assert "crm_message (merchant_id, dedupe_key);" in sql
    assert "WHERE dedupe_key IS NOT NULL" not in sql


def test_expected_columns_are_all_present() -> None:
    sql = _ddl()
    for column in (
        "binding_id",
        "purpose_key",
        "cost_micros",
        "decision_id",
        "attempt",
        "source_kind",
        "source_id",
        "provider_message_id",
        "delivered_at",
        "read_at",
    ):
        assert column in sql, column


def test_decision_id_matches_the_diary_pages() -> None:
    # The permission decision table keys on a bigserial; a uuid would not join.
    sql = _ddl()
    assert "decision_id         bigint" in sql


def test_variables_shape_is_enforced_in_code_not_in_the_table() -> None:
    # Any JSON value is a legal row; _load_variables neutralises the rest.
    sql = _ddl()
    assert "variables           jsonb NOT NULL" in sql
    assert "jsonb_typeof" not in sql


def test_purpose_key_cannot_be_null() -> None:
    # A mandatory gate input, and missing inputs must fail closed.
    sql = _ddl()
    assert "purpose_key         text NOT NULL" in sql


def test_proposal_fields_are_immutable_by_trigger() -> None:
    sql = _ddl()
    assert "crm_message_immutable_guard" in sql
    for frozen in ("sent_to_address", "purpose_key", "variables", "dedupe_key"):
        assert f"NEW.{frozen}" in sql, frozen
    # The envelope must stay writable, or the dispatcher cannot work.
    for mutable in ("status", "claimed_at", "next_attempt_at", "provider_message_id"):
        assert f"NEW.{mutable} " not in sql, mutable


def test_the_work_queue_is_a_partial_index() -> None:
    sql = _ddl()
    assert "crm_message_queued_ix" in sql
    assert "WHERE status = 'queued'" in sql
    assert "crm_message_claimed_ix" in sql


def test_index_set_is_exactly_what_is_read() -> None:
    # Three for the loop, two for analytics. The delivery-receipt index ships
    # with the walker that queries it — an unused index is a write cost.
    sql = _ddl()
    created = set(re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)", sql))
    assert created == {
        "crm_message_merchant_dedupe_uq",
        "crm_message_queued_ix",
        "crm_message_claimed_ix",
        "crm_message_merchant_created_ix",
        "crm_message_merchant_customer_ix",
        "crm_message_provider_id_uq",
    }


def test_one_provider_message_is_one_row() -> None:
    # A write rule, not a read index: recording the same provider id twice
    # must be impossible. Not merchant-scoped on purpose — a provider id is
    # globally unique, so scoping it per tenant would let two merchants each
    # record the same real message.
    sql = _ddl()
    assert "crm_message (provider_message_id)" in sql
    assert "(merchant_id, provider_message_id)" not in sql


def test_analytics_indexes_are_merchant_first_and_time_ordered() -> None:
    # merchant-first makes a tenant's slice a range scan, not a filter over
    # everyone; created_at DESC matches how every report reads.
    sql = _ddl()
    assert "crm_message (merchant_id, created_at DESC)" in sql
    assert "crm_message (merchant_id, customer_id, created_at DESC)" in sql


def test_no_touch_trigger_because_there_is_no_updated_at() -> None:
    # Every state stamps its own clock; same choice as crm_event_raw (051).
    sql = _ddl()
    assert "updated_at" not in sql
    assert "crm_touch_updated_at" not in sql
    assert "crm_message_immutable_guard" in sql


def test_status_is_a_closed_enum_but_channel_is_not() -> None:
    # Format CHECKs are required; vocabulary CHECKs are the 027 scar.
    sql = _ddl()
    assert "CHECK (status IN (" in sql
    for state in ("queued", "sending", "blocked", "accepted", "sent", "dead"):
        assert f"'{state}'" in sql, state
    # Vocabulary in a CHECK is the migration-027 scar.
    assert "CHECK (channel" not in sql
    assert "CHECK (source_kind" not in sql


def test_customer_fk_is_composite_so_a_message_cannot_cross_tenants() -> None:
    sql = _ddl()
    assert "FOREIGN KEY (merchant_id, customer_id)" in sql
    assert "REFERENCES crm_customer (merchant_id, id)" in sql
