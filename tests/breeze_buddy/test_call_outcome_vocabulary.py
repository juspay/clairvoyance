"""The call outcome facts and legacy_outcome() (app/schemas/breeze_buddy/outcomes.py).

The golden table below is the plan's scenario table (docs/CALL_OUTCOMES.md,
section 6): for every way a lead ends today, the facts its writers record and
the exact ``outcome`` word the old writers produce. legacy_outcome() must
reproduce every one byte-for-byte — that is what lets PR 2 replace the old
writers with it.
"""

from types import SimpleNamespace

import pytest

from app.schemas.breeze_buddy.outcomes import (
    AgentOutcomeSource,
    CallOutcome,
    PlatformReason,
    PlatformStatus,
    ProviderReason,
    ProviderStatus,
    SessionEndReason,
    agent_word,
    call_outcome_from_lead,
    completed_call_outcome,
    ended_session_call_outcome,
    hangup_cause_from_callback,
    initiated_call_outcome,
    is_web_session,
    legacy_outcome,
    not_initiated_call_outcome,
    provider_from_status,
    record_session_end_reason,
    session_end_reason_from_meta,
)

# ---------------------------------------------------------------------------
# legacy_outcome: the scenario table
# ---------------------------------------------------------------------------

P = PlatformReason
V = ProviderReason
E = SessionEndReason
INITIATED = PlatformStatus.INITIATED
NOT_INITIATED = PlatformStatus.NOT_INITIATED
ANSWERED = ProviderStatus.ANSWERED
NOT_ANSWERED = ProviderStatus.NOT_ANSWERED


def facts(
    platform=None,
    platform_reason=None,
    provider=None,
    provider_reason=None,
    end=None,
    word=None,
    source=None,
):
    return SimpleNamespace(
        platform_status=platform,
        platform_reason=platform_reason,
        provider_status=provider,
        provider_reason=provider_reason,
        session_end_reason=end,
        agent_outcome=word,
        agent_outcome_source=source,
    )


def refused(reason):
    return facts(NOT_INITIATED, reason)


def unanswered(reason):
    return facts(INITIATED, P.DIALED, NOT_ANSWERED, reason)


def answered(end=None, word=None, source=None):
    return facts(INITIATED, P.DIALED, ANSWERED, V.COMPLETED, end, word, source)


def web(end=None, word=None, source=None):
    return facts(INITIATED, P.WEB_SESSION, end=end, word=word, source=source)


SCENARIOS = [
    # not dialled / turned away: the reason is the word
    ("pre-check aborts", refused(P.PRECHECK_FAILED), "PRECHECK_FAILED"),
    ("blacklisted", refused(P.BLACKLISTED), "BLACKLISTED"),
    ("no free number", refused(P.NUMBER_UNAVAILABLE), "NUMBER_UNAVAILABLE"),
    ("invalid phone", refused(P.INVALID_PHONE), "INVALID_PHONE"),
    ("no config", refused(P.NO_CONFIG), "NO_CONFIG"),
    ("call limit", refused(P.CALL_LIMIT_REACHED), "CALL_LIMIT_REACHED"),
    ("abort: API/campaign/widget/demo", refused(P.ABORT), "ABORT"),
    ("abort: WooCommerce / CRM cap", refused(P.ABORTED), "ABORTED"),
    ("inbound blocked, reject", refused(P.BLOCKED_REJECT), "BLOCKED_REJECT"),
    ("inbound blocked, redirect", refused(P.BLOCKED_REDIRECT), "BLOCKED_REDIRECT"),
    ("inbound capacity", refused(P.CAPACITY_REJECTED), "CAPACITY_REJECTED"),
    # dialled, not answered: always NO_ANSWER
    ("no answer", unanswered(V.NO_ANSWER), "NO_ANSWER"),
    ("line busy", unanswered(V.BUSY), "NO_ANSWER"),
    ("call failed", unanswered(V.FAILED), "NO_ANSWER"),
    ("cancelled", unanswered(V.CANCELED), "NO_ANSWER"),
    ("plivo timeout", unanswered(V.TIMEOUT), "NO_ANSWER"),
    ("unmapped carrier status", unanswered(None), "NO_ANSWER"),
    # answered, the agent decided: its word, raw
    (
        "agent decides, flow ends",
        answered(E.AGENT_ENDED, "confirmed", "LLM"),
        "confirmed",
    ),
    (
        "agent decides, customer hangs up",
        answered(E.CUSTOMER_HANGUP, "confirmed", "LLM"),
        "confirmed",
    ),
    (
        "agent decides, LLM global end",
        answered(E.GLOBAL_END, "confirmed", "LLM"),
        "confirmed",
    ),
    (
        "agent decides, then transferred",
        answered(E.TRANSFERRED, "RESOLVED", "LLM"),
        "TRANSFERRED",
    ),
    (
        "agent decides, then user idle",
        answered(E.USER_IDLE_TIMEOUT, "confirmed", "LLM"),
        "BUSY",
    ),
    (
        "agent decides, then pipeline idle",
        answered(E.IDLE_TIMEOUT, "confirmed", "LLM"),
        "confirmed",
    ),
    (
        "agent: customer busy, call later",
        answered(E.AGENT_ENDED, "BUSY", "LLM"),
        "BUSY",
    ),
    (
        "agent says no answer after talking",
        answered(E.AGENT_ENDED, "NO_ANSWER", "LLM"),
        "NO_ANSWER",
    ),
    (
        "voicemail observer",
        answered(E.AGENT_ENDED, "VOICEMAIL", "OBSERVER"),
        "VOICEMAIL",
    ),
    (
        "agent decides, widget visitor ends",
        web(E.WIDGET_ENDED, "confirmed", "LLM"),
        "confirmed",
    ),
    # answered, no agent word: the ending's default
    ("no word, user idle", answered(E.USER_IDLE_TIMEOUT), "BUSY"),
    ("no word, pipeline idle", answered(E.IDLE_TIMEOUT), "BUSY"),
    ("no word, customer hangup", answered(E.CUSTOMER_HANGUP), "BUSY"),
    ("no word, LLM global end", answered(E.GLOBAL_END), "BUSY"),
    ("no word, flow end_conversation", answered(E.AGENT_ENDED), None),
    ("early hangup", answered(E.EARLY_HANGUP), "EARLY_HANGUP"),
    ("setup error", answered(E.PIPELINE_ERROR), "UNKNOWN"),
    (
        "setup error after an agent word",
        answered(E.PIPELINE_ERROR, "confirmed", "LLM"),
        "UNKNOWN",
    ),
    ("widget end, no word", web(E.WIDGET_ENDED), "ended_by_widget"),
    ("web session, visitor leaves", web(E.CUSTOMER_HANGUP), "BUSY"),
    # IVR
    ("IVR option chosen", answered(E.IVR_ENDED, "CONFIRM", "IVR"), "CONFIRM"),
    (
        "IVR no input, timeout word",
        answered(E.IVR_NO_INPUT, "NO_RESPONSE", "IVR"),
        "NO_RESPONSE",
    ),
    ("IVR END option, nothing chosen", answered(E.IVR_ENDED), "BUSY"),
    ("IVR no input, no timeout word", answered(E.IVR_NO_INPUT), "BUSY"),
    ("IVR hangup, nothing chosen", answered(E.CUSTOMER_HANGUP), "BUSY"),
    ("IVR setup error", answered(E.IVR_ERROR), "IVR_ERROR"),
    (
        "IVR loop guard after an option",
        answered(E.IVR_LOOP_GUARD, "CONFIRM", "IVR"),
        "IVR_LOOP_GUARD",
    ),
    ("IVR node missing", answered(E.IVR_NODE_MISSING), "IVR_NODE_MISSING"),
    (
        "IVR exception after an option",
        answered(E.IVR_EXCEPTION, "CONFIRM", "IVR"),
        "CONFIRM",
    ),
    ("IVR exception, nothing chosen", answered(E.IVR_EXCEPTION), "BUSY"),
    # safety nets
    ("carrier completed, no pipeline", answered(), "UNKNOWN"),
    (
        "reaper, pipeline ran with a word",
        answered(E.REAPED, "confirmed", "LLM"),
        "confirmed",
    ),
    (
        "reaper keeps an earlier ending",
        answered(E.USER_IDLE_TIMEOUT, "confirmed", "LLM"),
        "BUSY",
    ),
    (
        "reaper after a transfer",
        answered(E.TRANSFERRED, "RESOLVED", "LLM"),
        "TRANSFERRED",
    ),
    (
        "reaper, no word on the row",
        facts(INITIATED, P.DIALED, ProviderStatus.UNKNOWN),
        "UNKNOWN",
    ),
    ("reaper, web session with a word", web(E.REAPED, "confirmed", "LLM"), "confirmed"),
    ("reaper, web session, no word", web(E.REAPED), "UNKNOWN"),
    # chat: no platform or provider facts, the agent's word
    ("chat session", facts(word="confirmed", source="LLM"), "confirmed"),
    # set up, nothing else recorded yet
    ("call placed, still ringing", facts(INITIATED, P.DIALED), None),
    ("web session started", web(), None),
    ("nothing recorded", facts(), None),
]


@pytest.mark.parametrize(
    "name, lead, word", SCENARIOS, ids=[name for name, _, _ in SCENARIOS]
)
def test_legacy_outcome_reproduces_todays_word(name, lead, word):
    assert legacy_outcome(lead) == word


def test_legacy_outcome_reads_stored_strings_too():
    """A row read back from the database holds strings, not enum members."""
    lead = facts(
        "INITIATED", "DIALED", "ANSWERED", "COMPLETED", "USER_IDLE_TIMEOUT", "ok"
    )
    assert legacy_outcome(lead) == "BUSY"
    assert legacy_outcome(facts("NOT_INITIATED", "ABORT")) == "ABORT"
    assert legacy_outcome(facts("INITIATED", "DIALED", "NOT_ANSWERED")) == "NO_ANSWER"


def test_legacy_outcome_keeps_the_agents_casing():
    lead = answered(E.AGENT_ENDED, "Address Updated ")
    assert legacy_outcome(lead) == "Address Updated "


def test_only_a_refusal_reason_is_copied_as_the_word():
    """How a call was set up (DIALED / WEB_SESSION) is never an outcome."""
    assert legacy_outcome(facts(NOT_INITIATED, P.DIALED)) is None
    assert legacy_outcome(facts(NOT_INITIATED)) is None


def test_a_refusal_wins_over_any_later_fact():
    """An inbound call blocked by IVR selection after it was accepted."""
    lead = facts(NOT_INITIATED, P.BLOCKED_REDIRECT, ANSWERED, V.COMPLETED)
    assert legacy_outcome(lead) == "BLOCKED_REDIRECT"


def test_unknown_vocabulary_is_ignored_not_raised():
    lead = facts("SOMETHING_NEW", "WHATEVER", "NEW", "NEW", "NOT_AN_ENDING", "ok")
    assert legacy_outcome(lead) == "ok"


# ---------------------------------------------------------------------------
# carrier mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, status, reason",
    [
        ("no-answer", NOT_ANSWERED, V.NO_ANSWER),
        ("busy", NOT_ANSWERED, V.BUSY),
        ("failed", NOT_ANSWERED, V.FAILED),
        ("canceled", NOT_ANSWERED, V.CANCELED),
        ("cancelled", NOT_ANSWERED, V.CANCELED),
        ("cancel", NOT_ANSWERED, V.CANCELED),
        ("timeout", NOT_ANSWERED, V.TIMEOUT),
        ("completed", ANSWERED, V.COMPLETED),
        (" BUSY ", NOT_ANSWERED, V.BUSY),
    ],
)
def test_carrier_status_maps_to_one_spelling(raw, status, reason):
    """The legacy word is NO_ANSWER for every unanswered status; the provider
    reason keeps what the carrier actually said, in one spelling."""
    assert provider_from_status(raw) == (status, reason)


@pytest.mark.parametrize("raw", [None, "", "ringing", "in-progress"])
def test_unknown_or_missing_carrier_status_maps_to_nothing(raw):
    assert provider_from_status(raw) == (None, None)


def test_hangup_cause_prefers_the_named_cause():
    form = {"HangupCause": "16", "HangupCauseName": "NORMAL_CLEARING"}
    assert hangup_cause_from_callback(form) == "NORMAL_CLEARING"


def test_hangup_cause_falls_back_through_provider_keys():
    assert hangup_cause_from_callback({"SipResponseCode": 486}) == "486"
    assert hangup_cause_from_callback({"HangupCauseName": "", "ErrorCode": "3"}) == "3"
    assert hangup_cause_from_callback({}) is None


@pytest.mark.parametrize(
    "mode, web_session",
    [
        ("DAILY", True),
        ("DAILY_TEST", True),
        ("DAILY_STREAM", True),
        ("TELEPHONY", False),
        ("TELEPHONY_TEST", False),
        ("HOLD_TRANSFER", False),
        (None, False),
    ],
)
def test_web_sessions_are_the_daily_modes(mode, web_session):
    assert is_web_session(mode) is web_session
    assert is_web_session(SimpleNamespace(value=mode)) is web_session


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


def test_columns_name_only_what_the_write_carries():
    assert not_initiated_call_outcome(P.PRECHECK_FAILED).columns() == {
        "platform_status": "NOT_INITIATED",
        "platform_reason": "PRECHECK_FAILED",
    }
    assert CallOutcome().columns() == {}


def test_initiated_records_how_the_call_was_set_up():
    assert initiated_call_outcome().columns() == {
        "platform_status": "INITIATED",
        "platform_reason": "DIALED",
    }
    assert initiated_call_outcome(web_session=True).columns() == {
        "platform_status": "INITIATED",
        "platform_reason": "WEB_SESSION",
    }


def test_columns_keep_the_agents_word_raw():
    outcome = CallOutcome(
        agent_outcome="confirmed", agent_outcome_source=AgentOutcomeSource.LLM
    )
    assert outcome.columns() == {
        "agent_outcome": "confirmed",
        "agent_outcome_source": "LLM",
    }


def test_columns_clip_the_hangup_cause_to_the_column_width():
    columns = CallOutcome(provider_hangup_cause="y" * 150).columns()
    assert len(columns["provider_hangup_cause"]) == 100


def test_lead_values_are_read_tolerantly():
    lead = SimpleNamespace(
        platform_status="INITIATED",
        platform_reason="NOT_A_REASON",
        provider_status="ANSWERED",
        provider_reason="COMPLETED",
        session_end_reason=SessionEndReason.CUSTOMER_HANGUP,
        agent_outcome="confirmed",
        agent_outcome_source="LLM",
    )
    outcome = call_outcome_from_lead(lead)
    assert outcome.platform_status is PlatformStatus.INITIATED
    assert outcome.platform_reason is None
    assert outcome.provider_status is ProviderStatus.ANSWERED
    assert outcome.provider_reason is ProviderReason.COMPLETED
    assert outcome.session_end_reason is SessionEndReason.CUSTOMER_HANGUP
    assert outcome.agent_outcome == "confirmed"
    assert outcome.agent_outcome_source is AgentOutcomeSource.LLM


def test_lead_values_take_overrides():
    outcome = call_outcome_from_lead(
        SimpleNamespace(),
        session_end_reason=SessionEndReason.IVR_ERROR,
        agent_outcome="X",
    )
    assert outcome.session_end_reason is SessionEndReason.IVR_ERROR
    assert outcome.agent_outcome == "X"


def test_the_first_ending_wins():
    lead = SimpleNamespace(session_end_reason=None)
    record_session_end_reason(lead, SessionEndReason.USER_IDLE_TIMEOUT)
    record_session_end_reason(lead, SessionEndReason.CUSTOMER_HANGUP)
    assert lead.session_end_reason is SessionEndReason.USER_IDLE_TIMEOUT
    record_session_end_reason(None, SessionEndReason.CUSTOMER_HANGUP)  # no lead


@pytest.mark.parametrize(
    "meta, reason",
    [
        ({"call_ended_by": "customer"}, SessionEndReason.CUSTOMER_HANGUP),
        ({"call_ended_by": "agent"}, SessionEndReason.AGENT_ENDED),
        (
            {"call_ended_by": "agent", "call_end_reason": "order confirmed"},
            SessionEndReason.GLOBAL_END,
        ),
        (
            {"call_ended_by": "system", "call_end_reason": "user_idle_timeout"},
            SessionEndReason.USER_IDLE_TIMEOUT,
        ),
        # Pipeline idle is the only "system" ending that relies on this
        # fallback; end_conversation stores the bot's errors on every call, so
        # their presence says nothing about how it ended.
        ({"call_ended_by": "system"}, SessionEndReason.IDLE_TIMEOUT),
        (
            {"call_ended_by": "system", "errors": [{"error": "tts"}]},
            SessionEndReason.IDLE_TIMEOUT,
        ),
        ({"call_ended_by": "someone"}, None),
        ({}, None),
    ],
)
def test_session_end_reason_fallback_from_meta(meta, reason):
    assert session_end_reason_from_meta(meta) == reason


def test_pipeline_idle_fallback_keeps_the_agents_word():
    """The race the fallback exists for: a hook refresh dropped the recorded
    IDLE_TIMEOUT. The word must stay the agent's, as the legacy word does."""
    lead = SimpleNamespace(
        session_end_reason=None,
        metaData={"call_ended_by": "system", "errors": []},
        agent_outcome="confirmed",
        agent_outcome_source="LLM",
    )
    outcome = completed_call_outcome(ended_session_call_outcome(lead))
    assert outcome.session_end_reason is SessionEndReason.IDLE_TIMEOUT
    assert legacy_outcome(outcome) == "confirmed"


def test_ended_session_prefers_the_recorded_end_reason():
    lead = SimpleNamespace(
        session_end_reason=SessionEndReason.USER_IDLE_TIMEOUT,
        metaData={"call_ended_by": "customer"},
        agent_outcome="confirmed",
        agent_outcome_source="LLM",
    )
    outcome = ended_session_call_outcome(lead)
    assert outcome.session_end_reason is SessionEndReason.USER_IDLE_TIMEOUT
    assert outcome.agent_outcome == "confirmed"


def test_ended_session_falls_back_to_meta():
    lead = SimpleNamespace(
        session_end_reason=None, metaData={"call_ended_by": "customer"}
    )
    outcome = ended_session_call_outcome(lead)
    assert outcome.session_end_reason is SessionEndReason.CUSTOMER_HANGUP


def test_ended_session_without_metadata():
    assert ended_session_call_outcome(SimpleNamespace()).session_end_reason is None


def test_a_completed_phone_call_was_set_up_and_answered():
    outcome = completed_call_outcome(None)
    assert outcome.platform_status is PlatformStatus.INITIATED
    assert outcome.platform_reason is PlatformReason.DIALED
    assert outcome.provider_status is ProviderStatus.ANSWERED
    assert outcome.provider_reason is ProviderReason.COMPLETED


def test_a_completed_web_session_has_no_provider_facts():
    outcome = completed_call_outcome(None, web_session=True)
    assert outcome.platform_status is PlatformStatus.INITIATED
    assert outcome.platform_reason is PlatformReason.WEB_SESSION
    assert outcome.provider_status is None
    assert outcome.provider_reason is None


def test_completion_keeps_what_the_row_already_says():
    kept = completed_call_outcome(
        CallOutcome(
            platform_status=PlatformStatus.NOT_INITIATED,
            platform_reason=PlatformReason.ABORT,
            provider_status=ProviderStatus.UNKNOWN,
        )
    )
    assert kept.platform_status is PlatformStatus.NOT_INITIATED
    assert kept.platform_reason is PlatformReason.ABORT
    assert kept.provider_status is ProviderStatus.UNKNOWN
    assert kept.provider_reason is None


def test_transfer_overrides_the_ending_and_keeps_the_agents_word():
    outcome = completed_call_outcome(
        CallOutcome(
            session_end_reason=SessionEndReason.AGENT_ENDED,
            agent_outcome="RESOLVED",
        ),
        is_transfer=True,
    )
    assert outcome.session_end_reason is SessionEndReason.TRANSFERRED
    assert outcome.agent_outcome == "RESOLVED"
    assert legacy_outcome(outcome) == "TRANSFERRED"
