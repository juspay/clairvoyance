"""The one-off backfill's classifier (scripts/backfill_call_outcomes.py).

Each case is a row as a real write path leaves it: its legacy word plus the
meta_data that path writes. The classifier must say only what history can
still prove.
"""

import pytest

import scripts.backfill_call_outcomes as backfill
from app.ai.voice.agents.breeze_buddy.services.call_limiter import CALL_LIMIT_OUTCOME
from app.schemas.breeze_buddy.outcomes import LEGACY_NOT_DIALED_REASONS

TRANSCRIPT = {"transcription": [{"role": "user", "content": "hello"}]}


def values(outcome, meta=None):
    got = backfill.column_values(outcome, meta)
    return {k: v for k, v in got.items() if v is not None}


@pytest.mark.parametrize(
    "outcome, reason",
    [
        ("PRECHECK_FAILED", "PRECHECK_FAILED"),
        ("BLACKLISTED", "BLACKLISTED"),
        ("NUMBER_UNAVAILABLE", "NUMBER_UNAVAILABLE"),
        ("INVALID_PHONE", "INVALID_PHONE"),
        ("NO_CONFIG", "NO_CONFIG"),
        ("CALL_LIMIT_REACHED", "CALL_LIMIT"),
        ("ABORT", "ABORTED"),
        ("ABORTED", "ABORTED"),
    ],
)
def test_dispatcher_refusals_are_not_dialed(outcome, reason):
    assert values(outcome, {"reason": "Dispatcher: x"}) == {
        "connection_status": "NOT_DIALED",
        "connection_reason": reason,
    }


@pytest.mark.parametrize(
    "outcome, reason",
    [
        ("BLOCKED_REJECT", "BLOCKED"),
        ("BLOCKED_REDIRECT", "BLOCKED"),
        ("CAPACITY_REJECTED", "CAPACITY"),
    ],
)
def test_inbound_refusals_are_rejected(outcome, reason):
    assert values(outcome, {"block_reason": "x"}) == {
        "connection_status": "REJECTED",
        "connection_reason": reason,
    }


def test_carrier_no_answer_has_empty_meta():
    """handle_unanswered_calls writes meta_data={}; busy / failed / no-answer
    can no longer be told apart."""
    assert values("NO_ANSWER", {}) == {"connection_status": "NO_ANSWER"}
    assert values("NO_ANSWER", None) == {"connection_status": "NO_ANSWER"}


def test_agent_written_no_answer_is_an_answered_call():
    meta = {**TRANSCRIPT, "outcome": {}, "call_ended_by": "agent"}
    assert values("NO_ANSWER", meta) == {
        "connection_status": "ANSWERED",
        "end_reason": "AGENT_ENDED",
    }


def test_idle_timeout_busy_wins_over_the_hook_trace():
    """The idle timeout overwrites the agent's word, even after a hook ran."""
    meta = {
        "outcome": {"reason": "x"},
        "call_ended_by": "system",
        "call_end_reason": "user_idle_timeout",
    }
    assert values("BUSY", meta) == {
        "connection_status": "ANSWERED",
        "end_reason": "IDLE_TIMEOUT",
    }


def test_busy_with_the_hook_trace_is_the_agents_word():
    meta = {**TRANSCRIPT, "outcome": {}, "call_ended_by": "agent"}
    assert values("BUSY", meta) == {
        "connection_status": "ANSWERED",
        "end_reason": "AGENT_ENDED",
        "agent_outcome": "BUSY",
        "outcome_source": "LLM",
    }


def test_busy_fallback_has_no_agent_outcome():
    """Disconnect / end_conversation_global / IVR incomplete never write
    meta_data.outcome."""
    assert values("BUSY", {**TRANSCRIPT, "call_ended_by": "customer"}) == {
        "connection_status": "ANSWERED",
        "end_reason": "CUSTOMER_HANGUP",
    }


@pytest.mark.parametrize(
    "meta, expected",
    [
        (
            {"cleanup": "completed_no_pipeline"},
            {"connection_status": "ANSWERED", "provider_status": "completed"},
        ),
        ({"cleanup": "stuck_processing_timeout"}, {"connection_status": "UNKNOWN"}),
        (
            {"errors": [{"error": "boom"}], "call_ended_by": "system"},
            {"connection_status": "ANSWERED", "end_reason": "PIPELINE_ERROR"},
        ),
        ({}, {"connection_status": "UNKNOWN"}),
    ],
)
def test_unknown_says_only_what_its_writer_proves(meta, expected):
    assert values("UNKNOWN", meta) == expected


@pytest.mark.parametrize(
    "outcome, end_reason",
    [
        ("EARLY_HANGUP", "EARLY_HANGUP"),
        ("IVR_ERROR", "IVR_ERROR"),
        ("IVR_LOOP_GUARD", "IVR_ERROR"),
        ("IVR_NODE_MISSING", "IVR_ERROR"),
        ("TRANSFERRED", "TRANSFERRED"),
        ("ended_by_widget", "CUSTOMER_HANGUP"),
    ],
)
def test_system_endings_are_end_reasons_not_agent_outcomes(outcome, end_reason):
    assert values(outcome, {"call_ended_by": "agent"}) == {
        "connection_status": "ANSWERED",
        "end_reason": end_reason,
    }


def test_agent_word_is_upper_cased_with_its_source():
    assert values("confirmed", {**TRANSCRIPT, "call_ended_by": "agent"}) == {
        "connection_status": "ANSWERED",
        "end_reason": "AGENT_ENDED",
        "agent_outcome": "CONFIRMED",
        "outcome_source": "LLM",
    }


def test_observer_and_ivr_sources_are_recovered_from_meta():
    observer = values("VOICEMAIL", {"observer_triggered": "voicemail_detector"})
    assert observer["outcome_source"] == "OBSERVER"
    ivr = {"node_traversal": [{"node_name": "main", "dtmf_inputs": [{"digit": "1"}]}]}
    assert values("CONFIRMED", ivr)["outcome_source"] == "IVR"


def test_finished_without_any_outcome():
    assert values(None, {**TRANSCRIPT, "call_ended_by": "customer"}) == {
        "connection_status": "ANSWERED",
        "end_reason": "CUSTOMER_HANGUP",
    }
    assert values(None, {}) == {"connection_status": "UNKNOWN"}


def test_every_row_gets_a_connection_status():
    """The rerun resumes on connection_status IS NULL, so no classification
    may leave it empty."""
    for outcome in ["BUSY", "NO_ANSWER", "UNKNOWN", "SOMETHING", None, ""]:
        assert backfill.column_values(outcome, {})["connection_status"]


def test_vocabulary_call_limit_word_matches_the_limiter():
    """The vocabulary spells CALL_LIMIT_REACHED rather than importing the
    service it describes; this pins the two equal."""
    assert CALL_LIMIT_OUTCOME in LEGACY_NOT_DIALED_REASONS


def test_meta_parser_tolerates_what_asyncpg_returns():
    assert backfill._meta('{"a": 1}') == {"a": 1}
    assert backfill._meta(None) == {}
    assert backfill._meta("not json") == {}
    assert backfill._meta("[1, 2]") == {}
