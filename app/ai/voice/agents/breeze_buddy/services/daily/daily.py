"""
Daily transport service for Breeze Buddy.

This module handles Daily (web-based) voice session infrastructure:
- Room creation and token management
- Starting the bot for Daily sessions
- Call completion handling for Daily mode
"""

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from pipecat.runner.types import DailyRunnerArguments
from pipecat.transports.daily.utils import (
    DailyMeetingTokenParams,
    DailyMeetingTokenProperties,
    DailyRESTHelper,
    DailyRoomParams,
    DailyRoomProperties,
)

from app.ai.voice.agents.breeze_buddy.agent import daily_bot
from app.core.config.static import (
    BREEZE_BUDDY_DAILY_API_KEY,
    BREEZE_BUDDY_DAILY_API_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    update_lead_call_completion_details,
    update_lead_call_id_by_id,
    update_lead_call_recording_url,
)
from app.schemas import LeadCallStatus, LeadCallTracker


async def daily_completion_function(
    call_id: str,
    outcome: Optional[str] = None,
    call_end_time: Optional[datetime] = None,
    meta_data: Optional[dict] = None,
) -> Optional[LeadCallTracker]:
    """Completion function for Daily mode - updates lead status to FINISHED.

    For Daily mode, the call_id is actually the lead_id since Daily doesn't have
    a traditional call_sid like telephony providers.
    """
    logger.info(f"Daily completion: updating lead {call_id} to FINISHED")
    return await update_lead_call_completion_details(
        id=call_id,
        status=LeadCallStatus.FINISHED,
        outcome=outcome,
        meta_data=meta_data,
        call_end_time=call_end_time,
    )


async def start_daily_session(
    lead_id: str, *, enable_recording: bool = True
) -> Dict[str, Any]:
    """Create a Daily room and start the bot for a lead.

    This function handles the infrastructure setup for Daily sessions:
    1. Generates a unique session ID
    2. Creates a Daily room with appropriate settings
    3. Generates tokens for both user and bot
    4. Starts the bot in background

    Args:
        lead_id: The ID of the lead to start the session for
        enable_recording: When True (default — preserves existing
            telephony / demo behaviour), enable Daily cloud recording
            and set the recording_url sentinel so the dashboard player
            renders. The unified widget router (CHAT_MODE.md §14)
            passes False to skip recording entirely for widget voice
            attachments — the lead is reused across attachments and a
            single recording_url field cannot represent N
            recordings, so we drop the feature for widget v1.

    Returns:
        Dict containing room_url, token (for user), session_id, and lead_id
    """
    # Generate session ID
    session_id = str(uuid.uuid4())

    # Create Daily room on-demand
    async with create_aiohttp_session() as aiohttp_session:
        daily_rest = DailyRESTHelper(
            daily_api_key=BREEZE_BUDDY_DAILY_API_KEY,
            daily_api_url=BREEZE_BUDDY_DAILY_API_URL,
            aiohttp_session=aiohttp_session,
        )

        # Create room with params. Cloud recording is opt-in per call so
        # widget voice attachments can skip it (no per-call recording
        # surface — the lead is reused across attachments).
        room_properties_kwargs: Dict[str, Any] = {
            "exp": time.time() + 3600,  # 1 hour expiry
            "eject_at_room_exp": True,
        }
        if enable_recording:
            room_properties_kwargs["enable_recording"] = "cloud"
        room_params = DailyRoomParams(
            properties=DailyRoomProperties(**room_properties_kwargs)
        )
        room = await daily_rest.create_room(room_params)
        room_url = room.url

        # Create tokens
        user_token = await daily_rest.get_token(room_url)
        if enable_recording:
            bot_token = await daily_rest.get_token(
                room_url,
                expiry_time=3600,
                params=DailyMeetingTokenParams(
                    properties=DailyMeetingTokenProperties(
                        start_cloud_recording=True,
                    )
                ),
            )
        else:
            bot_token = await daily_rest.get_token(room_url, expiry_time=3600)

    # Store room name as call_id for on-demand recording retrieval.
    # Always set call_id (it's the call SID equivalent for Daily mode
    # and feeds end_conversation lookups), but only set the
    # recording_url sentinel when recording is actually enabled — a
    # widget call with no recording shouldn't show a player.
    updated_lead = await update_lead_call_id_by_id(lead_id, room.name)
    if not updated_lead:
        logger.warning(
            f"Failed to set call_id for lead {lead_id} — completion lookup may not work"
        )
    elif enable_recording:
        # Sentinel recording_url so frontend shows the recording player
        # (depends on call_id being set above).
        recording_lead = await update_lead_call_recording_url(room.name, "daily")
        if not recording_lead:
            logger.warning(
                f"Failed to set recording_url for lead {lead_id} — frontend may not show recording player"
            )
    else:
        # Clear any stale sentinel from a prior recorded attachment on this
        # same lead. Widget voice reuses leads across attachments, so a
        # previous run that set ``recording_url="daily"`` would otherwise
        # leave a player visible on this non-recorded session.
        cleared_lead = await update_lead_call_recording_url(room.name, "")
        if not cleared_lead:
            logger.warning(
                f"Failed to clear stale recording_url for lead {lead_id} — "
                "frontend may still show a stale recording player"
            )

    logger.info(
        f"Created Daily room for Breeze Buddy session {session_id}: {room_url} "
        f"(recording={'on' if enable_recording else 'off'})"
    )

    # Prepare runner arguments for Daily transport
    runner_args = DailyRunnerArguments(
        room_url=room_url,
        token=bot_token,
        body={
            "lead_id": lead_id,
            "session_id": session_id,
        },
    )

    # Create aiohttp session for the bot's lifecycle (for global HTTP functions)
    # Not using async with here because bot runs in background task
    bot_aiohttp_session = create_aiohttp_session()

    # Start the bot in background with the completion function and aiohttp session
    asyncio.create_task(
        daily_bot(runner_args, daily_completion_function, bot_aiohttp_session)
    )

    logger.info(
        f"Successfully started Breeze Buddy Daily bot for lead_id: {lead_id}, session: {session_id}"
    )

    # Return room credentials to caller
    return {
        "room_url": room_url,
        "token": user_token,
        "session_id": session_id,
        "lead_id": lead_id,
    }
