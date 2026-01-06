"""
Daily transport endpoint for Breeze Buddy agent.

This module provides endpoints for web/mobile clients to interact with the agent
via Daily.co rooms instead of telephony.

Endpoints:
- POST   /connect              - Create Daily room and start agent
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import BreezeBuddyDailyConnectRequest, UserInfo

from .handlers import breeze_buddy_daily_connect_handler

router = APIRouter()


@router.post("/connect")
async def breeze_buddy_daily_connect(
    request: BreezeBuddyDailyConnectRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Dict[str, Any]:
    """
    Connect to Breeze Buddy agent via Daily transport for web/mobile clients.

    This endpoint creates a new Daily room on-demand, starts the agent,
    and returns room credentials to the client.

    Args:
        request: BreezeBuddyDailyConnectRequest containing lead_id

    Returns:
        Dict with room_url, token, session_id, and lead_id

    Example:
        POST /api/agent/voice/breeze-buddy/connect
        {
            "lead_id": "uuid-of-lead-from-lead_call_tracker"
        }
    """
    return await breeze_buddy_daily_connect_handler(request, current_user)
