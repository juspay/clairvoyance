"""
Handler functions for Daily transport endpoints.
"""

from typing import Any, Dict

from fastapi import HTTPException

from app.ai.voice.agents.breeze_buddy.services.daily import start_daily_session
from app.core.logger import logger
from app.database.accessor import get_lead_by_id
from app.schemas import (
    BreezeBuddyDailyConnectRequest,
    ExecutionMode,
    LeadCallStatus,
    UserInfo,
)


async def breeze_buddy_daily_connect_handler(
    request: BreezeBuddyDailyConnectRequest,
    current_user: UserInfo,
) -> Dict[str, Any]:
    """
    Handler for creating Daily room and starting Breeze Buddy agent.

    Args:
        request: BreezeBuddyDailyConnectRequest containing lead_id
        current_user: Authenticated user info

    Returns:
        Dict with room_url, token, session_id, and lead_id
    """
    logger.info(
        f"Received Breeze Buddy Daily connect request: {request.model_dump_json(exclude_none=True)}"
    )

    try:
        # 1. Validate lead exists
        lead = await get_lead_by_id(request.lead_id)
        if not lead:
            raise HTTPException(
                status_code=404,
                detail=f"Lead not found with id: {request.lead_id}",
            )

        logger.info(
            f"Found lead for Daily session: {lead.id}, template: {lead.template}, "
            f"status: {lead.status}, execution_mode: {lead.execution_mode}"
        )

        # 2. Validate execution_mode is DAILY or DAILY_TEST
        if lead.execution_mode not in (ExecutionMode.DAILY, ExecutionMode.DAILY_TEST):
            raise HTTPException(
                status_code=400,
                detail=f"Lead is not a Daily lead. execution_mode: {lead.execution_mode}",
            )

        # 3. Validate status is BACKLOG (not already processed)
        if lead.status != LeadCallStatus.BACKLOG:
            raise HTTPException(
                status_code=400,
                detail=f"Lead already processed. status: {lead.status}",
            )

        # 4. Start Daily session (creates room, tokens, and starts bot)
        return await start_daily_session(request.lead_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create Breeze Buddy Daily session: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create Breeze Buddy Daily session: {str(e)}",
        )
