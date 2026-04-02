"""
Playground endpoints for Breeze Buddy configuration exploration.

Endpoints:
- GET /playground/configurations/options - Get available configuration options
"""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo

from .handlers import get_configuration_options_handler

router = APIRouter()


@router.get("/playground/configurations/options")
async def get_configuration_options(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    return await get_configuration_options_handler()
