"""Pydantic shapes for meeting recording (meeting + meeting_segment).

Leaf models: nothing here imports application code. The state vocabularies
live in these enums; the DB only checks their format.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class MeetingState(str, Enum):
    """Lifecycle of one recording."""

    RECORDING = "RECORDING"
    FINISHED = "FINISHED"
    INTERRUPTED = "INTERRUPTED"


class DeliveryState(str, Enum):
    """Whether the finished transcript reached its destination."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class MeetingEndReason(str, Enum):
    """Why a recording stopped."""

    VOICE = "voice"
    MAX_DURATION = "max_duration"
    STALE = "stale"


class SegmentLine(BaseModel):
    """One transcript line as the pipeline produces it, before it has a seq."""

    speaker: Optional[str] = None
    text: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class MeetingSegment(SegmentLine):
    """A stored transcript line."""

    meeting_id: str
    seq: int = Field(ge=0)


class Meeting(BaseModel):
    """One recording."""

    id: str
    reseller_id: str
    merchant_id: str
    lead_id: str
    state: MeetingState
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_s: Optional[int] = None
    end_reason: Optional[MeetingEndReason] = None
    last_heartbeat_at: datetime
    stt_provider: str
    delivery_state: DeliveryState
    delivery_attempts: int
    delivery_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class MeetingTranscript(BaseModel):
    """What delivery and the API hand out: the meeting plus its ordered lines."""

    meeting: Meeting
    segments: List[MeetingSegment]
