"""Row → model translation for meeting tables. Pure functions, no I/O."""

from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    Meeting,
    MeetingEndReason,
    MeetingSegment,
    MeetingState,
)


def decode_meeting(row) -> Meeting:
    """Build a Meeting from a meeting row."""
    end_reason = row.get("end_reason")
    return Meeting(
        id=row["id"],
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        lead_id=row["lead_id"],
        state=MeetingState(row["state"]),
        started_at=row["started_at"],
        ended_at=row.get("ended_at"),
        duration_s=row.get("duration_s"),
        end_reason=MeetingEndReason(end_reason) if end_reason else None,
        last_heartbeat_at=row["last_heartbeat_at"],
        stt_provider=row["stt_provider"],
        delivery_state=DeliveryState(row["delivery_state"]),
        delivery_attempts=row["delivery_attempts"],
        delivery_error=row.get("delivery_error"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_segment(row) -> MeetingSegment:
    """Build a MeetingSegment from a meeting_segment row."""
    return MeetingSegment(
        meeting_id=row["meeting_id"],
        seq=row["seq"],
        speaker=row.get("speaker"),
        text=row["text"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
    )
