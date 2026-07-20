"""DragonTTS management endpoints (admin-only).

Manual control of the one-way DragonTTS health flag:

- ``POST /admin/dragontts/manage`` — ``kill_switch`` marks DragonTTS unhealthy
  (calls bypass caching) or ``restore`` marks it healthy (calls resume
  caching). A single atomic Redis SET.
- ``GET  /admin/dragontts/status`` — current health state.

The automatic monitor runs independently and only ever marks the flag
unhealthy (1->0); the manage endpoint is the only way to restore it.
"""

from fastapi import APIRouter, Depends

from app.ai.voice.agents.breeze_buddy.tts.dragontts.kill_switch import (
    get_dragontts_status,
    set_dragontts_health,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.dragontts import (
    DragonTTSStatus,
    ManageDragonTTSAction,
    ManageDragonTTSRequest,
    ManageDragonTTSResponse,
)

router = APIRouter()


@router.post("/admin/dragontts/manage", response_model=ManageDragonTTSResponse)
async def manage_dragon_tts(
    body: ManageDragonTTSRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ManageDragonTTSResponse:
    """Manage the DragonTTS health flag: ``kill_switch`` -> bypass caching,
    ``restore`` -> resume caching."""
    require_admin(current_user)
    await set_dragontts_health(body.action == ManageDragonTTSAction.RESTORE)
    status = await get_dragontts_status()
    return ManageDragonTTSResponse(action=body.action, **status)


@router.get(
    "/admin/dragontts/status",
    response_model=DragonTTSStatus,
)
async def get_dragon_tts_status(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> DragonTTSStatus:
    """Return the current DragonTTS health-gate state (health + caching)."""
    require_admin(current_user)
    return DragonTTSStatus(**await get_dragontts_status())
