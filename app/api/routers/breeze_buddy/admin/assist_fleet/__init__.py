"""Admin-only Buddy Assist fleet inventory.

``GET /admin/assist-fleet`` — every widget_config (an assist agent's public
surface) with its merchant, bound template, chat-session usage and health
flags; every chat-agent template under the assist resellers with its
generation and, for orphans, a cleanup recommendation; the shared-prompt-block
variants; and fleet-level findings. Read-only. Actions (deactivate / delete a
template, disable a widget) go through the existing template and
widget-config endpoints.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.routers.breeze_buddy.admin.assist_fleet.handlers import (
    get_assist_fleet_handler,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.admin.assist_fleet import AssistFleetResponse

router = APIRouter()


@router.get("/admin/assist-fleet", response_model=AssistFleetResponse)
async def get_assist_fleet(
    window_days: int = Query(
        default=30, ge=7, le=90, description="Usage window in days (7-90)."
    ),
    reference_template_id: Optional[str] = Query(
        default=None,
        description=(
            "Template whose '## Operating principles' block counts as the fleet "
            "standard, in addition to the reseller blueprints."
        ),
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> AssistFleetResponse:
    """Fleet-wide inventory of assist agents.

    **RBAC rules:** admin only; a reseller or merchant token gets 403.
    """
    require_admin(current_user)
    return await get_assist_fleet_handler(
        current_user,
        window_days=window_days,
        reference_template_id=reference_template_id,
    )


__all__ = ["router"]
