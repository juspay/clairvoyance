"""The call outcome facts and legacy_outcome() (app/schemas/breeze_buddy/outcomes.py).

The golden table below is the plan's scenario table (docs/CALL_OUTCOMES.md,
section 4): for every way a lead ends today, the facts its writers record and
the exact ``outcome`` word the old writers produce. legacy_outcome() must
reproduce every one byte-for-byte — that is what lets phase 2 replace the old
writers with it.
"""

from types import SimpleNamespace

import pytest

from app.schemas.breeze_buddy.outcomes import (
    CallOutcome,
    ConnectionReason,
    ConnectionStatus,
    EndReason,
    OutcomeSource,
    agent_word,
    call_outcome_from_lead,
    completed_call_outcome,
    connection_from_provider_status,
    end_reason_from_meta,
    ended_session_call_outcome,
    hangup_cause_from_callback,
    legacy_outcome,
    record_end_reason,
)

# ---------------------------------------------------------------------------
# legacy_outcome: the scenario table
# ---------------------------------------------------------------------------

S = ConnectionStatus
R = ConnectionReason
E = EndReason


def facts(status=None, reason=None, end=None, word=None, source=None, provider=None):
    return SimpleNamespace(
        connection_status=status,
        connection_reason=reason,
        end_reason=end,
        agent_outcome=word,
        outcome_source=source,
        provider_status=provider,
    )


SCENARIOS = [
    # never dialed / turned away: the reason is the word
    ("pre-check aborts", facts(S.NOT_DIALED, R.PRECHECK_FAILED), "PRECHECK_FAILED"),
    ("blacklisted", facts(S.NOT_DIALED, R.BLACKLISTED), "BLACKLISTED"),
    ("no free number", facts(S.NOT_DIALED, R.NUMBER_UNAVAILABLE), "NUMBER_UNAVAILABLE"),
    ("invalid phone", facts(S.NOT_DIALED, R.INVALID_PHONE), "INVALID_PHONE"),
    ("no config", facts(S.NOT_DIALED, R.NO_CONFIG), "NO_CONFIG"),
    ("call limit", facts(S.NOT_DIALED, R.CALL_LIMIT_REACHED), "CALL_LIMIT_REACHED"),
    ("abort: API/campaign/widget/demo", facts(S.NOT_DIALED, R.ABORT), "ABORT"),
    ("abort: WooCommerce / CRM cap", facts(S.NOT_DIALED, R.ABORTED), "ABORTED"),
    ("inbound blocked, reject", facts(S.REJECTED, R.BLOCKED_REJECT), "BLOCKED_REJECT"),
    (
        "inbound blocked, redirect",
        facts(S.REJECTED, R.BLOCKED_REDIRECT),
        "BLOCKED_REDIRECT",
    ),
    ("inbound capacity", facts(S.REJECTED, R.CAPACITY_REJECTED), "CAPACITY_REJECTED"),
    # carrier failures: always NO_ANSWER
    ("no answer", facts(S.NO_ANSWER, provider="no-answer"), "NO_ANSWER"),
    ("line busy", facts(S.BUSY, provider="busy"), "NO_ANSWER"),
    ("call failed", facts(S.FAILED, provider="failed"), "NO_ANSWER"),
    ("cancelled", facts(S.CANCELED, provider="canceled"), "NO_ANSWER"),
    ("plivo timeout", facts(S.NO_ANSWER, R.TIMEOUT, provider="timeout"), "NO_ANSWER"),
    # answered, the agent decided: its word, raw
    (
        "agent decides, flow ends",
        facts(S.ANSWERED, end=E.AGENT_ENDED, word="confirmed", source="LLM"),
        "confirmed",
    ),
    (
        "agent decides, customer hangs up",
        facts(S.ANSWERED, end=E.CUSTOMER_HANGUP, word="confirmed", source="LLM"),
        "confirmed",
    ),
    (
        "agent decides, then transferred",
        facts(S.ANSWERED, end=E.TRANSFERRED, word="RESOLVED", source="LLM"),
        "TRANSFERRED",
    ),
    (
        "agent decides, then user idle",
        facts(S.ANSWERED, end=E.USER_IDLE_TIMEOUT, word="confirmed", source="LLM"),
        "BUSY",
    ),
    (
        "agent: customer busy, call later",
        facts(S.ANSWERED, end=E.AGENT_ENDED, word="BUSY", source="LLM"),
        "BUSY",
    ),
    (
        "voicemail observer",
        facts(S.ANSWERED, end=E.CUSTOMER_HANGUP, word="VOICEMAIL", source="OBSERVER"),
        "VOICEMAIL",
    ),
    # answered, no agent word: the ending's default
    ("no word, user idle", facts(S.ANSWERED, end=E.USER_IDLE_TIMEOUT), "BUSY"),
    ("no word, pipecat idle", facts(S.ANSWERED, end=E.IDLE_TIMEOUT), "BUSY"),
    ("no word, customer hangup", facts(S.ANSWERED, end=E.CUSTOMER_HANGUP), "BUSY"),
    ("no word, LLM global end", facts(S.ANSWERED, end=E.GLOBAL_END), "BUSY"),
    ("no word, flow end_conversation", facts(S.ANSWERED, end=E.AGENT_ENDED), None),
    ("early hangup", facts(S.ANSWERED, end=E.EARLY_HANGUP), "EARLY_HANGUP"),
    ("setup error", facts(S.ANSWERED, end=E.PIPELINE_ERROR), "UNKNOWN"),
    (
        "setup error after an agent word",
        facts(S.ANSWERED, end=E.PIPELINE_ERROR, word="confirmed", source="LLM"),
        "UNKNOWN",
    ),
    ("widget end, no word", facts(S.ANSWERED, end=E.WIDGET_ENDED), "ended_by_widget"),
    (
        "widget end, agent word",
        facts(S.ANSWERED, end=E.WIDGET_ENDED, word="confirmed", source="LLM"),
        "confirmed",
    ),
    # IVR
    (
        "IVR option chosen",
        facts(S.ANSWERED, end=E.IVR_ENDED, word="CONFIRM", source="IVR"),
        "CONFIRM",
    ),
    (
        "IVR no input, timeout word",
        facts(S.ANSWERED, end=E.IVR_NO_INPUT, word="NO_RESPONSE", source="IVR"),
        "NO_RESPONSE",
    ),
    ("IVR END option, nothing chosen", facts(S.ANSWERED, end=E.IVR_ENDED), "BUSY"),
    ("IVR no input, no timeout word", facts(S.ANSWERED, end=E.IVR_NO_INPUT), "BUSY"),
    ("IVR hangup, nothing chosen", facts(S.ANSWERED, end=E.CUSTOMER_HANGUP), "BUSY"),
    ("IVR setup error", facts(S.ANSWERED, end=E.IVR_ERROR), "IVR_ERROR"),
    (
        "IVR loop guard after an option",
        facts(S.ANSWERED, end=E.IVR_LOOP_GUARD, word="CONFIRM", source="IVR"),
        "IVR_LOOP_GUARD",
    ),
    ("IVR node missing", facts(S.ANSWERED, end=E.IVR_NODE_MISSING), "IVR_NODE_MISSING"),
    (
        "IVR exception after an option",
        facts(S.ANSWERED, end=E.IVR_EXCEPTION, word="CONFIRM", source="IVR"),
        "CONFIRM",
    ),
    ("IVR exception, nothing chosen", facts(S.ANSWERED, end=E.IVR_EXCEPTION), "BUSY"),
    # safety nets
    (
        "carrier completed, no pipeline",
        facts(S.ANSWERED, provider="completed"),
        "UNKNOWN",
    ),
    (
        "reaper, pipeline ran with a word",
        facts(S.ANSWERED, end=E.REAPED, word="confirmed", source="LLM"),
        "confirmed",
    ),
    (
        "reaper keeps an earlier ending",
        facts(S.ANSWERED, end=E.USER_IDLE_TIMEOUT, word="confirmed", source="LLM"),
        "BUSY",
    ),
    ("reaper, pipeline ran, no word", facts(S.ANSWERED, end=E.REAPED), "UNKNOWN"),
    ("reaper, no pipeline", facts(S.UNKNOWN), "UNKNOWN"),
    # chat: no connection facts, the agent's word
    ("chat session", facts(word="confirmed", source="LLM"), "confirmed"),
    ("nothing recorded", facts(), None),
]


@pytest.mark.parametrize(
    "name, lead, word", SCENARIOS, ids=[name for name, _, _ in SCENARIOS]
)
def test_legacy_outcome_reproduces_todays_word(name, lead, word):
    assert legacy_outcome(lead) == word


def test_legacy_outcome_reads_stored_strings_too():
    """A row read back from the database holds strings, not enum members."""
    lead = facts("ANSWERED", end="USER_IDLE_TIMEOUT", word="confirmed")
    assert legacy_outcome(lead) == "BUSY"
    assert legacy_outcome(facts("NOT_DIALED", "ABORT")) == "ABORT"


def test_legacy_outcome_keeps_the_agents_casing():
    lead = facts(S.ANSWERED, end=E.AGENT_ENDED, word="Address Updated ")
    assert legacy_outcome(lead) == "Address Updated "


def test_unknown_vocabulary_is_ignored_not_raised():
    lead = facts("SOMETHING_NEW", "WHATEVER", end="NOT_AN_ENDING", word="ok")
    assert legacy_outcome(lead) == "ok"


# ---------------------------------------------------------------------------
# carrier mapping
# ---------------------------------------------------------------------------


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
    """The legacy word is NO_ANSWER for all of these; the connection status
    keeps what the carrier actually said."""
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
    assert hangup_cause_from_callback({}) is None


# ---------------------------------------------------------------------------
# storing facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, stored",
    [
        ("confirmed", "confirmed"),
        (" Address Updated ", " Address Updated "),
        (42, "42"),
        ("", None),
        (None, None),
        ("x" * 51, None),
    ],
)
def test_agent_word_is_stored_exactly_as_the_legacy_column_stores_it(raw, stored):
    assert agent_word(raw) == stored


def test_columns_names_only_what_the_write_carries():
    outcome = CallOutcome(
        connection_status=ConnectionStatus.NOT_DIALED,
        connection_reason=ConnectionReason.PRECHECK_FAILED,
    )
    assert outcome.columns() == {
        "connection_status": "NOT_DIALED",
        "connection_reason": "PRECHECK_FAILED",
    }
    assert CallOutcome().columns() == {}


def test_columns_keep_the_agents_word_raw():
    outcome = CallOutcome(agent_outcome="confirmed", outcome_source=OutcomeSource.LLM)
    assert outcome.columns() == {"agent_outcome": "confirmed", "outcome_source": "LLM"}


def test_columns_clip_raw_provider_text_to_the_column_width():
    columns = CallOutcome(provider_status="x" * 80, hangup_cause="y" * 150).columns()
    assert len(columns["provider_status"]) == 50
    assert len(columns["hangup_cause"]) == 100


def test_lead_values_are_read_tolerantly():
    lead = SimpleNamespace(
        connection_status="ANSWERED",
        connection_reason="NOT_A_REASON",
        end_reason=EndReason.CUSTOMER_HANGUP,
        agent_outcome="confirmed",
        outcome_source="LLM",
    )
    outcome = call_outcome_from_lead(lead)
    assert outcome.connection_status is ConnectionStatus.ANSWERED
    assert outcome.connection_reason is None
    assert outcome.end_reason is EndReason.CUSTOMER_HANGUP
    assert outcome.agent_outcome == "confirmed"
    assert outcome.outcome_source is OutcomeSource.LLM


def test_lead_values_take_overrides():
    outcome = call_outcome_from_lead(
        SimpleNamespace(), end_reason=EndReason.IVR_ERROR, agent_outcome="X"
    )
    assert outcome.end_reason is EndReason.IVR_ERROR
    assert outcome.agent_outcome == "X"


def test_the_first_ending_wins():
    lead = SimpleNamespace(end_reason=None)
    record_end_reason(lead, EndReason.USER_IDLE_TIMEOUT)
    record_end_reason(lead, EndReason.CUSTOMER_HANGUP)
    assert lead.end_reason is EndReason.USER_IDLE_TIMEOUT
    record_end_reason(None, EndReason.CUSTOMER_HANGUP)  # no lead: nothing to do


@pytest.mark.parametrize(
    "meta, reason",
    [
        ({"call_ended_by": "customer"}, EndReason.CUSTOMER_HANGUP),
        ({"call_ended_by": "agent"}, EndReason.AGENT_ENDED),
        (
            {"call_ended_by": "agent", "call_end_reason": "order confirmed"},
            EndReason.GLOBAL_END,
        ),
        (
            {"call_ended_by": "system", "call_end_reason": "user_idle_timeout"},
            EndReason.USER_IDLE_TIMEOUT,
        ),
        ({"call_ended_by": "system"}, EndReason.PIPELINE_ERROR),
        ({"call_ended_by": "someone"}, None),
        ({}, None),
    ],
)
def test_end_reason_fallback_from_meta(meta, reason):
    assert end_reason_from_meta(meta) == reason


def test_ended_session_prefers_the_recorded_end_reason():
    lead = SimpleNamespace(
        end_reason=EndReason.USER_IDLE_TIMEOUT,
        metaData={"call_ended_by": "customer"},
        agent_outcome="confirmed",
        outcome_source="LLM",
    )
    outcome = ended_session_call_outcome(lead)
    assert outcome.end_reason is EndReason.USER_IDLE_TIMEOUT
    assert outcome.agent_outcome == "confirmed"


def test_ended_session_falls_back_to_meta():
    lead = SimpleNamespace(end_reason=None, metaData={"call_ended_by": "customer"})
    assert ended_session_call_outcome(lead).end_reason is EndReason.CUSTOMER_HANGUP


def test_ended_session_without_metadata():
    assert ended_session_call_outcome(SimpleNamespace()).end_reason is None


def test_completion_is_answered_unless_the_caller_says_otherwise():
    assert completed_call_outcome(None).connection_status is ConnectionStatus.ANSWERED
    kept = completed_call_outcome(
        CallOutcome(connection_status=ConnectionStatus.UNKNOWN)
    )
    assert kept.connection_status is ConnectionStatus.UNKNOWN


def test_transfer_overrides_the_ending_and_keeps_the_agents_word():
    outcome = completed_call_outcome(
        CallOutcome(end_reason=EndReason.AGENT_ENDED, agent_outcome="RESOLVED"),
        is_transfer=True,
    )
    assert outcome.end_reason is EndReason.TRANSFERRED
    assert outcome.agent_outcome == "RESOLVED"
    assert legacy_outcome(outcome) == "TRANSFERRED"
