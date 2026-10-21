"""Meeting recording storage: query builders are pure and guard every transition."""

import pytest

from app.database.decoder.breeze_buddy.meetings import decode_meeting, decode_segment
from app.database.queries.breeze_buddy.meetings import (
    append_segments_query,
    create_meeting_query,
    get_last_seq_query,
    get_meeting_by_id_query,
    get_recording_meeting_by_lead_query,
    get_segments_query,
    get_stale_recording_meetings_query,
    heartbeat_meeting_query,
    set_delivery_state_query,
)
from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    MeetingState,
    SegmentLine,
)


def test_create_opens_in_recording_state() -> None:
    sql, values = create_meeting_query("mtg_1", "breeze", "NOA", "lead_1", "soniox")
    assert "INSERT INTO meeting" in sql and "RETURNING" in sql
    assert values == ["mtg_1", "breeze", "NOA", "lead_1", "RECORDING", "soniox"]


def test_read_is_merchant_scoped() -> None:
    sql, values = get_meeting_by_id_query("mtg_1", "NOA")
    assert "merchant_id = $2" in sql and values == ["mtg_1", "NOA"]


def test_recording_lookup_filters_on_state() -> None:
    sql, values = get_recording_meeting_by_lead_query("lead_1")
    assert "state = $2" in sql and values == ["lead_1", "RECORDING"]


def test_heartbeat_touches_only_a_recording_meeting() -> None:
    sql, values = heartbeat_meeting_query("mtg_1")
    assert "last_heartbeat_at = now()" in sql
    assert "WHERE id = $1 AND state = $2" in sql and values[1] == "RECORDING"


def test_delivery_state_increments_attempts() -> None:
    sql, values = set_delivery_state_query(
        "mtg_1", DeliveryState.FAILED, "503 from xyne"
    )
    assert "delivery_attempts = delivery_attempts + 1" in sql
    assert values == ["mtg_1", "FAILED", "503 from xyne"]


def test_stale_scan_uses_the_partial_index_predicate() -> None:
    sql, values = get_stale_recording_meetings_query(300, limit=50)
    assert "state = $1" in sql and "make_interval(secs => $2)" in sql
    assert values == ["RECORDING", 300, 50]


def test_append_numbers_lines_from_start_seq_and_ignores_conflicts() -> None:
    lines = [
        SegmentLine(speaker="1", text="Let's start.", start_ms=4000, end_ms=9000),
        SegmentLine(speaker="2", text="Sure.", start_ms=10000, end_ms=15000),
    ]
    sql, values = append_segments_query("mtg_1", 7, lines)
    assert "ON CONFLICT (meeting_id, seq) DO NOTHING" in sql
    assert "($1, $2, $3, $4, $5, $6)" in sql and "($1, $7, $8, $9, $10, $11)" in sql
    assert values == [
        "mtg_1",
        7,
        "1",
        "Let's start.",
        4000,
        9000,
        8,
        "2",
        "Sure.",
        10000,
        15000,
    ]


def test_append_refuses_an_empty_batch() -> None:
    with pytest.raises(ValueError):
        append_segments_query("mtg_1", 0, [])


def test_segments_read_in_seq_order_and_last_seq_defaults_to_minus_one() -> None:
    sql, _ = get_segments_query("mtg_1")
    assert "ORDER BY seq" in sql
    sql, _ = get_last_seq_query("mtg_1")
    assert "COALESCE(MAX(seq), -1)" in sql


def test_decoders_translate_vocabulary_from_text() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    meeting = decode_meeting(
        {
            "id": "mtg_1",
            "reseller_id": "breeze",
            "merchant_id": "NOA",
            "lead_id": "lead_1",
            "state": "FINISHED",
            "started_at": now,
            "ended_at": now,
            "duration_s": 2479,
            "end_reason": "voice",
            "last_heartbeat_at": now,
            "stt_provider": "soniox",
            "delivery_state": "SENT",
            "delivery_attempts": 1,
            "delivery_error": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    assert meeting.state is MeetingState.FINISHED
    assert meeting.end_reason is not None and meeting.end_reason.value == "voice"

    segment = decode_segment(
        {
            "meeting_id": "mtg_1",
            "seq": 0,
            "speaker": None,
            "text": "hi",
            "start_ms": 0,
            "end_ms": 400,
        }
    )
    assert segment.speaker is None and segment.seq == 0


def test_finish_closes_only_a_recording_meeting_and_stores_duration() -> None:
    from app.database.queries.breeze_buddy.meetings import finish_meeting_query
    from app.schemas.breeze_buddy.meetings import MeetingEndReason

    sql, values = finish_meeting_query("mtg_1", MeetingEndReason.VOICE)
    assert "WHERE id = $1 AND state = $4" in sql
    assert "EXTRACT(EPOCH FROM (now() - started_at))::integer" in sql
    assert "RETURNING" in sql
    assert values == ["mtg_1", "FINISHED", "voice", "RECORDING"]

    sql, values = finish_meeting_query(
        "mtg_1", MeetingEndReason.STALE, final_state=MeetingState.INTERRUPTED
    )
    assert values == ["mtg_1", "INTERRUPTED", "stale", "RECORDING"]
