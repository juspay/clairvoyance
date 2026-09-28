"""Phase 2 of docs/CALL_OUTCOMES.md: the call outcome columns, exposed.

Covers the DB-free pieces: the analytics filters, funnel and breakdown
queries and their handlers; the call detail and CSV columns; the merchant
webhook keys on every builder (off: today's payload byte for byte); the CRM
letters (yielding facts, call.outcome_evaluated); the opt-in second webhook;
the span attributes; the journey card's outcome layers.
"""

import importlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from uuid import uuid4

import pytest

# Import the dispatch package first: managers.calls reaches into
# dispatch.alerts, whose package __init__ imports dispatch.worker, which
# imports managers.calls straight back.
from app.ai.voice.agents.breeze_buddy import (  # noqa: F401
    crm_mirror as cm,
    dispatch as _dispatch,
)
from app.ai.voice.agents.breeze_buddy.callbacks import outcome_evaluated as oe
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.observability import tracing_setup
from app.ai.voice.agents.breeze_buddy.services.call_limiter import CallLimitVerdict
from app.ai.voice.agents.breeze_buddy.utils import call_outcome_webhook as cow
from app.api.routers.breeze_buddy.analytics import (
    _ANALYTICS_HANDLERS,
    handlers as analytics_handlers,
)
from app.crm.record import timeline
from app.crm.record.schemas import JourneyCard
from app.database.accessor.breeze_buddy import lead_call_tracker as lct_accessor
from app.database.queries.breeze_buddy import lead_call_tracker as lead_q
from app.database.queries.breeze_buddy.analytics import analytics as aq
from app.schemas import ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.analytics import AnalyticsType
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.schemas.breeze_buddy.outcomes import (
    CallOutcome,
    ConnectionStatus,
    EndReason,
    OutcomeSource,
)

# The package re-exports the service_callback FUNCTION under the module's
# own name, so the module is taken from the import system.
sc_mod = importlib.import_module(
    "app.ai.voice.agents.breeze_buddy.callbacks.service_callback"
)

WEBHOOK = "https://merchant.example/hook"
OUTCOME_KEYS = {
    "event",
    "connectionStatus",
    "connectionReason",
    "endReason",
    "agentOutcome",
    "outcomeSource",
    "evalOutcome",
}


def make_lead(**overrides: Any) -> LeadCallTracker:
    values: Dict[str, Any] = dict(
        id="lead-1",
        reseller_id="breeze",
        template="order-confirmation",
        template_id="tmpl-1",
        merchant_id="shop",
        request_id="req-1",
        payload={
            "customer_mobile_number": "+919999999999",
            "reporting_webhook_url": WEBHOOK,
        },
        metaData={},
        status=LeadCallStatus.FINISHED,
        outcome="CONFIRMED",
        call_id="CA-1",
        call_initiated_time=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
        call_end_time=datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc),
        execution_mode=ExecutionMode.TELEPHONY,
        customer_id="cust-1",
        enrollment_id="enr-1",
    )
    values.update(overrides)
    return LeadCallTracker(**values)


def switch(monkeypatch, on: bool) -> None:
    async def _flag() -> bool:
        return on

    monkeypatch.setattr(cow, "WEBHOOK_CALL_OUTCOME_KEYS", _flag)


@pytest.fixture
def sent(monkeypatch) -> List[Dict[str, Any]]:
    """Every merchant webhook the builders send, with its log labels."""
    captured: List[Dict[str, Any]] = []

    async def _send(session, url, data, max_retries=3, **labels):
        captured.append({"url": url, "data": data, **labels})
        return True

    for module in (calls_mod, sc_mod, oe):
        monkeypatch.setattr(module, "send_webhook_with_retry", _send)

    @asynccontextmanager
    async def _session():
        yield object()

    for module in (calls_mod, oe):
        monkeypatch.setattr(module, "create_aiohttp_session", _session)
    return captured


@pytest.fixture
def spawned(monkeypatch) -> List[Any]:
    """Background taps, collected so a test can await them in order."""
    coros: List[Any] = []

    def _spawn(coro, name=None):
        coros.append(coro)

    monkeypatch.setattr(cm, "spawn_background_task", _spawn)
    monkeypatch.setattr(oe, "spawn_background_task", _spawn)
    return coros


async def drain(coros: List[Any]) -> None:
    while coros:
        await coros.pop(0)


# ---------------------------------------------------------------------------
# Analytics: filters, funnel and breakdowns
# ---------------------------------------------------------------------------


def test_filters_match_the_upper_cased_columns():
    conditions, values = aq.build_analytics_where_clause(
        {"connection_status": ["answered"], "agent_outcome": [" confirmed ", "Busy"]}
    )
    assert "lct.connection_status = ANY($1)" in conditions
    assert "lct.agent_outcome = ANY($2)" in conditions
    assert values == [["ANSWERED"], ["CONFIRMED", "BUSY"]]


def test_no_new_filter_no_new_condition():
    conditions, values = aq.build_analytics_where_clause({})
    assert not any("connection_status" in c or "agent_outcome" in c for c in conditions)
    assert values == []


@pytest.mark.parametrize(
    "builder",
    [
        aq.get_connection_funnel_query,
        aq.get_connection_breakdown_query,
        aq.get_agent_outcome_breakdown_query,
        aq.get_eval_agreement_query,
    ],
)
def test_outcome_queries_scope_finished_attempts_by_created_at(builder):
    """A NOT_DIALED attempt has no call_initiated_time, so the window is on
    created_at or the funnel loses the attempts it exists to count."""
    date_from = datetime(2026, 9, 1)
    sql, values = builder({"date_from": date_from, "merchant_id": ["shop"]})
    assert "lct.status = 'FINISHED'" in sql
    assert "lct.created_at >=" in sql
    assert "lct.call_initiated_time >=" not in sql
    assert "telephony_numbers" not in sql
    assert len(values) == sql.count("$")


def test_provider_filter_brings_its_join():
    sql, _ = aq.get_connection_breakdown_query({"provider": ["plivo"]})
    assert 'LEFT JOIN "telephony_numbers" ou' in sql


def test_funnel_stages():
    sql, _ = aq.get_connection_funnel_query({})
    for stage in (
        "AS total",
        "AS unclassified",
        "AS dialled",
        "AS answered",
        "AS reached_human",
        "AS outcome_decided",
    ):
        assert stage in sql
    assert "NOT IN ('NOT_DIALED', 'REJECTED')" in sql
    assert "<> 'VOICEMAIL'" in sql


def test_agent_and_eval_reads_are_over_answered_attempts():
    for builder in (aq.get_agent_outcome_breakdown_query, aq.get_eval_agreement_query):
        sql, _ = builder({})
        assert "lct.connection_status = 'ANSWERED'" in sql


def test_eval_agreement_buckets():
    sql, _ = aq.get_eval_agreement_query({})
    for bucket in (
        "AS not_evaluated",
        "AS pending",
        "AS failed",
        "AS skipped",
        "AS filled",
        "AS confirmed",
        "AS flagged",
        "AS no_outcome",
    ):
        assert bucket in sql


@pytest.mark.parametrize(
    "kind, accessor, result",
    [
        (
            AnalyticsType.CONNECTION_FUNNEL,
            "get_connection_funnel_from_db",
            {"total": 3, "answered": 2},
        ),
        (
            AnalyticsType.CONNECTION_BREAKDOWN,
            "get_connection_breakdown_from_db",
            [{"connection_status": "ANSWERED", "count": 2}],
        ),
        (
            AnalyticsType.AGENT_OUTCOME_BREAKDOWN,
            "get_agent_outcome_breakdown_from_db",
            [{"agent_outcome": "CONFIRMED", "count": 1}],
        ),
        (
            AnalyticsType.EVAL_AGREEMENT,
            "get_eval_agreement_from_db",
            {"answered": 2, "confirmed": 1},
        ),
    ],
)
async def test_outcome_analytics_handlers(monkeypatch, kind, accessor, result):
    seen: List[Dict[str, Any]] = []

    async def _read(filters):
        seen.append(filters)
        return result

    monkeypatch.setattr(analytics_handlers, accessor, _read)
    filters = {"merchant_id": ["shop"]}
    handler = _ANALYTICS_HANDLERS[kind]
    got = await handler(filters, {}, SimpleNamespace())
    assert got == {"type": kind.value, "filters_applied": filters, "results": result}
    assert seen == [filters]


# ---------------------------------------------------------------------------
# Call detail and CSV
# ---------------------------------------------------------------------------


def test_call_detail_carries_the_outcome_columns():
    tracker = {
        "id": "lead-1",
        "template": "t",
        "reseller_id": "r",
        "payload": None,
        "meta_data": None,
        "status": "FINISHED",
        "outcome": "BUSY",
        "created_at": datetime(2026, 9, 24),
        "connection_status": "ANSWERED",
        "end_reason": "AGENT_ENDED",
        "agent_outcome": "BUSY",
        "outcome_source": "LLM",
        "eval_status": "PENDING",
    }
    detail = analytics_handlers._build_call_detail_result(tracker)
    assert detail.outcome == "BUSY"
    assert detail.connection_status == "ANSWERED"
    assert detail.end_reason == "AGENT_ENDED"
    assert detail.agent_outcome == "BUSY"
    assert detail.outcome_source == "LLM"
    assert detail.eval_status == "PENDING"
    assert detail.eval_outcome is None
    assert detail.connection_reason is None


def test_csv_query_selects_every_call_detail_column():
    """The list and grouped reads select lct.*; the CSV read names columns."""
    sql, _ = aq.get_call_details_records_query({})
    for column in analytics_handlers.CALL_DETAIL_OUTCOME_COLUMNS:
        assert f"lct.{column}" in sql


def test_csv_outcome_columns_come_after_every_existing_column():
    labels = list(analytics_handlers.EXPORT_OUTCOME_COLUMNS)
    assert analytics_handlers.EXPORT_COLUMNS[-len(labels) :] == labels
    assert analytics_handlers.EXPORT_COLUMNS[: -len(labels)] == [
        "Lead ID",
        "Call ID",
        "Template",
        "Name",
        "Mobile Number",
        "Start Time",
        "End Time",
        "Duration",
        "Outcome",
        "Metadata Outcome",
        "Call Ended By",
        "Recording URL",
        "Attempt Count",
        "Record",
    ]


# ---------------------------------------------------------------------------
# Merchant webhook keys
# ---------------------------------------------------------------------------


async def test_keys_off_returns_the_payload_itself(monkeypatch):
    switch(monkeypatch, False)
    data = {"outcome": "BUSY"}
    assert await cow.with_call_outcome_keys(data, CallOutcome()) is data


async def test_an_unreadable_switch_is_off(monkeypatch):
    async def _boom() -> bool:
        raise RuntimeError("redis down")

    monkeypatch.setattr(cow, "WEBHOOK_CALL_OUTCOME_KEYS", _boom)
    data = {"outcome": "BUSY"}
    assert await cow.with_call_outcome_keys(data, CallOutcome()) is data


async def test_keys_on_are_additive_and_never_replace(monkeypatch):
    switch(monkeypatch, True)
    call_outcome = CallOutcome(
        connection_status=ConnectionStatus.ANSWERED,
        end_reason=EndReason.AGENT_ENDED,
        agent_outcome="CONFIRMED",
        outcome_source=OutcomeSource.LLM,
    )
    got = await cow.with_call_outcome_keys(
        {"outcome": "CONFIRMED", "event": "merchant-owned"}, call_outcome
    )
    assert got == {
        "event": "merchant-owned",
        "connectionStatus": "ANSWERED",
        "connectionReason": None,
        "endReason": "AGENT_ENDED",
        "agentOutcome": "CONFIRMED",
        "outcomeSource": "LLM",
        "evalOutcome": {"status": None, "value": None},
        "outcome": "CONFIRMED",
    }


async def test_keys_on_without_an_outcome_are_all_null(monkeypatch):
    switch(monkeypatch, True)
    got = await cow.with_call_outcome_keys({}, None)
    assert set(got) == OUTCOME_KEYS
    assert got["event"] == cow.EVENT_CALL_COMPLETED
    assert got["connectionStatus"] is None


def _service_context(lead: LeadCallTracker, schema=None) -> Any:
    return SimpleNamespace(
        lead=lead,
        call_sid=lead.call_id,
        expected_callback_response_schema=schema,
        aiohttp_session=object(),
    )


@pytest.mark.parametrize("on", [False, True])
async def test_service_callback(monkeypatch, sent, on):
    switch(monkeypatch, on)
    lead = make_lead(
        status=LeadCallStatus.PROCESSING,
        agent_outcome="CONFIRMED",
        outcome_source="LLM",
        metaData={"outcome": {"agentOutcome": "declared-by-merchant"}},
    )
    schema = {"agentOutcome": {"optional": True}}
    await sc_mod.service_callback(_service_context(lead, schema), {})

    [call] = sent
    data = call["data"]
    assert call["webhook"] == "service_callback"
    assert call["merchant_id"] == "shop"
    legacy = {
        "callSid",
        "outcome",
        "attemptCount",
        "transcription",
        "callDuration",
        "orderId",
    }
    if not on:
        assert set(data) == legacy | {"agentOutcome"}
        assert data["agentOutcome"] == "declared-by-merchant"
        return
    assert set(data) == legacy | OUTCOME_KEYS
    # Inside a live conversation: answered, ending not known yet.
    assert data["connectionStatus"] == "ANSWERED"
    assert data["endReason"] is None
    # A field the template declares keeps today's value.
    assert data["agentOutcome"] == "declared-by-merchant"
    assert data["outcomeSource"] == "LLM"


@pytest.mark.parametrize("on", [False, True])
async def test_precheck_failed_webhook(monkeypatch, sent, on):
    switch(monkeypatch, on)

    async def _run_pre_checks(**_):
        return SimpleNamespace(
            should_proceed=False,
            failure_action=None,
            results=[SimpleNamespace(name="stock", passed=False, reason="gone")],
            summary=lambda: "stock: gone",
        )

    async def _write(**_):
        return make_lead()

    monkeypatch.setattr(calls_mod, "run_pre_checks", _run_pre_checks)
    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", _write)
    config: Any = SimpleNamespace(pre_checks=[object()])
    await calls_mod._run_pre_checks_for_lead(config, make_lead(), None, object())

    [call] = sent
    assert call["webhook"] == "precheck_failed"
    legacy = {
        "outcome": "PRECHECK_FAILED",
        "attemptCount": 1,
        "failureReason": "stock: gone",
        "orderId": "req-1",
    }
    if not on:
        assert call["data"] == legacy
        return
    assert call["data"] == {
        **legacy,
        "event": "call.completed",
        "connectionStatus": "NOT_DIALED",
        "connectionReason": "PRECHECK_FAILED",
        "endReason": None,
        "agentOutcome": None,
        "outcomeSource": None,
        "evalOutcome": {"status": None, "value": None},
    }


@pytest.mark.parametrize("on", [False, True])
async def test_call_limit_webhook(monkeypatch, sent, on):
    switch(monkeypatch, on)

    async def _write(**_):
        return make_lead()

    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", _write)
    verdict = CallLimitVerdict(allowed=False, count=3, rule=None)
    assert await calls_mod.finish_lead_call_limit_reached(
        make_lead(), verdict, object()
    )

    [call] = sent
    assert call["webhook"] == "call_limit"
    assert call["data"]["outcome"] == "CALL_LIMIT_REACHED"
    if not on:
        assert set(call["data"]) == {
            "outcome",
            "attemptCount",
            "failureReason",
            "orderId",
        }
        return
    assert call["data"]["connectionStatus"] == "NOT_DIALED"
    assert call["data"]["connectionReason"] == "CALL_LIMIT"


@pytest.mark.parametrize("on", [False, True])
async def test_no_answer_webhook(monkeypatch, sent, on):
    switch(monkeypatch, on)
    config: Any = SimpleNamespace(max_retry=1, retry_offset=60)
    unanswered = CallOutcome(
        connection_status=ConnectionStatus.BUSY, provider_status="busy"
    )
    await calls_mod._retry_call(make_lead(), config, "NO_ANSWER", unanswered)

    [call] = sent
    assert call["webhook"] == "no_answer"
    legacy = {"callSid", "outcome", "attemptCount", "callDuration", "orderId"}
    assert call["data"]["outcome"] == "NO_ANSWER"
    if not on:
        assert set(call["data"]) == legacy
        return
    assert set(call["data"]) == legacy | OUTCOME_KEYS
    assert call["data"]["connectionStatus"] == "BUSY"
    assert call["data"]["agentOutcome"] is None


# ---------------------------------------------------------------------------
# CRM letters
# ---------------------------------------------------------------------------


@pytest.fixture
def letters(monkeypatch) -> List[Dict[str, Any]]:
    captured: List[Dict[str, Any]] = []

    async def _record_event(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(cm, "record_event", _record_event)
    return captured


async def test_yielding_facts_join_the_letter(letters):
    await cm.mirror_to_crm(
        "call.completed",
        merchant_id="shop",
        external_id="CA-1",
        outcome="CONFIRMED",
        yielding={"connection_status": "ANSWERED", "agent_outcome": None},
    )
    [letter] = letters
    assert letter["payload"]["connection_status"] == "ANSWERED"
    assert "agent_outcome" not in letter["payload"]


async def test_a_declared_field_wins_over_a_yielding_one(letters):
    await cm.mirror_to_crm(
        "call.completed",
        merchant_id="shop",
        external_id="CA-1",
        declared={"agent_outcome": "merchant's own"},
        yielding={"agent_outcome": "CONFIRMED"},
    )
    assert letters[0]["payload"]["agent_outcome"] == "merchant's own"


async def test_a_yielding_fact_never_replaces_our_own(letters):
    await cm.mirror_to_crm(
        "call.completed",
        merchant_id="shop",
        external_id="CA-1",
        lead_id="lead-1",
        yielding={"lead_id": "other"},
    )
    assert letters[0]["payload"]["lead_id"] == "lead-1"


async def test_finished_tap_carries_the_outcome_columns(monkeypatch, letters, spawned):
    async def _no_facts(lead):
        return {}

    monkeypatch.setattr(cm, "call_facts", _no_facts)
    cm._finished_lead_tap(
        make_lead(connection_status="ANSWERED", agent_outcome="CONFIRMED")
    )
    await drain(spawned)
    [letter] = letters
    assert letter["topic"] == "call.completed"
    assert letter["payload"]["outcome"] == "CONFIRMED"
    assert letter["payload"]["connection_status"] == "ANSWERED"
    assert letter["payload"]["agent_outcome"] == "CONFIRMED"


async def test_evaluated_letter(letters, spawned):
    lead = make_lead(
        connection_status="ANSWERED",
        agent_outcome="BUSY",
        eval_outcome="CONFIRMED",
        eval_status="DONE",
        eval_result_id="res-9",
    )
    cm._evaluated_lead_tap(lead, False)
    await drain(spawned)
    [letter] = letters
    assert letter["topic"] == "call.outcome_evaluated"
    assert letter["source"] == cm.SOURCE_TELEPHONY
    # A re-evaluation is a new result, so a new letter.
    assert letter["external_id"] == "call.outcome_evaluated:res-9"
    assert letter["customer_id"] == "cust-1"
    assert letter["payload"] == {
        "lead_id": "lead-1",
        "customer_mobile_number": "+919999999999",
        "call_id": "CA-1",
        "enrollment_id": "enr-1",
        "eval_outcome": "CONFIRMED",
        "eval_status": "DONE",
        "agent_outcome": "BUSY",
        "connection_status": "ANSWERED",
    }


async def test_evaluated_letter_skips_test_traffic(letters, spawned):
    cm._evaluated_lead_tap(make_lead(execution_mode=ExecutionMode.TELEPHONY_TEST))
    await drain(spawned)
    assert letters == []


def test_evaluated_topic_is_mirror_only():
    assert cm.MIRRORS["call.outcome_evaluated"] == cm.SOURCE_TELEPHONY


def test_evaluated_hooks_are_installed():
    assert cm._evaluated_lead_tap in lct_accessor._evaluated_hooks
    assert oe._outcome_evaluated_webhook_tap in lct_accessor._evaluated_hooks


def test_announce_is_fail_open(monkeypatch):
    calls: List[Any] = []

    def _boom(lead, notify):
        raise RuntimeError("tap broke")

    def _ok(lead, notify):
        calls.append(notify)

    monkeypatch.setattr(lct_accessor, "_evaluated_hooks", [_boom, _ok])
    lct_accessor.announce_call_evaluated(make_lead(), notify_webhook=True)
    assert calls == [True]


# ---------------------------------------------------------------------------
# The opt-in second webhook
# ---------------------------------------------------------------------------


def _evaluated_lead() -> LeadCallTracker:
    return make_lead(
        connection_status="ANSWERED",
        end_reason="AGENT_ENDED",
        agent_outcome="BUSY",
        outcome_source="LLM",
        eval_outcome="CONFIRMED",
        eval_status="DONE",
        eval_result_id="res-9",
    )


async def test_second_webhook_only_when_the_template_opted_in(sent, spawned):
    oe._outcome_evaluated_webhook_tap(_evaluated_lead(), False)
    await drain(spawned)
    assert sent == []


async def test_second_webhook_needs_a_reporting_url(sent, spawned):
    oe._outcome_evaluated_webhook_tap(make_lead(payload={}), True)
    await drain(spawned)
    assert sent == []


async def test_second_webhook_payload(monkeypatch, sent, spawned):
    # Opt-in is the template's; the global keys switch does not gate it.
    switch(monkeypatch, False)
    oe._outcome_evaluated_webhook_tap(_evaluated_lead(), True)
    await drain(spawned)
    [call] = sent
    assert call["url"] == WEBHOOK
    assert call["webhook"] == "call_outcome_evaluated"
    assert call["merchant_id"] == "shop"
    assert call["data"] == {
        "callSid": "CA-1",
        "outcome": "CONFIRMED",
        "attemptCount": 1,
        "callDuration": 120.0,
        "orderId": "req-1",
        "event": "call.outcome_evaluated",
        "connectionStatus": "ANSWERED",
        "connectionReason": None,
        "endReason": "AGENT_ENDED",
        "agentOutcome": "BUSY",
        "outcomeSource": "LLM",
        "evalOutcome": {"status": "DONE", "value": "CONFIRMED"},
    }


# ---------------------------------------------------------------------------
# Span attributes
# ---------------------------------------------------------------------------


class _Span:
    def __init__(self) -> None:
        self.attributes: Dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def _span_context(lead: LeadCallTracker) -> Any:
    return SimpleNamespace(
        root_span=_Span(),
        lead=lead,
        call_sid=lead.call_id,
        template_name="t",
        template_id="tmpl-1",
        bot=None,
        expected_callback_response_schema=None,
    )


def test_span_prefers_the_stored_columns(monkeypatch):
    monkeypatch.setattr(tracing_setup, "ENABLE_BREEZE_BUDDY_TRACING", True)
    context = _span_context(
        make_lead(connection_status="ANSWERED", agent_outcome="CONFIRMED")
    )
    tracing_setup.update_span_with_evaluation_data(
        context, CallOutcome(agent_outcome="OTHER")
    )
    attributes = context.root_span.attributes
    assert attributes["call_outcome"] == "CONFIRMED"
    assert attributes["connection_status"] == "ANSWERED"
    assert attributes["agent_outcome"] == "CONFIRMED"
    assert "end_reason" not in attributes


def test_span_falls_back_to_the_in_memory_outcome(monkeypatch):
    """Writes switched off: the lead has no columns, the span still does."""
    monkeypatch.setattr(tracing_setup, "ENABLE_BREEZE_BUDDY_TRACING", True)
    context = _span_context(make_lead())
    tracing_setup.update_span_with_evaluation_data(
        context,
        CallOutcome(end_reason=EndReason.AGENT_ENDED, agent_outcome="CONFIRMED"),
    )
    attributes = context.root_span.attributes
    assert attributes["connection_status"] == "ANSWERED"
    assert attributes["end_reason"] == "AGENT_ENDED"
    assert attributes["agent_outcome"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# Journey card
# ---------------------------------------------------------------------------


def _card(id: str, source_kind: str = "call") -> JourneyCard:
    return JourneyCard(
        id=id,
        merchant_id="shop",
        customer_id=uuid4(),
        channel=source_kind,
        started_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        outcome="BUSY",
        source_kind=source_kind,
    )


def test_journey_outcome_query_is_merchant_scoped():
    sql, values = lead_q.get_call_outcome_columns_query("shop", ["lead-1"])
    assert values == ["shop", ["lead-1"]]
    assert '"merchant_id" = $1' in sql
    assert '"id" = ANY($2::text[])' in sql
    for column in ("connection_status", "agent_outcome", "eval_outcome"):
        assert f'"{column}"' in sql


async def test_journey_fills_call_cards_only(monkeypatch):
    asked: List[Any] = []

    async def _columns(merchant_id, lead_ids):
        asked.append((merchant_id, list(lead_ids)))
        return {
            "lead-1": {
                "connection_status": "ANSWERED",
                "agent_outcome": "BUSY",
                "eval_outcome": "CONFIRMED",
            }
        }

    monkeypatch.setattr(timeline, "get_call_outcome_columns", _columns)
    cards = [_card("lead-1"), _card("lead-2"), _card("msg-1", "message")]
    got = await timeline.with_call_outcomes("shop", cards)
    assert asked == [("shop", ["lead-1", "lead-2"])]
    assert got[0].connection_status == "ANSWERED"
    assert got[0].agent_outcome == "BUSY"
    assert got[0].eval_outcome == "CONFIRMED"
    assert got[0].outcome == "BUSY"
    assert got[1].connection_status is None
    assert got[2] is cards[2]


async def test_journey_without_call_cards_reads_nothing(monkeypatch):
    async def _must_not_run(*_):
        raise AssertionError("no call card, no read")

    monkeypatch.setattr(timeline, "get_call_outcome_columns", _must_not_run)
    cards = [_card("msg-1", "message")]
    assert await timeline.with_call_outcomes("shop", cards) == cards


async def test_journey_read_is_fail_open(monkeypatch):
    async def _boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(timeline, "get_call_outcome_columns", _boom)
    cards = [_card("lead-1")]
    assert await timeline.with_call_outcomes("shop", cards) == cards
