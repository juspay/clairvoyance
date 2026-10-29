"""
Handler functions for Daily transport endpoints.
"""

from typing import Any, Dict

from fastapi import HTTPException

from app.ai.voice.agents.breeze_buddy.services.daily.daily import start_daily_session
from app.api.routers.breeze_buddy.leads.rbac import validate_lead_read_access
from app.core.logger import logger
from app.database.accessor import get_lead_by_id, get_template_by_id
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
            # Same body as the access-denied 404 from validate_lead_read_access
            # below, so the two cannot be told apart to enumerate lead ids.
            raise HTTPException(status_code=404, detail="Lead not found")

        # RBAC: the caller may only connect to a lead in their own tenant scope.
        # Without this, any authenticated user could pass another tenant's
        # lead_id and receive a Daily room + join token (N-TEL-2). Raises 404 on
        # denial (not 403) to avoid leaking lead existence, matching the leads
        # router.
        validate_lead_read_access(current_user, lead, operation="connect")

        logger.info(
            f"Found lead for Daily session: {lead.id}, template: {lead.template}, "
            f"status: {lead.status}, execution_mode: {lead.execution_mode}"
        )

        # 2. Validate execution_mode is a Daily variant
        daily_modes = (
            ExecutionMode.DAILY,
            ExecutionMode.DAILY_TEST,
            ExecutionMode.DAILY_STREAM,
        )
        if lead.execution_mode not in daily_modes:
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

        # 4. Client audio-processing flags. Browser Krisp noise cancellation
        # defaults ON; templates opt out via
        # configurations.client_noise_cancellation=false. Echo cancellation
        # has no server knob — it's a getUserMedia constraint the web clients
        # always force on (Daily has no room-level echo/noise property; its
        # enable_noise_cancellation_ui only affects the Prebuilt UI).
        noise_cancellation = True
        if lead.template_id:
            template = await get_template_by_id(str(lead.template_id))
            cfg = template.configurations if template else None
            if cfg is not None and cfg.client_noise_cancellation is False:
                noise_cancellation = False

        # 5. Start Daily session (creates room, tokens, and starts bot)
        session = await start_daily_session(request.lead_id)
        session["noise_cancellation"] = noise_cancellation
        return session

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create Breeze Buddy Daily session: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create Breeze Buddy Daily session: {str(e)}",
        )
