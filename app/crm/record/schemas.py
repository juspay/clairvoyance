"""Leaf shapes for the record module (module rules §1). Imports nothing
internal — db/decoder.py is the only place a row becomes one of these.

JourneyCard's field set is canon's crm.journey_event 12-column contract,
not ours — see app/database/migrations/052_create_crm_journey_view.sql.
Columns the call arm has no data for (handled_by, transcript_ref) come
back None; they stay real fields so future arms (chat/message/consent/
commerce) populate the same shape instead of the schema growing per arm.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class JourneyCard(BaseModel):
    id: str
    merchant_id: str
    customer_id: UUID
    channel: str
    direction: Optional[str] = None
    handled_by: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    outcome: Optional[str] = None
    recording_ref: Optional[str] = None
    transcript_ref: Optional[str] = None
    source_kind: str
