"""``GET /assist/blueprint`` — the reseller-level Assist blueprint, read-only.

The console's Template Studio compares an agent's operating core against
the blueprint the reseller's merchant templates are built from ("standard"
vs "drifted"); until now the row was reachable only by paging the whole
templates list without a merchant filter.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.ai.voice.agents.breeze_buddy.assist.verticals import registry as verticals
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.utils.secrets import mask_template_secrets
from app.api.security.breeze_buddy.authorization import validate_reseller_access
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_role
from app.database.accessor import get_template_in_scope
from app.schemas import UserInfo, UserRole

router = APIRouter()

# Read-only and secrets-masked, so every scoped role may see it: a merchant
# editing their own agent in the console needs the core to diff against.
_BLUEPRINT_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]


@router.get("/assist/blueprint", response_model=TemplateModel)
async def get_assist_blueprint(
    reseller_id: str = Query(..., min_length=1, max_length=255),
    vertical: Optional[str] = Query(
        None,
        description="The vertical whose blueprint to read; the default when absent.",
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplateModel:
    """The reseller-level blueprint the vertical's agents are built from,
    secrets masked exactly like ``GET /templates/{id}``. 404 when the
    reseller has none; 400 for an unknown vertical."""
    require_role(current_user, _BLUEPRINT_ROLES)
    validate_reseller_access(current_user, reseller_id=reseller_id)
    try:
        blueprint_name = verticals.for_request(vertical).blueprint_name
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    template = await get_template_in_scope(reseller_id.strip(), None, blueprint_name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The default Assist template is not configured for this reseller.",
        )
    return mask_template_secrets(template)


__all__ = ["router"]
