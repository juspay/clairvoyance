"""Meeting API handlers: thin reads plus one re-delivery."""

from typing import List

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.agent.flow import load_template_config
from app.ai.voice.agents.breeze_buddy.recorder.delivery import deliver
from app.api.routers.breeze_buddy.leads.rbac import validate_lead_access
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import get_lead_by_id
from app.database.accessor.breeze_buddy import meetings as meetings_db
from app.schemas import UserInfo
from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    Meeting,
    MeetingState,
    MeetingTranscript,
)


async def _load_scoped(
    meeting_id: str, current_user: UserInfo, operation: str
) -> Meeting:
    meeting = await meetings_db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found"
        )
    validate_lead_access(
        current_user, meeting.reseller_id, meeting.merchant_id, operation
    )
    return meeting


async def get_meeting_handler(meeting_id: str, current_user: UserInfo) -> Meeting:
    return await _load_scoped(meeting_id, current_user, "read meeting")


async def get_transcript_handler(
    meeting_id: str, current_user: UserInfo
) -> MeetingTranscript:
    meeting = await _load_scoped(meeting_id, current_user, "read transcript")
    segments = await meetings_db.get_segments(meeting.id)
    return MeetingTranscript(meeting=meeting, segments=segments)


async def list_lead_meetings_handler(
    lead_id: str, current_user: UserInfo
) -> List[Meeting]:
    lead = await get_lead_by_id(lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found"
        )
    validate_lead_access(
        current_user, lead.reseller_id, lead.merchant_id, "list meetings"
    )
    return await meetings_db.get_meetings_by_lead(lead_id)


async def resend_handler(meeting_id: str, current_user: UserInfo) -> Meeting:
    """Run the template's on_stop_hooks again for a finished meeting."""
    meeting = await _load_scoped(meeting_id, current_user, "resend transcript")
    if meeting.state is MeetingState.RECORDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Meeting is still recording"
        )
    lead = await get_lead_by_id(meeting.lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found"
        )
    _template, configurations, _vars = await load_template_config(lead)
    config = getattr(configurations, "meeting_recording", None)
    hooks = config.on_stop_hooks if config else []
    if not hooks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The template has no meeting_recording.on_stop_hooks",
        )
    segments = await meetings_db.get_segments(meeting.id)
    async with create_aiohttp_session() as session:
        ok, error = await deliver(meeting, segments, hooks, session)
    await meetings_db.set_delivery_state(
        meeting.id, DeliveryState.SENT if ok else DeliveryState.FAILED, error
    )
    refreshed = await meetings_db.get_meeting(meeting.id)
    return refreshed or meeting
