"""Accessors for meeting recording: one query builder per function, no decisions.

Every function logs and re-raises on failure so the caller (the recorder
session) decides what a failed write means for the meeting.
"""

from typing import List, Optional, Sequence

from app.core.logger import logger
from app.database import db_connection
from app.database.decoder.breeze_buddy.meetings import decode_meeting, decode_segment
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.meetings import (
    append_segments_query,
    create_meeting_query,
    finish_meeting_query,
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
    Meeting,
    MeetingEndReason,
    MeetingSegment,
    MeetingState,
    SegmentLine,
)


async def create_meeting(
    meeting_id: str,
    reseller_id: str,
    merchant_id: str,
    lead_id: str,
    stt_provider: str,
) -> Meeting:
    """Open a RECORDING meeting and return it."""
    query, values = create_meeting_query(
        meeting_id, reseller_id, merchant_id, lead_id, stt_provider
    )
    try:
        rows = await run_parameterized_query(query, values)
        return decode_meeting(rows[0])
    except Exception as e:
        logger.error(f"Error creating meeting {meeting_id} for lead {lead_id}: {e}")
        raise


async def get_meeting_by_id(meeting_id: str, merchant_id: str) -> Optional[Meeting]:
    """One meeting, merchant-scoped; None when absent or another merchant's."""
    query, values = get_meeting_by_id_query(meeting_id, merchant_id)
    try:
        rows = await run_parameterized_query(query, values)
        return decode_meeting(rows[0]) if rows else None
    except Exception as e:
        logger.error(f"Error reading meeting {meeting_id}: {e}")
        raise


async def get_recording_meeting_by_lead(lead_id: str) -> Optional[Meeting]:
    """The meeting still RECORDING on this lead, if any."""
    query, values = get_recording_meeting_by_lead_query(lead_id)
    try:
        rows = await run_parameterized_query(query, values)
        return decode_meeting(rows[0]) if rows else None
    except Exception as e:
        logger.error(f"Error reading recording meeting for lead {lead_id}: {e}")
        raise


async def heartbeat_meeting(meeting_id: str) -> bool:
    """Refresh liveness. False means the meeting is no longer RECORDING."""
    query, values = heartbeat_meeting_query(meeting_id)
    try:
        rows = await run_parameterized_query(query, values)
        return bool(rows)
    except Exception as e:
        logger.error(f"Error heartbeating meeting {meeting_id}: {e}")
        raise


async def finish_meeting(
    meeting_id: str,
    end_reason: MeetingEndReason,
    final_state: MeetingState = MeetingState.FINISHED,
) -> Optional[Meeting]:
    """Close the meeting once. None means it was already closed."""
    query, values = finish_meeting_query(meeting_id, end_reason, final_state)
    try:
        rows = await run_parameterized_query(query, values)
        return decode_meeting(rows[0]) if rows else None
    except Exception as e:
        logger.error(f"Error finishing meeting {meeting_id} ({end_reason.value}): {e}")
        raise


async def set_delivery_state(
    meeting_id: str, delivery_state: DeliveryState, error: Optional[str] = None
) -> bool:
    """Record a delivery outcome. False means no such meeting."""
    query, values = set_delivery_state_query(meeting_id, delivery_state, error)
    try:
        rows = await run_parameterized_query(query, values)
        return bool(rows)
    except Exception as e:
        logger.error(f"Error setting delivery state on meeting {meeting_id}: {e}")
        raise


async def get_stale_recording_meetings(
    older_than_seconds: int, limit: int = 100
) -> List[Meeting]:
    """RECORDING meetings whose heartbeat is older than the grace window."""
    query, values = get_stale_recording_meetings_query(older_than_seconds, limit)
    try:
        rows = await run_parameterized_query(query, values)
        return [decode_meeting(r) for r in rows]
    except Exception as e:
        logger.error(f"Error reading stale meetings: {e}")
        raise


async def append_segments(
    meeting_id: str, start_seq: int, lines: Sequence[SegmentLine]
) -> int:
    """Insert a batch of lines numbered from start_seq; returns rows inserted.

    A retried batch inserts 0 (ON CONFLICT DO NOTHING) and that is success.
    """
    if not lines:
        return 0
    query, values = append_segments_query(meeting_id, start_seq, lines)
    try:
        async with db_connection() as conn:
            status = await conn.execute(query, *values)
        # asyncpg returns "INSERT 0 <n>"
        return int(status.rsplit(" ", 1)[-1])
    except Exception as e:
        logger.error(
            f"Error appending {len(lines)} segment(s) to meeting {meeting_id} "
            f"from seq {start_seq}: {e}"
        )
        raise


async def get_segments(meeting_id: str) -> List[MeetingSegment]:
    """Every line of one meeting, ordered by seq."""
    query, values = get_segments_query(meeting_id)
    try:
        rows = await run_parameterized_query(query, values)
        return [decode_segment(r) for r in rows]
    except Exception as e:
        logger.error(f"Error reading segments of meeting {meeting_id}: {e}")
        raise


async def get_last_seq(meeting_id: str) -> int:
    """Highest stored seq, or -1 when the meeting has no lines yet."""
    query, values = get_last_seq_query(meeting_id)
    try:
        rows = await run_parameterized_query(query, values)
        return int(rows[0]["last_seq"])
    except Exception as e:
        logger.error(f"Error reading last seq of meeting {meeting_id}: {e}")
        raise
