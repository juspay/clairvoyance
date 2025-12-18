"""
Cron job endpoints for Breeze Buddy.
Handles scheduled tasks and background job triggers.
"""

from fastapi import APIRouter, BackgroundTasks, Depends

from app.ai.voice.agents.breeze_buddy.managers.calls import process_backlog_leads
from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app.schemas import TokenData

router = APIRouter()


@router.get("/cron/initiate")
async def initiate_cron(
    background_tasks: BackgroundTasks,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Initiates the cron job to process backlog leads.
    """
    logger.info(f"Authenticated user {current_user.user_id} initiating cron job")
    background_tasks.add_task(process_backlog_leads)
    return {"status": "success", "message": "Lead processing initiated"}
