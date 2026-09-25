"""The call outcome vocabulary (app/schemas/breeze_buddy/outcomes.py).

Pure mapping and normalization: what a carrier status becomes, how an agent
word is stored, which columns a write names, and how a completion fills in
what its caller left open.
"""

from types import SimpleNamespace

import pytest

from app.schemas.breeze_buddy.outcomes import (
    CallOutcome,
    ConnectionReason,
    ConnectionStatus,
    EndReason,
    OutcomeSource,
    call_outcome_from_lead,
    completed_call_outcome,
    connection_from_provider_status,
    end_reason_from_ended_by,
    ended_session_call_outcome,
    hangup_cause_from_callback,
    normalize_agent_outcome,
)


@pytest.mark.parametrize(
    "raw, status, reason",
    [
        ("no-answer", ConnectionStatus.NO_ANSWER, None),
        ("busy", ConnectionStatus.BUSY, None),
        ("failed", ConnectionStatus.FAILED, None),
        ("canceled", ConnectionStatus.CANCELED, None),
        ("cancelled", ConnectionStatus.CANCELED, None),
        ("cancel", ConnectionStatus.CANCELED, None),
        ("timeout", ConnectionStatus.NO_ANSWER, ConnectionReason.TIMEOUT),
        ("completed", ConnectionStatus.ANSWERED, None),
        (" BUSY ", ConnectionStatus.BUSY, None),
    ],
)
def test_carrier_status_maps_to_its_own_connection_status(raw, status, reason):
    """The legacy column says NO_ANSWER for all of these; the connection
    status keeps what the carrier actually said."""
    assert connection_from_provider_status(raw) == (status, reason)


@pytest.mark.parametrize("raw", [None, "", "ringing", "in-progress"])
def test_unknown_or_missing_carrier_status_maps_to_nothing(raw):
    assert connection_from_provider_status(raw) == (None, None)


def test_hangup_cause_prefers_the_named_cause():
    form = {"HangupCause": "16", "HangupCauseName": "NORMAL_CLEARING"}
    assert hangup_cause_from_callback(form) == "NORMAL_CLEARING"


def test_hangup_cause_falls_back_through_provider_keys():
    assert hangup_cause_from_callback({"SipResponseCode": 486}) == "486"
    assert hangup_cause_from_callback({"HangupCauseName": "", "ErrorCode": "3"}) == "3"
    assert hangup_cause_from_callback({"CallStatus": "busy"}) is None


@pytest.mark.parametrize(
    "raw, stored",
    [
        ("confirmed", "CONFIRMED"),
        ("  Cancel ", "CANCEL"),
        ("CALLBACK_REQUESTED", "CALLBACK_REQUESTED"),
        ("", None),
        ("   ", None),
        (None, None),
        ("X" * 51, None),
    ],
)
def test_agent_outcome_is_trimmed_and_upper_cased(raw, stored):
    assert normalize_agent_outcome(raw) == stored


def test_columns_names_only_what_the_write_carries():
    outcome = CallOutcome(
        connection_status=ConnectionStatus.ANSWERED,
        agent_outcome="confirmed",
        outcome_source=OutcomeSource.LLM,
    )
    assert outcome.columns() == {
        "connection_status": "ANSWERED",
        "agent_outcome": "CONFIRMED",
        "outcome_source": "LLM",
    }
    assert CallOutcome().columns() == {}


def test_columns_clip_raw_provider_text_to_the_column_width():
    columns = CallOutcome(provider_status="s" * 80, hangup_cause="c" * 150).columns()
    assert len(columns["provider_status"]) == 50
    assert len(columns["hangup_cause"]) == 100


def test_lead_values_are_read_tolerantly():
    """A value the vocabulary does not know is dropped, never raised: this
    runs on the call's terminal path."""
    lead = SimpleNamespace(
        connection_status="SOMETHING_NEW",
        end_reason=EndReason.IDLE_TIMEOUT,
        agent_outcome="resolved",
        outcome_source="NOT_A_SOURCE",
    )
    outcome = call_outcome_from_lead(lead)
    assert outcome.connection_status is None
    assert outcome.end_reason == EndReason.IDLE_TIMEOUT
    assert outcome.agent_outcome == "RESOLVED"
    assert outcome.outcome_source is None


def test_lead_values_take_overrides():
    lead = SimpleNamespace(end_reason="IDLE_TIMEOUT")
    outcome = call_outcome_from_lead(lead, end_reason=EndReason.TRANSFERRED)
    assert outcome.end_reason == EndReason.TRANSFERRED


@pytest.mark.parametrize(
    "ended_by, reason",
    [
        ("customer", EndReason.CUSTOMER_HANGUP),
        ("agent", EndReason.AGENT_ENDED),
        ("system", EndReason.PIPELINE_ERROR),
        (None, None),
        ("timeout", None),
    ],
)
def test_end_reason_fallback_from_call_ended_by(ended_by, reason):
    assert end_reason_from_ended_by(ended_by) == reason


def test_ended_session_prefers_the_explicit_end_reason():
    lead = SimpleNamespace(
        end_reason=EndReason.IDLE_TIMEOUT,
        metaData={"call_ended_by": "system"},
        agent_outcome="CONFIRM",
        outcome_source="LLM",
    )
    outcome = ended_session_call_outcome(lead)
    assert outcome.end_reason == EndReason.IDLE_TIMEOUT
    assert outcome.agent_outcome == "CONFIRM"
    assert outcome.outcome_source == OutcomeSource.LLM


def test_ended_session_falls_back_to_call_ended_by():
    """A late mid-call refresh can drop the in-memory end_reason; metaData's
    call_ended_by survives it."""
    lead = SimpleNamespace(end_reason=None, metaData={"call_ended_by": "customer"})
    assert ended_session_call_outcome(lead).end_reason == EndReason.CUSTOMER_HANGUP


def test_ended_session_without_metadata():
    lead = SimpleNamespace(end_reason=None, metaData=None)
    assert ended_session_call_outcome(lead).end_reason is None


def test_completion_is_answered_unless_the_caller_says_otherwise():
    assert completed_call_outcome(None).connection_status == ConnectionStatus.ANSWERED
    given = CallOutcome(connection_status=ConnectionStatus.UNKNOWN)
    assert completed_call_outcome(given).connection_status == ConnectionStatus.UNKNOWN


def test_transfer_is_how_the_session_ended_not_the_agent_outcome():
    """The legacy column turns the agent's word into TRANSFERRED; the call
    outcome columns keep the word and record the transfer as the ending."""
    given = CallOutcome(
        end_reason=EndReason.AGENT_ENDED,
        agent_outcome="RESOLVED",
        outcome_source=OutcomeSource.LLM,
    )
    outcome = completed_call_outcome(given, is_transfer=True)
    assert outcome.end_reason == EndReason.TRANSFERRED
    assert outcome.agent_outcome == "RESOLVED"
    assert given.end_reason == EndReason.AGENT_ENDED  # caller's copy untouched
