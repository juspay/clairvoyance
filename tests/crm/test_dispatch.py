"""The dispatcher's retry policy, its plumbing, its SQL builders, and the DDL."""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

from app.core.config.static import _positive_float, _positive_int
from app.crm.connectivity import dispatch
from app.crm.connectivity.channels import CHANNELS, gate_handle_kind_for
from app.crm.connectivity.db.queries.message import (
    CLAIMED_COLUMNS,
    apply_outcome_query,
    claim_queued_messages_query,
    requeue_stale_claims_query,
)
from app.crm.connectivity.dispatch import (
    REASON_ATTEMPTS_EXHAUSTED,
    REASON_GATE_UNAVAILABLE,
    REASON_PROVIDER_REJECTED,
    REASON_SEND_ERROR,
    REASON_SUPPRESSED,
    RETRY_MAX_SECONDS,
    STATUS_ACCEPTED,
    STATUS_BLOCKED,
    STATUS_DEAD,
    STATUS_FAILED,
    STATUS_QUEUED,
    _jittered,
    backoff_seconds,
    plan_for_outcome,
    sample_ids,
)
from app.crm.connectivity.providers import ADAPTERS
from app.crm.connectivity.reasons import REASON_RECLAIMED_STALE_CLAIM
from app.crm.connectivity.schemas.message import QueuedMessage, SendOutcome
from app.crm.shared.decode import jsonb_object
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
    """Accepted records the provider id and stamps sent."""
    plan = plan_for_outcome(
        SendOutcome(status="accepted", provider_message_id="wamid.X"), 1, 3
    )
    assert plan.status == STATUS_ACCEPTED
    assert plan.provider_message_id == "wamid.X"
    assert plan.mark_sent is True
    assert plan.reason is None


def test_retryable_failure_goes_back_on_the_queue() -> None:
    """Retryable failure goes back on the queue."""
    plan = plan_for_outcome(
        SendOutcome(status="failed", reason="rate_limited", retryable=True), 1, 3
    )
    assert plan.status == STATUS_QUEUED
    assert plan.reason == "rate_limited"
    assert plan.mark_sent is False


def test_retryable_failure_goes_dead_on_the_last_attempt() -> None:
    """Retryable failure goes dead on the last attempt."""
    plan = plan_for_outcome(
        SendOutcome(status="failed", reason="rate_limited", retryable=True), 3, 3
    )
    # 'dead' (we stopped trying), not 'failed' (the wire said no).
    assert plan.status == STATUS_DEAD
    assert plan.reason == REASON_ATTEMPTS_EXHAUSTED


def test_permanent_failure_never_retries_however_many_attempts_remain() -> None:
    """Permanent failure never retries however many attempts remain."""
    plan = plan_for_outcome(
        SendOutcome(status="failed", reason="template_not_approved"), 1, 99
    )
    assert plan.status == STATUS_FAILED
    assert plan.reason == "template_not_approved"


def test_failure_without_a_reason_still_records_one() -> None:
    """Failure without a reason still records one."""
    plan = plan_for_outcome(SendOutcome(status="failed"), 1, 3)
    assert plan.reason == REASON_PROVIDER_REJECTED


def test_a_send_that_never_reports_cannot_be_marked_sent() -> None:
    """A send that never reports cannot be marked sent."""
    for outcome in (
        SendOutcome(status="failed", reason="boom", retryable=True),
        SendOutcome(status="failed", reason="boom"),
    ):
        assert plan_for_outcome(outcome, 1, 3).mark_sent is False


def test_our_refusal_lands_as_blocked_terminal_and_unsent() -> None:
    """Our refusal lands as blocked, terminal and unsent.

    T16 col 12: 'failed' is the provider refusing, 'blocked' is us. A
    merchant with a paused number must not read the word reserved for
    "Meta said no" — and OUR decision does not change by retrying.
    """
    plan = plan_for_outcome(
        SendOutcome(status="blocked", reason=REASON_SUPPRESSED), 1, 3
    )
    assert plan.status == STATUS_BLOCKED
    assert plan.reason == REASON_SUPPRESSED
    assert plan.mark_sent is False
    assert plan.retry_after_seconds is None
    assert plan.provider_message_id is None


# --- the plumbing never raises -----------------------------------------------
# The policy (plan_for_outcome) is pure and pinned above; these pin the paths
# AROUND it — the never-raises contract that keeps a claimed batch alive.


def _message(attempt: int = 1) -> QueuedMessage:
    """A queued message for tests; keyword overrides replace any field."""
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


async def _gate_open(handles):
    """Test double: the suppression probe finds nothing."""
    return False


async def test_an_accepted_send_records_the_outcome(monkeypatch) -> None:
    """An accepted send records the outcome."""
    written = {}

    async def fake_send(send_token, message):
        """Test double: a send with a scripted outcome."""
        # The dispatcher mints a token for every send — the fake carries the
        # real signature so a signature drift fails here, not in production.
        written["sent"] = message.id
        written["token_names_message"] = send_token.message_id == message.id
        return SendOutcome(status="accepted", provider_message_id="wamid.T")

    async def record_outcome(
        message_id, status, reason, pmid, mark_sent, attempt, retry
    ):
        """Test double: records what the dispatcher tried to write."""
        written.update(status=status, mark_sent=mark_sent, attempt=attempt)
        return True

    monkeypatch.setattr(dispatch, "is_suppressed", _gate_open)
    monkeypatch.setattr(dispatch, "send", fake_send)
    monkeypatch.setattr(dispatch.message_accessor, "apply_outcome", record_outcome)
    await dispatch._dispatch_one(_message(), 3)
    assert written["sent"] == "m-1"
    assert written["token_names_message"] is True
    assert written["status"] == STATUS_ACCEPTED
    assert written["mark_sent"] is True
    # The write carries the claim's generation, so a stale claim's late
    # outcome cannot land on a row a newer claim now owns.
    assert written["attempt"] == 1


async def test_a_raising_send_becomes_a_retryable_send_error(monkeypatch) -> None:
    """A raising send becomes a retryable send error."""
    written = {}

    async def broken_send(send_token, message):
        """Test double: a send that raises."""
        raise RuntimeError("wire fell over")

    async def record_outcome(
        message_id, status, reason, pmid, mark_sent, attempt, retry
    ):
        """Test double: records what the dispatcher tried to write."""
        written.update(status=status, reason=reason, retry=retry)
        return True

    monkeypatch.setattr(dispatch, "is_suppressed", _gate_open)
    monkeypatch.setattr(dispatch, "send", broken_send)
    monkeypatch.setattr(dispatch.message_accessor, "apply_outcome", record_outcome)
    await dispatch._dispatch_one(_message(attempt=1), 3)
    # We don't know whether the provider saw it, so it requeues with a delay.
    assert written["status"] == STATUS_QUEUED
    assert written["reason"] == REASON_SEND_ERROR
    assert written["retry"] is not None and written["retry"] >= 1


async def test_a_suppressed_address_is_blocked_and_the_adapter_never_called(
    monkeypatch,
) -> None:
    """A suppressed address is blocked and the adapter never called.

    The one check a person who said STOP is protected by. The send spy
    RAISES, so reaching the adapter at all fails the test — blocked means
    nothing left the building.
    """
    written = {}

    async def suppressed(handles):
        """Test double: this handle carries a live suppression."""
        assert handles == {"phone": "+919812345678"}
        return True

    async def exploding_send(send_token, message):
        """Test double: the adapter must never be reached."""
        raise AssertionError("adapter reached past a refusing gate")

    async def record_outcome(
        message_id, status, reason, pmid, mark_sent, attempt, retry
    ):
        """Test double: records what the dispatcher tried to write."""
        written.update(status=status, reason=reason, mark_sent=mark_sent)
        return True

    monkeypatch.setattr(dispatch, "is_suppressed", suppressed)
    monkeypatch.setattr(dispatch, "send", exploding_send)
    monkeypatch.setattr(dispatch.message_accessor, "apply_outcome", record_outcome)
    await dispatch._dispatch_one(_message(), 3)
    assert written["status"] == STATUS_BLOCKED
    assert written["reason"] == REASON_SUPPRESSED
    assert written["mark_sent"] is False


async def test_a_channel_the_gate_cannot_check_fails_closed(monkeypatch) -> None:
    """A channel the gate cannot check fails closed.

    ADR 0018: unknown gate input means NO. A channel with no handle-kind
    mapping must block, not slip past the gate unchecked.
    """
    written = {}

    async def exploding_send(send_token, message):
        """Test double: the adapter must never be reached."""
        raise AssertionError("adapter reached past a refusing gate")

    async def record_outcome(
        message_id, status, reason, pmid, mark_sent, attempt, retry
    ):
        """Test double: records what the dispatcher tried to write."""
        written.update(status=status, reason=reason)
        return True

    monkeypatch.setattr(dispatch, "send", exploding_send)
    monkeypatch.setattr(dispatch.message_accessor, "apply_outcome", record_outcome)
    message = _message()
    message = message.model_copy(update={"channel": "carrier_pigeon"})
    await dispatch._dispatch_one(message, 3)
    assert written["status"] == STATUS_BLOCKED
    assert written["reason"] == REASON_GATE_UNAVAILABLE


async def test_a_hung_gate_probe_is_bounded_and_fails_closed(monkeypatch) -> None:
    """A hung gate probe is bounded and fails closed.

    The probe reads the same pool send() guards with its deadline; unbounded,
    a hung probe stalls the serial batch past the claim lease and reproduces
    the double send the lease inequality pins against. Bounded, it is OUR
    refusal — blocked/gate_unavailable — and nothing reaches the provider.
    """
    written = {}

    async def hanging_probe(handles):
        """Test double: the suppression probe never answers."""
        await asyncio.sleep(3600)

    async def exploding_send(send_token, message):
        """Test double: the adapter must never be reached."""
        raise AssertionError("adapter reached past a refusing gate")

    async def record_outcome(
        message_id, status, reason, pmid, mark_sent, attempt, retry
    ):
        """Test double: records what the dispatcher tried to write."""
        written.update(status=status, reason=reason, mark_sent=mark_sent)
        return True

    monkeypatch.setattr(dispatch, "is_suppressed", hanging_probe)
    monkeypatch.setattr(dispatch, "send", exploding_send)
    monkeypatch.setattr(dispatch, "CRM_MESSAGE_SEND_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(dispatch.message_accessor, "apply_outcome", record_outcome)
    await dispatch._dispatch_one(_message(), 3)
    assert written["status"] == STATUS_BLOCKED
    assert written["reason"] == REASON_GATE_UNAVAILABLE
    assert written["mark_sent"] is False


def test_every_adapter_channel_is_registered_in_channels() -> None:
    """Every adapter channel is registered in channels.py (ADAPTERS ⊆ CHANNELS).

    The fail-closed default above means an unregistered channel blocks every
    send — safe but useless. Registering an adapter without an entry in the
    channel registry fails here, in CI. The pin lives in the tests because
    channels.py itself may not import providers/ (rule 11 confines them
    behind the send door — the reason the registry is a separate file).
    """
    assert set(ADAPTERS) <= set(CHANNELS)
    # Every entry carries a probe-able handle kind, not just a key.
    for channel in ADAPTERS:
        assert gate_handle_kind_for(channel)


async def test_a_reclaimed_row_discards_the_late_outcome(monkeypatch) -> None:
    """A reclaimed row discards the late outcome."""
    calls = []

    async def fake_send(send_token, message):
        """Test double: a send with a scripted outcome."""
        return SendOutcome(status="accepted", provider_message_id="wamid.L")

    async def reclaimed(message_id, status, reason, pmid, mark_sent, attempt, retry):
        """Test double: the row already belongs to another worker."""
        calls.append(status)
        return False  # the sweep already handed this row to another worker

    monkeypatch.setattr(dispatch, "is_suppressed", _gate_open)
    monkeypatch.setattr(dispatch, "send", fake_send)
    monkeypatch.setattr(dispatch.message_accessor, "apply_outcome", reclaimed)
    await dispatch._dispatch_one(_message(), 3)
    # Discarded means one write attempt, no retry, no raise — their row now.
    assert calls == [STATUS_ACCEPTED]


def test_jitter_stays_in_bounds_and_never_reaches_zero() -> None:
    """Jitter stays in bounds and never reaches zero."""
    assert _jittered(None) is None
    for _ in range(500):
        spread = _jittered(30)
        assert spread is not None and 24 <= spread <= 36  # +-20% of 30
        # An instant retry is the one thing jitter must never produce.
        floor = _jittered(1)
        assert floor is not None and floor >= 1


# --- the SQL builders -------------------------------------------------------


def test_claim_is_race_safe_across_pods() -> None:
    """Claim is race safe across pods."""
    query, values = claim_queued_messages_query(25)
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "status = 'queued'" in query
    assert "LIMIT $1" in query
    assert values == [25]


# --- log lines stay bounded --------------------------------------------------


def test_sample_ids_lists_everything_when_it_fits() -> None:
    """Sample ids lists everything when it fits."""
    assert sample_ids(["a", "b"], limit=10) == "a, b"
    assert sample_ids([], limit=10) == ""


def test_sample_ids_truncates_and_says_how_many_it_hid() -> None:
    """Sample ids truncates and says how many it hid."""
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
    """Dispatcher is a registered worker role."""
    # start_worker_role raises on an unregistered role, so without this entry
    # a CRM_ROLE=dispatcher pod crashes at boot.
    from app.crm.worker_main import ROLES

    assert "dispatcher" in ROLES


def test_contracts_export_the_scaffold_pair() -> None:
    """Contracts export the scaffold pair."""
    # worker_main may import only the module's contracts — the claim/handle
    # pair must be reachable there, not by deep import.
    from app.crm.connectivity import contracts

    assert callable(contracts.claim_sends)
    assert callable(contracts.dispatch_send)


# --- the dials read from the environment -------------------------------------


def test_a_bad_dial_falls_back_instead_of_stopping_the_pod(monkeypatch) -> None:
    """A bad dial falls back instead of stopping the pod."""
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
    """A bad float dial falls back too."""
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
    """Retry waits longer each attempt then caps."""
    # Retrying a rate-limited send instantly is the one response guaranteed
    # to make it fail again.
    assert backoff_seconds(1, 30, RETRY_MAX_SECONDS) == 30
    assert backoff_seconds(2, 30, RETRY_MAX_SECONDS) == 60
    assert backoff_seconds(3, 30, RETRY_MAX_SECONDS) == 120
    # The ceiling only binds if MAX_ATTEMPTS is raised well past its default.
    assert backoff_seconds(9, 30, RETRY_MAX_SECONDS) == RETRY_MAX_SECONDS


def test_requeue_carries_a_delay_and_terminal_outcomes_do_not() -> None:
    """Requeue carries a delay and terminal outcomes do not."""
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
    """Claim returns when rows came due for the lag metric."""
    # The lag log line reads next_attempt_at off the claimed row; dropping it
    # from CLAIMED_COLUMNS would fail the decoder on every claimed batch.
    assert "next_attempt_at" in CLAIMED_COLUMNS


def test_queue_filters_and_orders_by_when_a_row_is_due() -> None:
    """Queue filters and orders by when a row is due."""
    # Ordering by created_at would send a requeued row to the FRONT on its
    # original timestamp, retrying it ahead of every fresh message.
    query, _ = claim_queued_messages_query(25)
    assert "next_attempt_at <= now()" in query
    assert "ORDER BY next_attempt_at" in query
    assert "ORDER BY created_at" not in query


def test_only_a_requeue_moves_the_next_attempt_time() -> None:
    """Only a requeue moves the next attempt time."""
    query, values = apply_outcome_query(
        "m-1", "queued", "rate_limited", None, False, 1, 30
    )
    assert "make_interval(secs => $6::int)" in query
    assert values[5] == 30


def test_claim_burns_an_attempt_so_a_dead_worker_cannot_loop_forever() -> None:
    """Claim burns an attempt so a dead worker cannot loop forever."""
    query, _ = claim_queued_messages_query(10)
    assert "attempt = attempt + 1" in query


def test_outcome_write_is_guarded_by_the_claim() -> None:
    """Outcome write is guarded by the claim."""
    # Two guards, one question — "is this still MY claim?": the status stops
    # a write after a terminal outcome landed, and the attempt stops one
    # after the sweep reassigned the row to a NEWER claim (which put it back
    # in 'sending', so status alone would let the stale write through).
    query, values = apply_outcome_query(
        "m-1", "accepted", None, "wamid.X", True, 2, None
    )
    assert "status = 'sending'" in query
    assert "attempt = $7::int" in query
    assert values == ["m-1", "accepted", None, "wamid.X", True, None, 2]


def test_outcome_write_never_erases_a_known_provider_id() -> None:
    """Outcome write never erases a known provider id."""
    query, _ = apply_outcome_query("m-1", "queued", "rate_limited", None, False, 1, 30)
    assert "COALESCE($4, provider_message_id)" in query


def test_stale_sweep_targets_only_expired_claims() -> None:
    """Stale sweep targets only expired claims."""
    query, values = requeue_stale_claims_query(5, 3)
    assert "status = 'sending'" in query
    assert "make_interval(mins => $1::int)" in query
    # The reclaim reason the sweep writes is manifest vocabulary, so the
    # word must be the one reasons.py declares — not a SQL-only spelling.
    assert f"'{REASON_RECLAIMED_STALE_CLAIM}'" in query
    assert values == [5, 3]


def test_stale_sweep_kills_rows_that_are_out_of_attempts() -> None:
    """Stale sweep kills rows that are out of attempts."""
    # The loop this closes: a row whose outcome can never be RECORDED (e.g. a
    # duplicate provider_message_id makes apply_outcome raise every time) is
    # claimed, really sent, left in 'sending', reclaimed — and really sent
    # again on every lap. Each lap's claim spends an attempt, so the sweep
    # applying the same ceiling as the retry ladder bounds it at max_attempts
    # copies instead of one per stale window, forever.
    query, values = requeue_stale_claims_query(5, 3)
    assert "attempt >= $2::int" in query
    assert "'dead'" in query
    # Same word as plan_for_outcome's dead: we stopped, the provider didn't.
    assert f"'{REASON_ATTEMPTS_EXHAUSTED}'" in query
    assert "RETURNING id, status" in query
    assert values == [5, 3]


def test_builders_parameterize_every_value() -> None:
    """Builders parameterize every value."""
    # A mismatch is an unbound $n or, worse, a value that got interpolated.
    for query, values in (
        claim_queued_messages_query(25),
        requeue_stale_claims_query(5, 3),
        apply_outcome_query("m-1", "failed", "nope", None, False, 1, None),
    ):
        placeholders = set(re.findall(r"\$(\d+)", query))
        assert placeholders == {str(n) for n in range(1, len(values) + 1)}


# --- the decoder can never stop the queue -----------------------------------


def test_jsonb_object_is_total() -> None:
    """Jsonb object is total."""
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
        assert jsonb_object(value) == expected, value


def test_jsonb_object_refuses_to_invent_keys() -> None:
    """Jsonb object refuses to invent keys."""
    # dict([["a", 1]]) == {"a": 1} would post a variable nobody wrote.
    assert jsonb_object([["a", 1]]) == {}
    assert jsonb_object([("a", 1)]) == {}


def test_decoded_variables_do_not_alias_the_row() -> None:
    """Decoded variables do not alias the row."""
    row = {"a": 1}
    decoded = jsonb_object(row)
    decoded["b"] = 2
    assert row == {"a": 1}


# --- the table --------------------------------------------------------------


def test_manifest_is_owned_by_connectivity() -> None:
    """Manifest is owned by connectivity."""
    assert TABLE_OWNERS["crm_message"] == "connectivity"


def test_dedupe_key_is_mandatory_and_unique_per_merchant() -> None:
    """Dedupe key is mandatory and unique per merchant."""
    # The only thing between a crash-retry and a duplicate send, so it covers
    # every row: NOT NULL with no default, and a total (not partial) unique.
    sql = _ddl()
    assert "dedupe_key          text NOT NULL" in sql
    assert "crm_message (merchant_id, dedupe_key);" in sql
    assert "WHERE dedupe_key IS NOT NULL" not in sql


def test_expected_columns_are_all_present() -> None:
    """Expected columns are all present."""
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
    """Decision id matches the diary pages."""
    # The permission decision table keys on a bigserial; a uuid would not join.
    sql = _ddl()
    assert "decision_id         bigint" in sql


def test_variables_shape_is_enforced_in_code_not_in_the_table() -> None:
    """Variables shape is enforced in code not in the table."""
    # Any JSON value is a legal row; jsonb_object neutralises the rest.
    sql = _ddl()
    assert "variables           jsonb NOT NULL" in sql
    assert "jsonb_typeof" not in sql


def test_purpose_key_cannot_be_null() -> None:
    """Purpose key cannot be null."""
    # A mandatory gate input, and missing inputs must fail closed.
    sql = _ddl()
    assert "purpose_key         text NOT NULL" in sql


def test_proposal_fields_are_immutable_by_trigger() -> None:
    """Proposal fields are immutable by trigger."""
    sql = _ddl()
    assert "crm_message_immutable_guard" in sql
    for frozen in ("sent_to_address", "purpose_key", "variables", "dedupe_key"):
        assert f"NEW.{frozen}" in sql, frozen
    # The envelope must stay writable, or the dispatcher cannot work.
    for mutable in ("status", "claimed_at", "next_attempt_at", "provider_message_id"):
        assert f"NEW.{mutable} " not in sql, mutable


def test_binding_id_is_set_once_never_rewritten() -> None:
    """Binding id is set once never rewritten.

    NULL on refused rows, stamped when a route is picked — so it may be SET
    once but never changed: a rewrite would forge which endpoint a message
    left on, while full immutability would forbid the stamp itself. Lives in
    060 as a CREATE OR REPLACE of 056's function, because merged migrations
    are never edited.
    """
    sql = Path(
        "app/database/migrations/060_create_crm_connector_tables.sql"
    ).read_text()
    assert "CREATE OR REPLACE FUNCTION crm_message_immutable()" in sql
    assert "OLD.binding_id IS NOT NULL" in sql
    assert "NEW.binding_id IS DISTINCT FROM OLD.binding_id" in sql
    # And 056 itself carries no set-once clause — it must stay as merged.
    assert "OLD.binding_id" not in _ddl()


def test_the_work_queue_is_a_partial_index() -> None:
    """The work queue is a partial index."""
    sql = _ddl()
    assert "crm_message_queued_ix" in sql
    assert "WHERE status = 'queued'" in sql
    assert "crm_message_claimed_ix" in sql


def test_index_set_is_exactly_what_is_read() -> None:
    """Index set is exactly what is read."""
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
    """One provider message is one row."""
    # A write rule, not a read index: recording the same provider id twice
    # must be impossible. Not merchant-scoped on purpose — a provider id is
    # globally unique, so scoping it per tenant would let two merchants each
    # record the same real message.
    sql = _ddl()
    assert "crm_message (provider_message_id)" in sql
    assert "(merchant_id, provider_message_id)" not in sql


def test_analytics_indexes_are_merchant_first_and_time_ordered() -> None:
    """Analytics indexes are merchant first and time ordered."""
    # merchant-first makes a tenant's slice a range scan, not a filter over
    # everyone; created_at DESC matches how every report reads.
    sql = _ddl()
    assert "crm_message (merchant_id, created_at DESC)" in sql
    assert "crm_message (merchant_id, customer_id, created_at DESC)" in sql


def test_no_touch_trigger_because_there_is_no_updated_at() -> None:
    """No touch trigger because there is no updated at."""
    # Every state stamps its own clock; same choice as crm_event_raw (051).
    sql = _ddl()
    assert "updated_at" not in sql
    assert "crm_touch_updated_at" not in sql
    assert "crm_message_immutable_guard" in sql


def test_status_is_a_closed_enum_but_channel_is_not() -> None:
    """Status is a closed enum but channel is not."""
    # Format CHECKs are required; vocabulary CHECKs are the 027 scar.
    sql = _ddl()
    assert "CHECK (status IN (" in sql
    for state in ("queued", "sending", "blocked", "accepted", "sent", "dead"):
        assert f"'{state}'" in sql, state
    # Vocabulary in a CHECK is the migration-027 scar.
    assert "CHECK (channel" not in sql
    assert "CHECK (source_kind" not in sql


def test_customer_fk_is_composite_so_a_message_cannot_cross_tenants() -> None:
    """Customer fk is composite so a message cannot cross tenants."""
    sql = _ddl()
    assert "FOREIGN KEY (merchant_id, customer_id)" in sql
    assert "REFERENCES crm_customer (merchant_id, id)" in sql
