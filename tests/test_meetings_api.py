"""Meetings API handlers: tenant scope, 404s, and resend rules."""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.routers.breeze_buddy.meetings import handlers as h
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.meetings import (
    DeliveryState,
    Meeting,
    MeetingState,
)

NOW = datetime.now(timezone.utc)


def meeting(state=MeetingState.FINISHED) -> Meeting:
    return Meeting(
        id="mtg_1",
        reseller_id="breeze",
        merchant_id="NOA",
        lead_id="lead_1",
        state=state,
        started_at=NOW,
        ended_at=None,
        duration_s=10,
        end_reason=None,
        last_heartbeat_at=NOW,
        stt_provider="soniox",
        delivery_state=DeliveryState.PENDING,
        delivery_attempts=0,
        delivery_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


def user(merchants) -> UserInfo:
    return UserInfo(
        id="u",
        username="u",
        role=UserRole.RESELLER,
        reseller_ids=["breeze"],
        merchant_ids=merchants,
    )


@pytest.fixture
def db(monkeypatch):
    store = {"meeting": meeting(), "segments": []}

    async def get_meeting(meeting_id):
        return store["meeting"] if meeting_id == "mtg_1" else None

    async def get_segments(meeting_id):
        return store["segments"]

    monkeypatch.setattr(h.meetings_db, "get_meeting", get_meeting)
    monkeypatch.setattr(h.meetings_db, "get_segments", get_segments)
    return store


@pytest.mark.asyncio
async def test_read_is_scoped_to_the_meetings_merchant(db):
    assert (await h.get_meeting_handler("mtg_1", user(["NOA"]))).id == "mtg_1"
    with pytest.raises(HTTPException) as e:
        await h.get_meeting_handler("mtg_1", user(["other"]))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await h.get_meeting_handler("nope", user(["NOA"]))
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_transcript_bundles_meeting_and_lines(db):
    t = await h.get_transcript_handler("mtg_1", user(["*"]))
    assert t.meeting.id == "mtg_1" and t.segments == []


@pytest.mark.asyncio
async def test_resend_refuses_a_live_recording(db):
    db["meeting"] = meeting(state=MeetingState.RECORDING)
    with pytest.raises(HTTPException) as e:
        await h.resend_handler("mtg_1", user(["NOA"]))
    assert e.value.status_code == 409
