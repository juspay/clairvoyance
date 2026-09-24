"""Call outcome columns on every write path (migration 080).

Each write path keeps writing the legacy ``outcome`` exactly as before and
carries the call outcome columns beside it, in the same statement. These
tests pin both halves: the legacy value a path writes is unchanged, and the
columns it adds say what the path actually knows. The switch
(CALL_OUTCOME_WRITES_ENABLED) is covered at the accessor gate.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

# Import the dispatch package first: managers.calls reaches into
# dispatch.alerts, whose package __init__ imports dispatch.worker, which
# imports managers.calls straight back.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.ivr import walker as walker_mod
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.ai.voice.agents.breeze_buddy.template import hooks as hooks_mod
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig
from app.database.accessor.breeze_buddy import (
    call_outcome as gate_mod,
    chat_session as chat_acc,
    lead_call_tracker as lead_acc,
)
from app.database.queries.breeze_buddy import (
    chat_session as chat_q,
    lead_call_tracker as lead_q,
)
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.schemas.breeze_buddy.outcomes import (
    CallOutcome,
    ConnectionReason,
    ConnectionStatus,
    EndReason,
    OutcomeSource,
)

CALL_SID = "CA-1"


def make_lead(**overrides: Any) -> LeadCallTracker:
    values: Dict[str, Any] = dict(
        id="lead-1",
        reseller_id="breeze",
        template="order-confirmation",
        template_id="tmpl-1",
        merchant_id="shop",
        request_id="req-1",
        payload={"customer_mobile_number": "+919999999999"},
        metaData={},
        status=LeadCallStatus.PROCESSING,
        call_id=CALL_SID,
        call_initiated_time=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
    )
    values.update(overrides)
    return LeadCallTracker(**values)


def set_writes(monkeypatch: pytest.MonkeyPatch, enabled: Any) -> None:
    """Point the gate's flag at a fixed answer (or an exception)."""

    async def flag() -> bool:
        if isinstance(enabled, Exception):
            raise enabled
        return enabled

    monkeypatch.setattr(gate_mod, "CALL_OUTCOME_WRITES_ENABLED", flag)


# ---------------------------------------------------------------------------
# Query builders: the columns ride the same statement
# ---------------------------------------------------------------------------


def test_completion_query_adds_columns_before_the_where_clause():
    text, values = lead_q.update_lead_call_completion_details_query(
        "lead-1",
        LeadCallStatus.FINISHED,
        "NO_ANSWER",
        None,
        None,
        LeadCallStatus.PROCESSING,
        call_outcome=CallOutcome(
            connection_status=ConnectionStatus.BUSY, provider_status="busy"
        ),
    )
    assert '"connection_status" = $3' in text
    assert '"provider_status" = $4' in text
    assert '"id" = $5' in text and '"status" = $6' in text
    assert values == ["FINISHED", "NO_ANSWER", "BUSY", "busy", "lead-1", "PROCESSING"]


def test_insert_query_appends_columns_and_values_in_step():
    text, values = lead_q.insert_lead_call_tracker_query(
        "lead-1",
        "breeze",
        "tmpl",
        "shop",
        None,
        None,
        None,
        status=LeadCallStatus.FINISHED,
        outcome="BLOCKED_REJECT",
        call_outcome=CallOutcome(
            connection_status=ConnectionStatus.REJECTED,
            connection_reason=ConnectionReason.BLOCKED,
        ),
    )
    assert '"connection_status"' in text and '"connection_reason"' in text
    assert "$23" in text and "$24" not in text
    assert len(values) == 23
    assert values[-2:] == ["REJECTED", "BLOCKED"]


def test_abort_query_adds_columns_after_its_fixed_parameters():
    text, values = lead_q.abort_lead_by_id_query(
        "lead-1",
        "customer asked",
        call_outcome=CallOutcome(
            connection_status=ConnectionStatus.NOT_DIALED,
            connection_reason=ConnectionReason.ABORTED,
        ),
    )
    assert '"connection_status" = $7' in text
    assert '"connection_reason" = $8' in text
    assert values[6:] == ["NOT_DIALED", "ABORTED"]


def test_widget_reset_clears_the_columns_only_when_asked():
    cleared, _ = lead_q.reset_widget_voice_lead_query(
        "lead-1", {}, {}, "DAILY_STREAM", clear_call_outcome=True
    )
    kept, _ = lead_q.reset_widget_voice_lead_query("lead-1", {}, {}, "DAILY_STREAM")
    assert '"agent_outcome" = NULL' in cleared
    assert '"eval_result_id" = NULL' in cleared
    assert "agent_outcome" not in kept


def test_chat_outcome_query_adds_the_agent_columns():
    text, values = chat_q.update_chat_session_outcome_query(
        "session-1", "confirmed", "CONFIRMED", "LLM"
    )
    assert "agent_outcome = $3" in text and "outcome_source = $4" in text
    assert values == ["session-1", "confirmed", "CONFIRMED", "LLM"]


# ---------------------------------------------------------------------------
# The gate: off (the default) means a legacy-only statement
# ---------------------------------------------------------------------------


class _QuerySpy:
    def __init__(self) -> None:
        self.kwargs: Dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return "SELECT 1", []


async def _no_rows(*_args: Any, **_kwargs: Any) -> List[Any]:
    return []


@pytest.mark.parametrize(
    "enabled, forwarded",
    [(True, True), (False, False), (RuntimeError("redis down"), False)],
)
async def test_completion_accessor_forwards_columns_only_when_on(
    monkeypatch, enabled, forwarded
):
    spy = _QuerySpy()
    monkeypatch.setattr(lead_acc, "update_lead_call_completion_details_query", spy)
    monkeypatch.setattr(lead_acc, "run_parameterized_query", _no_rows)
    set_writes(monkeypatch, enabled)
    outcome = CallOutcome(connection_status=ConnectionStatus.ANSWERED)

    await lead_acc.update_lead_call_completion_details(
        id="lead-1", outcome="CONFIRM", call_outcome=outcome
    )
    assert spy.kwargs["call_outcome"] == (outcome if forwarded else None)


async def test_insert_accessor_forwards_columns_only_when_on(monkeypatch):
    spy = _QuerySpy()
    monkeypatch.setattr(lead_acc, "insert_lead_call_tracker_query", spy)
    monkeypatch.setattr(lead_acc, "run_parameterized_query", _no_rows)
    outcome = CallOutcome(connection_status=ConnectionStatus.REJECTED)

    set_writes(monkeypatch, False)
    await lead_acc.create_lead_call_tracker(
        "lead-1",
        "breeze",
        "tmpl",
        "shop",
        None,
        None,
        template_id="t",
        call_outcome=outcome,
    )
    assert spy.kwargs["call_outcome"] is None

    set_writes(monkeypatch, True)
    await lead_acc.create_lead_call_tracker(
        "lead-1",
        "breeze",
        "tmpl",
        "shop",
        None,
        None,
        template_id="t",
        call_outcome=outcome,
    )
    assert spy.kwargs["call_outcome"] == outcome


@pytest.mark.parametrize("enabled", [True, False])
async def test_abort_records_not_dialed_only_when_on(monkeypatch, enabled):
    spy = _QuerySpy()
    monkeypatch.setattr(lead_acc, "abort_lead_by_id_query", spy)
    monkeypatch.setattr(lead_acc, "run_parameterized_query", _no_rows)
    set_writes(monkeypatch, enabled)

    await lead_acc.handle_lead_abort("lead-1", "customer asked")
    expected = CallOutcome(
        connection_status=ConnectionStatus.NOT_DIALED,
        connection_reason=ConnectionReason.ABORTED,
    )
    assert spy.kwargs["call_outcome"] == (expected if enabled else None)


@pytest.mark.parametrize("enabled", [True, False])
async def test_widget_reset_clears_columns_only_when_on(monkeypatch, enabled):
    spy = _QuerySpy()
    monkeypatch.setattr(lead_acc, "reset_widget_voice_lead_query", spy)
    monkeypatch.setattr(lead_acc, "run_parameterized_query", _no_rows)
    set_writes(monkeypatch, enabled)

    await lead_acc.reset_widget_voice_lead("lead-1", {}, {})
    assert spy.kwargs["clear_call_outcome"] is enabled


@pytest.mark.parametrize("enabled", [True, False])
async def test_chat_accessor_drops_agent_columns_when_off(monkeypatch, enabled):
    seen: Dict[str, Any] = {}

    def spy(session_id, outcome, agent_outcome=None, outcome_source=None):
        seen.update(agent_outcome=agent_outcome, outcome_source=outcome_source)
        return "SELECT 1", []

    monkeypatch.setattr(chat_acc, "update_chat_session_outcome_query", spy)
    monkeypatch.setattr(chat_acc, "run_parameterized_query", _no_rows)
    set_writes(monkeypatch, enabled)

    await chat_acc.update_chat_session_outcome(
        "session-1", "confirmed", "CONFIRMED", "LLM"
    )
    if enabled:
        assert seen == {"agent_outcome": "CONFIRMED", "outcome_source": "LLM"}
    else:
        assert seen == {"agent_outcome": None, "outcome_source": None}


# ---------------------------------------------------------------------------
# managers/calls.py: carrier failures, completion, reconcile, reaper
# ---------------------------------------------------------------------------


class _CallsHarness:
    """Stubs managers.calls' collaborators; records its terminal writes."""

    def __init__(self, lead: LeadCallTracker) -> None:
        self.lead = lead
        self.writes: List[Dict[str, Any]] = []
        self.retries: List[Optional[str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def noop(*_a: Any, **_k: Any) -> None:
            return None

        async def get_lead(_call_id: str) -> LeadCallTracker:
            return self.lead

        async def redis() -> Any:
            return SimpleNamespace(delete=noop)

        async def config(*_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(max_retry=3, retry_offset=60)

        async def write(**kwargs: Any) -> LeadCallTracker:
            self.writes.append(kwargs)
            return self.lead

        async def retry(_lead: Any, _config: Any, outcome: Optional[str] = None):
            self.retries.append(outcome)

        async def claim(*_a: Any, **_k: Any) -> LeadCallTracker:
            return self.lead

        monkeypatch.setattr(calls_mod, "safe_release_pod", noop)
        monkeypatch.setattr(calls_mod, "get_lead_by_call_id", get_lead)
        monkeypatch.setattr(calls_mod, "get_redis_service", redis)
        monkeypatch.setattr(calls_mod, "_release_call_resources", noop)
        monkeypatch.setattr(calls_mod, "_get_lead_config", config)
        monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", write)
        monkeypatch.setattr(calls_mod, "_retry_call", retry)
        monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", claim)
        monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", noop)
        monkeypatch.setattr(calls_mod.asyncio, "sleep", noop)


async def test_carrier_busy_stays_no_answer_in_legacy_and_busy_in_connection(
    monkeypatch,
):
    harness = _CallsHarness(make_lead())
    harness.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(
        CALL_SID, provider_status="busy", hangup_cause="USER_BUSY"
    )

    (write,) = harness.writes
    assert write["outcome"] == "NO_ANSWER"
    assert write["meta_data"] == {}
    assert write["call_outcome"] == CallOutcome(
        connection_status=ConnectionStatus.BUSY,
        provider_status="busy",
        hangup_cause="USER_BUSY",
    )
    assert harness.retries == ["NO_ANSWER"]  # today's retry, unchanged


async def test_carrier_timeout_is_no_answer_with_its_reason(monkeypatch):
    harness = _CallsHarness(make_lead())
    harness.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(CALL_SID, provider_status="timeout")

    assert harness.writes[0]["call_outcome"] == CallOutcome(
        connection_status=ConnectionStatus.NO_ANSWER,
        connection_reason=ConnectionReason.TIMEOUT,
        provider_status="timeout",
    )


async def test_unanswered_without_a_carrier_status_defaults_to_no_answer(monkeypatch):
    harness = _CallsHarness(make_lead())
    harness.install(monkeypatch)

    await calls_mod.handle_unanswered_calls(CALL_SID)

    assert harness.writes[0]["call_outcome"] == CallOutcome(
        connection_status=ConnectionStatus.NO_ANSWER
    )


async def test_transfer_keeps_the_agents_word_and_records_how_it_ended(monkeypatch):
    lead = make_lead(metaData={"transfer": {"status": "success"}})
    harness = _CallsHarness(lead)
    harness.install(monkeypatch)

    await calls_mod.handle_call_completion(
        CALL_SID,
        outcome="RESOLVED",
        call_outcome=CallOutcome(
            end_reason=EndReason.AGENT_ENDED,
            agent_outcome="RESOLVED",
            outcome_source=OutcomeSource.LLM,
        ),
    )

    (write,) = harness.writes
    assert write["outcome"] == "TRANSFERRED"  # legacy override, unchanged
    assert write["call_outcome"] == CallOutcome(
        connection_status=ConnectionStatus.ANSWERED,
        end_reason=EndReason.TRANSFERRED,
        agent_outcome="RESOLVED",
        outcome_source=OutcomeSource.LLM,
    )


async def test_completion_from_an_old_caller_is_still_answered(monkeypatch):
    harness = _CallsHarness(make_lead())
    harness.install(monkeypatch)

    await calls_mod.handle_call_completion(CALL_SID, outcome="BUSY")

    (write,) = harness.writes
    assert write["outcome"] == "BUSY"
    assert write["call_outcome"] == CallOutcome(
        connection_status=ConnectionStatus.ANSWERED
    )
    assert harness.retries == ["BUSY"]  # today's retry, unchanged


async def test_completed_call_with_no_pipeline_is_answered_and_not_started(
    monkeypatch,
):
    harness = _CallsHarness(make_lead())
    harness.install(monkeypatch)

    await calls_mod.reconcile_completed_call(CALL_SID)

    (write,) = harness.writes
    assert write["outcome"] == "UNKNOWN"
    assert write["call_outcome"] == CallOutcome(
        connection_status=ConnectionStatus.ANSWERED,
        provider_status="completed",
        end_reason=EndReason.PIPELINE_NOT_STARTED,
    )


@pytest.mark.parametrize(
    "legacy, expected",
    [
        (
            "CONFIRM",
            CallOutcome(
                connection_status=ConnectionStatus.ANSWERED,
                end_reason=EndReason.PIPELINE_ERROR,
            ),
        ),
        (None, CallOutcome(connection_status=ConnectionStatus.UNKNOWN)),
    ],
)
async def test_reaper_says_answered_only_when_a_pipeline_ran(
    monkeypatch, legacy, expected
):
    lead = make_lead(outcome=legacy, is_locked=True)
    harness = _CallsHarness(lead)
    harness.install(monkeypatch)

    async def stale(*_a: Any, **_k: Any) -> List[LeadCallTracker]:
        return [lead]

    monkeypatch.setattr(calls_mod, "get_leads_by_status_and_time_before", stale)

    await calls_mod.reconcile_stuck_processing_leads()

    (write,) = harness.writes
    assert write["outcome"] == (legacy or "UNKNOWN")
    assert write["call_outcome"] == expected


# ---------------------------------------------------------------------------
# template/hooks.py: the agent outcome
# ---------------------------------------------------------------------------


class _HookHarness:
    def __init__(
        self, lead: Optional[LeadCallTracker], session_id: Optional[str] = None
    ):
        self.bot = SimpleNamespace(lead=lead, call_sid=CALL_SID, session_id=session_id)
        self.context = TemplateContext(self.bot)
        self.writes: List[Dict[str, Any]] = []
        self.chat_writes: List[Dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def write(**kwargs: Any) -> None:
            self.writes.append(kwargs)
            return None  # keep context.lead as the test's object

        async def chat_write(session_id: str, outcome: str, **kwargs: Any) -> str:
            self.chat_writes.append({"outcome": outcome, **kwargs})
            return outcome

        monkeypatch.setattr(hooks_mod, "update_lead_call_completion_details", write)
        monkeypatch.setattr(hooks_mod, "update_chat_session_outcome", chat_write)

    async def fire(self, outcome: str, function_name: str) -> None:
        await hooks_mod.UpdateOutcomeInDatabaseHook().execute(
            self.context,
            {"outcome": outcome},
            function_name,
            HookConfig(name="update_outcome_in_database"),
        )


async def test_llm_outcome_is_recorded_upper_cased_beside_the_legacy_word(monkeypatch):
    lead = make_lead()
    harness = _HookHarness(lead)
    harness.install(monkeypatch)

    await harness.fire("confirmed", "confirm_order")

    (write,) = harness.writes
    assert write["outcome"] == "confirmed"  # legacy casing untouched
    assert lead.agent_outcome == "CONFIRMED"
    assert lead.outcome_source == OutcomeSource.LLM
    assert write["call_outcome"] == CallOutcome(
        agent_outcome="CONFIRMED", outcome_source=OutcomeSource.LLM
    )


async def test_transfer_override_is_legacy_only(monkeypatch):
    lead = make_lead(metaData={"transfer": {"status": "success"}})
    harness = _HookHarness(lead)
    harness.install(monkeypatch)

    await harness.fire("RESOLVED", "mark_resolved")

    assert harness.writes[0]["outcome"] == "TRANSFERRED"
    assert lead.agent_outcome == "RESOLVED"


async def test_an_observers_outcome_is_not_replaced_by_a_later_llm_call(monkeypatch):
    # observer.execute_action marks observer_triggered and sets the legacy
    # outcome in memory before it fires the hook with its own name.
    lead = make_lead(
        metaData={"observer_triggered": "voicemail_detector"}, outcome="VOICEMAIL"
    )
    harness = _HookHarness(lead)
    harness.install(monkeypatch)

    await harness.fire("VOICEMAIL", "voicemail_detector")
    assert lead.outcome_source == OutcomeSource.OBSERVER

    await harness.fire("BUSY", "customer_busy")

    assert harness.writes[-1]["outcome"] == "VOICEMAIL"  # legacy guard
    assert lead.agent_outcome == "VOICEMAIL"
    assert lead.outcome_source == OutcomeSource.OBSERVER


async def test_chat_outcome_carries_the_agent_columns(monkeypatch):
    harness = _HookHarness(None, session_id="session-1")
    harness.install(monkeypatch)

    await harness.fire("confirmed", "confirm_order")

    assert harness.chat_writes == [
        {"outcome": "confirmed", "agent_outcome": "CONFIRMED", "outcome_source": "LLM"}
    ]


# ---------------------------------------------------------------------------
# ivr/walker.py: options are the agent's word, walker errors are not
# ---------------------------------------------------------------------------


def _walker(lead: LeadCallTracker) -> Any:
    agent = SimpleNamespace(
        ws=object(), stream_sid="MZ1", provider="plivo", lead=lead, errors=[]
    )
    return walker_mod.IvrWalker(agent)  # type: ignore[arg-type]


async def test_ivr_option_outcome_is_the_agent_outcome(monkeypatch):
    writes: List[Dict[str, Any]] = []

    async def write(**kwargs: Any) -> None:
        writes.append(kwargs)

    monkeypatch.setattr(walker_mod, "update_lead_call_completion_details", write)
    lead = make_lead()
    walker = _walker(lead)

    walker._persist_outcome("confirmed", {"choice": "1"})
    await walker._drain_bg_tasks()

    assert lead.outcome == "confirmed"
    assert lead.agent_outcome == "CONFIRMED"
    assert lead.outcome_source == OutcomeSource.IVR
    assert writes[0]["outcome"] == "confirmed"
    assert writes[0]["call_outcome"] == CallOutcome(
        agent_outcome="CONFIRMED", outcome_source=OutcomeSource.IVR
    )


async def test_ivr_system_error_is_an_end_reason_not_an_agent_outcome(monkeypatch):
    writes: List[Dict[str, Any]] = []

    async def write(**kwargs: Any) -> None:
        writes.append(kwargs)

    monkeypatch.setattr(walker_mod, "update_lead_call_completion_details", write)
    lead = make_lead()
    walker = _walker(lead)

    walker._persist_system_error("IVR_LOOP_GUARD")
    await walker._drain_bg_tasks()

    assert lead.outcome == "IVR_LOOP_GUARD"
    assert lead.agent_outcome is None
    assert lead.end_reason == EndReason.IVR_ERROR
    assert writes[0]["call_outcome"] == CallOutcome(end_reason=EndReason.IVR_ERROR)
