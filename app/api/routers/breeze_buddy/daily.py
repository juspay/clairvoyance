"""
Daily transport endpoint for Breeze Buddy agent.

This endpoint allows web/mobile clients to interact with the agent
via Daily.co rooms instead of telephony.
"""

import asyncio
import uuid
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pipecat.runner.types import DailyRunnerArguments

from app.ai.voice.agents.breeze_buddy.agent import bot
from app.core.config.static import DAILY_API_KEY, DAILY_API_URL
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.schemas import BreezeBuddyDailyConnectRequest

router = APIRouter()


@router.post("/connect")
async def breeze_buddy_daily_connect(
    request: BreezeBuddyDailyConnectRequest,
) -> Dict[str, Any]:
    """
    Connect to Breeze Buddy agent via Daily transport for web/mobile clients.

    This endpoint creates a new Daily room on-demand, starts the agent,
    and returns room credentials to the client.

    Args:
        request: BreezeBuddyDailyConnectRequest containing call_sid

    Returns:
        Dict with room_url, token, session_id, and call_sid

    Example:
        POST /api/agent/voice/breeze-buddy/connect
        {
            "call_sid": "unique-call-identifier"
        }
    """
    logger.info(
        f"Received Breeze Buddy Daily connect request: {request.model_dump_json(exclude_none=True)}"
    )

    try:
        # Import Daily REST helper
        from pipecat.transports.daily.utils import DailyRESTHelper

        # 1. Generate session ID and call_sid
        session_id = str(uuid.uuid4())
        call_sid = request.call_sid or f"daily-{session_id}"

        # 2. Create Daily room on-demand
        async with create_aiohttp_session() as aiohttp_session:
            daily_rest = DailyRESTHelper(
                daily_api_key=DAILY_API_KEY,
                daily_api_url=DAILY_API_URL,
                aiohttp_session=aiohttp_session,
            )

            # Create room
            room = await daily_rest.create_room()
            room_url = room.url

            # Create tokens
            user_token = await daily_rest.get_token(room_url)
            bot_token = await daily_rest.get_token(room_url, expiry_time=3600)

        logger.info(
            f"Created Daily room for Breeze Buddy session {session_id}: {room_url}"
        )

        # 3. Prepare runner arguments for Daily transport
        runner_args = DailyRunnerArguments(
            room_url=room_url,
            token=bot_token,
            body={
                "call_sid": call_sid,
                "session_id": session_id,
            },
        )

        # 4. Start the bot in background
        asyncio.create_task(bot(runner_args))

        logger.info(
            f"Successfully started Breeze Buddy Daily bot for call_sid: {call_sid}, session: {session_id}"
        )

        # 5. Return room credentials to client
        return {
            "room_url": room_url,
            "token": user_token,
            "session_id": session_id,
            "call_sid": call_sid,
        }

    except Exception as e:
        logger.error(f"Failed to create Breeze Buddy Daily session: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create Breeze Buddy Daily session: {str(e)}",
        )
