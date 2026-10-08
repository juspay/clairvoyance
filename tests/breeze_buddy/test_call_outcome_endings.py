"""Every ending records the facts that give its ``outcome`` word.

The ending paths no longer write a word of their own: they record how the
session ended (and the agent its word), and the completion writes
``outcome`` from those facts. Each test runs the real handler, then feeds the
facts it recorded through the completion (``ended_session_call_outcome`` →
``completed_call_outcome`` → ``outcome_word``) and demands today's word.
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
from app.schemas import CallDirection, ExecutionMode, InboundBlockAction, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.schemas.breeze_buddy.outcomes import (
    PlatformReason,
    SessionEndReason,
    completed_call_outcome,
    ended_session_call_outcome,
    not_initiated_call_outcome,
    outcome_word,
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
    """The word the completion writes for the facts this lead recorded."""
    return outcome_word(completed_call_outcome(ended_session_call_outcome(lead)))


# ---------------------------------------------------------------------------
# ending paths: facts only, and the word they give
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
    lead = make_lead(agent_outcome="confirmed")
    await agent_mod.Agent._handle_user_idle_timeout(_agent(lead), 2)

    assert lead.outcome is None  # no word of its own any more
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

    assert lead.outcome is None
    assert lead.session_end_reason == session_end_reason
    assert completed_word(lead) == "BUSY"


async def test_disconnect_keeps_the_agents_word(ended):
    lead = make_lead(agent_outcome="confirmed")
    await agent_mod.Agent._handle_unexpected_disconnect(
        _agent(lead), "client_disconnected"
    )

    assert completed_word(lead) == "confirmed"


async def test_the_first_ending_wins_over_a_later_disconnect(ended):
    lead = make_lead(session_end_reason=SessionEndReason.USER_IDLE_TIMEOUT)
    await agent_mod.Agent._handle_unexpected_disconnect(
        _agent(lead), "client_disconnected"
    )

    assert lead.session_end_reason == SessionEndReason.USER_IDLE_TIMEOUT
    assert completed_word(lead) == "BUSY"


@pytest.mark.parametrize("word", [None, "confirmed"])
async def test_global_end_is_busy_only_without_a_word(ended, word):
    lead = make_lead(agent_outcome=word)
    lead.metaData = {"call_ended_by": "agent"}
    context: Any = SimpleNamespace(lead=lead, call_sid="CA-1")
    await global_mod.end_conversation_global(context, {"reason": "done"})

    assert lead.outcome is None
    assert lead.session_end_reason == SessionEndReason.GLOBAL_END
    assert completed_word(lead) == (word or "BUSY")


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
    lead = make_lead(session_end_reason=SessionEndReason.USER_IDLE_TIMEOUT)
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
    # The new agent decides and the customer hangs up: its word.
    lead.agent_outcome = "confirmed"
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
    "ending",
    [
        SessionEndReason.IVR_ENDED,
        SessionEndReason.IVR_NO_INPUT,
        SessionEndReason.CUSTOMER_HANGUP,
        SessionEndReason.IVR_EXCEPTION,
    ],
)
def test_ivr_with_nothing_chosen_is_busy(ending):
    """No option word: the walk's ending gives BUSY, so the call re-dials."""
    assert completed_word(make_lead(session_end_reason=ending)) == "BUSY"


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

    assert lead.outcome is None
    assert lead.agent_outcome == "CONFIRM"
    assert lead.session_end_reason == SessionEndReason(error)
    assert completed_word(lead) == error


def test_ivr_exception_keeps_an_earlier_option():
    lead = make_lead(
        session_end_reason=SessionEndReason.IVR_EXCEPTION, agent_outcome="CONFIRM"
    )
    assert completed_word(lead) == "CONFIRM"


def test_an_ivr_setup_error_is_its_own_word():
    assert completed_word(make_lead(session_end_reason="IVR_ERROR")) == "IVR_ERROR"


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
async def test_blocked_inbound_writes_the_word_its_reason_gives(
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
    assert insert["outcome"] == reason.value
