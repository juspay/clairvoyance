"""PR 1 of docs/CALL_OUTCOMES.md: every ending records facts that give back
its legacy word, and the shadow check compares the two on terminal writes.

The ending-path tests run the real handler, then feed the facts it recorded
through the completion (``ended_session_call_outcome`` → ``completed_call_outcome``)
into ``legacy_outcome`` and demand the word the handler wrote to the legacy
column — the same equality the shadow check watches in production.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

# Import the dispatch package first: managers.calls reaches into
# dispatch.alerts, whose package __init__ imports dispatch.worker, which
# imports managers.calls straight back.
from app.ai.voice.agents.breeze_buddy import (  # noqa: F401
    agent as agent_mod,
    dispatch as _dispatch,
)
from app.ai.voice.agents.breeze_buddy.agent import transfer as transfer_mod
from app.ai.voice.agents.breeze_buddy.handlers.internal import (
    end_conversation_global as global_mod,
)
from app.ai.voice.agents.breeze_buddy.ivr import walker as walker_mod
from app.ai.voice.agents.breeze_buddy.services import inbound_policy as policy_mod
from app.database.accessor.breeze_buddy import (
    call_outcome as gate_mod,
    lead_call_tracker as lead_acc,
)
from app.schemas import CallDirection, ExecutionMode, InboundBlockAction, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.schemas.breeze_buddy.outcomes import (
    PlatformReason,
    SessionEndReason,
    completed_call_outcome,
    ended_session_call_outcome,
    legacy_outcome,
    not_initiated_call_outcome,
)


def make_lead(**overrides: Any) -> LeadCallTracker:
    values: Dict[str, Any] = dict(
        id="lead-1",
        reseller_id="breeze",
        template="order-confirmation",
        template_id="tmpl-1",
        merchant_id="shop",
        payload={"customer_mobile_number": "+919999999999"},
        metaData={},
        status=LeadCallStatus.PROCESSING,
        call_id="CA-1",
        call_initiated_time=datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc),
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
    )
    values.update(overrides)
    return LeadCallTracker(**values)


def completed_word(lead: LeadCallTracker) -> Optional[str]:
    """What the facts give once the completion has written them."""
    return legacy_outcome(completed_call_outcome(ended_session_call_outcome(lead)))


def set_writes(monkeypatch: pytest.MonkeyPatch, enabled: Any) -> None:
    async def flag() -> bool:
        if isinstance(enabled, Exception):
            raise enabled
        return enabled

    monkeypatch.setattr(gate_mod, "CALL_OUTCOME_WRITES_ENABLED", flag)


# ---------------------------------------------------------------------------
# ending paths: the facts they record give back the word they write
# ---------------------------------------------------------------------------


@pytest.fixture
def ended(monkeypatch) -> List[Any]:
    """Stub the conversation end the ending paths hand off to."""
    calls: List[Any] = []

    async def end_conversation(context: Any, args: Any, *_a: Any) -> Dict:
        calls.append(context)
        return {}

    monkeypatch.setattr(agent_mod, "end_conversation", end_conversation)
    monkeypatch.setattr(agent_mod, "TemplateContext", lambda agent: agent)
    monkeypatch.setattr(global_mod, "end_conversation", end_conversation)

    async def mute_stt(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(global_mod, "mute_stt", mute_stt)
    return calls


def _agent(lead: LeadCallTracker) -> Any:
    return SimpleNamespace(
        conversation_ended=False, lead=lead, _transcript_collector=None
    )


async def test_user_idle_timeout_overrides_the_agents_word(ended):
    lead = make_lead(outcome="confirmed", agent_outcome="confirmed")
    await agent_mod.Agent._handle_user_idle_timeout(_agent(lead), 2)

    assert lead.outcome == "BUSY"
    assert lead.session_end_reason == SessionEndReason.USER_IDLE_TIMEOUT
    assert completed_word(lead) == "BUSY"


@pytest.mark.parametrize(
    "reason, session_end_reason",
    [
        ("client_disconnected", SessionEndReason.CUSTOMER_HANGUP),
        ("idle_timeout", SessionEndReason.IDLE_TIMEOUT),
        ("something_else", SessionEndReason.CUSTOMER_HANGUP),
    ],
)
async def test_disconnect_with_no_word_is_busy(ended, reason, session_end_reason):
    lead = make_lead()
    await agent_mod.Agent._handle_unexpected_disconnect(_agent(lead), reason)

    assert lead.outcome == "BUSY"
    assert lead.session_end_reason == session_end_reason
    assert completed_word(lead) == "BUSY"


async def test_disconnect_keeps_the_agents_word(ended):
    lead = make_lead(outcome="confirmed", agent_outcome="confirmed")
    await agent_mod.Agent._handle_unexpected_disconnect(
        _agent(lead), "client_disconnected"
    )

    assert lead.outcome == "confirmed"
    assert completed_word(lead) == "confirmed"


async def test_the_first_ending_wins_over_a_later_disconnect(ended):
    lead = make_lead(
        session_end_reason=SessionEndReason.USER_IDLE_TIMEOUT, outcome="BUSY"
    )
    await agent_mod.Agent._handle_unexpected_disconnect(
        _agent(lead), "client_disconnected"
    )

    assert lead.session_end_reason == SessionEndReason.USER_IDLE_TIMEOUT
    assert completed_word(lead) == "BUSY"


@pytest.mark.parametrize("word", [None, "confirmed"])
async def test_global_end_fills_busy_only_without_a_word(ended, word):
    lead = make_lead(outcome=word, agent_outcome=word)
    lead.metaData = {"call_ended_by": "agent"}
    context: Any = SimpleNamespace(lead=lead, call_sid="CA-1")
    await global_mod.end_conversation_global(context, {"reason": "done"})

    assert lead.outcome == (word or "BUSY")
    assert lead.session_end_reason == SessionEndReason.GLOBAL_END
    assert completed_word(lead) == lead.outcome


async def test_an_agent_transfer_forgets_the_outgoing_generations_ending(
    monkeypatch,
):
    """An idle timeout firing in the same tick as an agent-to-agent transfer
    records its ending, but end_conversation skips it and the call goes on
    with the new agent: the new generation must start with no ending."""

    class _Stop(Exception):
        pass

    class _Context:
        def __init__(self, _bot: Any) -> None:
            pass

        def record_node_exit(self) -> None:
            return None

        def _get_ist_timestamp(self) -> str:
            return "t"

    async def stop(**_k: Any) -> None:
        raise _Stop()

    monkeypatch.setattr(transfer_mod, "TemplateContext", _Context)
    monkeypatch.setattr(transfer_mod, "update_lead_template", stop)
    lead = make_lead(
        session_end_reason=SessionEndReason.USER_IDLE_TIMEOUT, outcome="BUSY"
    )
    bot: Any = SimpleNamespace(
        transfer_count=0,
        generation=0,
        context=None,
        metrics_collector=None,
        lead=lead,
        template=None,
    )
    target: Any = SimpleNamespace(
        template=SimpleNamespace(name="next-agent", id="tmpl-2")
    )

    with pytest.raises(_Stop):
        await transfer_mod.apply_transfer(bot, target)

    assert lead.session_end_reason is None
    # The new agent decides and the customer hangs up: the facts give the
    # new agent's word, as the legacy column does.
    lead.agent_outcome = lead.outcome = "confirmed"
    lead.metaData = {"call_ended_by": "customer"}
    assert completed_word(lead) == "confirmed"


# ---------------------------------------------------------------------------
# IVR walker endings
# ---------------------------------------------------------------------------


def _walker(lead: LeadCallTracker) -> Any:
    agent = SimpleNamespace(
        ws=object(), stream_sid="MZ1", provider="plivo", lead=lead, errors=[]
    )
    return walker_mod.IvrWalker(agent)  # type: ignore[arg-type]


def test_every_terminal_ivr_signal_has_its_ending():
    assert walker_mod._TERMINAL_END_REASONS == {
        walker_mod._END: SessionEndReason.IVR_ENDED,
        walker_mod._HANGUP: SessionEndReason.CUSTOMER_HANGUP,
        walker_mod._TIMEOUT: SessionEndReason.IVR_NO_INPUT,
    }


@pytest.mark.parametrize(
    "ending", [SessionEndReason.IVR_ENDED, SessionEndReason.IVR_NO_INPUT]
)
def test_ivr_with_nothing_chosen_is_busy(ending):
    """The finaliser fills BUSY; the IVR ending gives it back."""
    lead = make_lead(session_end_reason=ending, outcome="BUSY")
    assert completed_word(lead) == "BUSY"


@pytest.mark.parametrize("error", ["IVR_LOOP_GUARD", "IVR_NODE_MISSING"])
async def test_ivr_system_error_overrides_an_earlier_option(monkeypatch, error):
    async def write(**_k: Any) -> None:
        return None

    monkeypatch.setattr(walker_mod, "update_lead_call_completion_details", write)
    lead = make_lead()
    walker = _walker(lead)
    walker._persist_outcome("CONFIRM", {})
    walker._persist_system_error(error)
    await walker._drain_bg_tasks()

    assert lead.outcome == error
    assert lead.agent_outcome == "CONFIRM"
    assert lead.session_end_reason == SessionEndReason(error)
    assert completed_word(lead) == error


def test_ivr_exception_keeps_an_earlier_option():
    lead = make_lead(
        session_end_reason=SessionEndReason.IVR_EXCEPTION,
        outcome="CONFIRM",
        agent_outcome="CONFIRM",
    )
    assert completed_word(lead) == "CONFIRM"


# ---------------------------------------------------------------------------
# inbound blocks: the reason is the word
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action, redirect, outcome, reason",
    [
        (InboundBlockAction.REJECT, None, None, PlatformReason.BLOCKED_REJECT),
        (
            InboundBlockAction.REDIRECT,
            "+911234567890",
            None,
            PlatformReason.BLOCKED_REDIRECT,
        ),
        (None, None, "CAPACITY_REJECTED", PlatformReason.CAPACITY_REJECTED),
    ],
)
async def test_blocked_inbound_records_its_word_as_the_reason(
    monkeypatch, action, redirect, outcome, reason
):
    inserts: List[Dict[str, Any]] = []

    async def create(**kwargs: Any) -> None:
        inserts.append(kwargs)

    monkeypatch.setattr(policy_mod, "create_lead_call_tracker", create)
    await policy_mod.log_blocked_call(
        call_id="CA-1",
        from_number="+919999999999",
        to_number="+918888888888",
        provider="plivo",
        reseller_id="breeze",
        merchant_id="shop",
        template_name="t",
        template_id="tmpl-1",
        telephony_number_id=None,
        block_action=action,
        block_reason="policy",
        redirect_number=redirect,
        outcome=outcome,
    )

    (insert,) = inserts
    assert insert["call_outcome"] == not_initiated_call_outcome(reason)
    assert legacy_outcome(insert["call_outcome"]) == insert["outcome"]


# ---------------------------------------------------------------------------
# the shadow check
# ---------------------------------------------------------------------------


class _Log:
    """Records what check_legacy_outcome logs, and at which level."""

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []
        self._fields: Dict[str, Any] = {}

    def bind(self, **fields: Any) -> "_Log":
        child = _Log()
        child.entries = self.entries
        child._fields = {**self._fields, **fields}
        return child

    def _record(self, level: str, message: str) -> None:
        self.entries.append({"level": level, "message": message, **self._fields})

    def debug(self, message: str) -> None:
        self._record("debug", message)

    def warning(self, message: str) -> None:
        self._record("warning", message)

    def opt(self, **_k: Any) -> "_Log":
        return self


@pytest.fixture
def shadow_log(monkeypatch) -> _Log:
    log = _Log()
    monkeypatch.setattr(gate_mod, "logger", log)
    return log


def _finished(**overrides: Any) -> LeadCallTracker:
    return make_lead(status=LeadCallStatus.FINISHED, **overrides)


async def test_matching_word_is_a_quiet_match(monkeypatch, shadow_log):
    set_writes(monkeypatch, True)
    lead = _finished(
        outcome="NO_ANSWER",
        platform_status="INITIATED",
        platform_reason="DIALED",
        provider_status="NOT_ANSWERED",
        provider_reason="BUSY",
    )
    await gate_mod.check_legacy_outcome(lead, "completion")

    (entry,) = shadow_log.entries
    assert (entry["level"], entry["shadow"]) == ("debug", "match")
    assert entry["legacy"] == entry["derived"] == "NO_ANSWER"


async def test_disagreement_is_a_logged_mismatch(monkeypatch, shadow_log):
    set_writes(monkeypatch, True)
    lead = _finished(
        outcome="BUSY",
        platform_status="INITIATED",
        platform_reason="DIALED",
        provider_status="ANSWERED",
        provider_reason="COMPLETED",
        session_end_reason="AGENT_ENDED",
        agent_outcome="confirmed",
    )
    await gate_mod.check_legacy_outcome(lead, "completion")

    (entry,) = shadow_log.entries
    assert (entry["level"], entry["shadow"]) == ("warning", "mismatch")
    assert (entry["legacy"], entry["derived"]) == ("BUSY", "confirmed")
    assert entry["write"] == "completion"
    assert entry["session_end_reason"] == "AGENT_ENDED"
    assert entry["platform_reason"] == "DIALED"


async def test_a_terminal_row_without_facts_is_reported(monkeypatch, shadow_log):
    set_writes(monkeypatch, True)
    await gate_mod.check_legacy_outcome(_finished(outcome="ABORTED"), "insert")

    (entry,) = shadow_log.entries
    assert (entry["level"], entry["shadow"]) == ("warning", "no_facts")


@pytest.mark.parametrize("enabled", [False, RuntimeError("redis down")])
async def test_no_check_while_writes_are_off(monkeypatch, shadow_log, enabled):
    set_writes(monkeypatch, enabled)
    await gate_mod.check_legacy_outcome(_finished(outcome="BUSY"), "completion")
    # (an unreadable flag logs its own warning; no shadow verdict is logged)
    assert not [entry for entry in shadow_log.entries if "shadow" in entry]


async def test_only_terminal_rows_are_checked(monkeypatch, shadow_log):
    set_writes(monkeypatch, True)
    await gate_mod.check_legacy_outcome(make_lead(outcome="confirmed"), "completion")
    await gate_mod.check_legacy_outcome(None, "completion")
    assert shadow_log.entries == []


async def test_a_failing_check_never_raises(monkeypatch, shadow_log):
    set_writes(monkeypatch, True)

    def boom(_lead: Any) -> None:
        raise RuntimeError("bad facts")

    monkeypatch.setattr(gate_mod, "legacy_outcome", boom)
    await gate_mod.check_legacy_outcome(_finished(outcome="BUSY"), "completion")
    assert shadow_log.entries[-1]["level"] == "warning"


class _CheckSpy:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def __call__(self, lead: Any, write: str) -> None:
        self.calls.append((lead, write))


@pytest.mark.parametrize(
    "status, checked", [(LeadCallStatus.FINISHED, True), (None, False)]
)
async def test_completion_accessor_checks_only_terminal_writes(
    monkeypatch, status, checked
):
    spy = _CheckSpy()
    row = {"status": "FINISHED"}

    async def one_row(*_a: Any, **_k: Any) -> List[Any]:
        return [row]

    monkeypatch.setattr(lead_acc, "check_legacy_outcome", spy)
    monkeypatch.setattr(lead_acc, "run_parameterized_query", one_row)
    monkeypatch.setattr(lead_acc, "decode_lead_call_tracker", lambda _r: make_lead())
    monkeypatch.setattr(lead_acc, "_fire_hooks", lambda *_a: None)
    set_writes(monkeypatch, False)

    await lead_acc.update_lead_call_completion_details(id="lead-1", status=status)
    assert bool(spy.calls) is checked
    if checked:
        assert spy.calls[0][1] == "completion"
