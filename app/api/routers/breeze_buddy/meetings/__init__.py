"""Meeting recording API.

- GET  /meetings/{meeting_id}             - one meeting (state, timings, delivery)
- GET  /meetings/{meeting_id}/transcript  - the meeting plus its ordered lines
- POST /meetings/{meeting_id}/resend      - run the template's on_stop_hooks again
- GET  /leads/{lead_id}/meetings          - every meeting recorded on one connection

Tenant scope is the meeting row's reseller/merchant, checked like leads.
"""

from typing import List

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.meetings import Meeting, MeetingTranscript

from .handlers import (
    get_meeting_handler,
    get_transcript_handler,
    list_lead_meetings_handler,
    resend_handler,
)

router = APIRouter()


@router.get("/meetings/{meeting_id}", response_model=Meeting)
async def get_meeting(
    meeting_id: str, current_user: UserInfo = Depends(get_current_user_with_rbac)
):
    return await get_meeting_handler(meeting_id, current_user)


@router.get("/meetings/{meeting_id}/transcript", response_model=MeetingTranscript)
async def get_transcript(
    meeting_id: str, current_user: UserInfo = Depends(get_current_user_with_rbac)
):
    return await get_transcript_handler(meeting_id, current_user)


@router.post("/meetings/{meeting_id}/resend", response_model=Meeting)
async def resend(
    meeting_id: str, current_user: UserInfo = Depends(get_current_user_with_rbac)
):
    return await resend_handler(meeting_id, current_user)


@router.get("/leads/{lead_id}/meetings", response_model=List[Meeting])
async def list_lead_meetings(
    lead_id: str, current_user: UserInfo = Depends(get_current_user_with_rbac)
):
    return await list_lead_meetings_handler(lead_id, current_user)
