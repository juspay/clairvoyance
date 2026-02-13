"""
Cron job endpoints for Breeze Buddy.
Handles scheduled tasks and background job triggers.

DEPRECATED: Call initiation is now handled by the background task scheduler.
See: app/ai/voice/agents/breeze_buddy/managers/tasks.py
"""

from fastapi import APIRouter, Depends

from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app.schemas import TokenData

router = APIRouter()


@router.get("/cron/initiate")
async def initiate_cron(
    current_user: TokenData = Depends(get_current_user),
):
    """
    DEPRECATED: This endpoint is no longer functional.
    Call initiation is now handled automatically by the background task scheduler.
    This endpoint is kept for backward compatibility and returns a 200 OK response.
    """
    logger.warning(
        f"Deprecated /cron/initiate called by user {current_user.user_id}. "
        "Call initiation is now handled by background task scheduler."
    )
    return {
        "status": "ok",
        "message": "Deprecated: Call initiation is now handled by background task scheduler",
    }
