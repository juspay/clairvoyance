"""Admin-only template maintenance.

``DELETE /admin/templates/{id}/purge`` — delete a template *and* its chat
history in one transaction. The tenant ``DELETE /templates/{id}`` cannot
remove a template that ever served a chat session (``chat_session.template_id``
is ``ON DELETE RESTRICT``), so orphaned or duplicated agent templates could
only be deactivated. This is the admin's answer when the transcripts are not
worth keeping. ``?dry_run=true`` reports what would go without touching
anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.routers.breeze_buddy.admin.templates.handlers import (
    purge_template_handler,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.admin.template_purge import TemplatePurgeResponse

router = APIRouter()


@router.delete(
    "/admin/templates/{template_id}/purge", response_model=TemplatePurgeResponse
)
async def purge_template(
    template_id: str,
    dry_run: bool = Query(
        default=False,
        description="Report what the purge would delete without deleting anything.",
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TemplatePurgeResponse:
    """Delete a template together with every chat session that used it.

    **RBAC rules:** admin only; a reseller or merchant token gets 403.
    Refuses with 409 while a widget config, call execution config or
    in-flight lead still references the template.
    """
    require_admin(current_user)
    return await purge_template_handler(template_id, current_user, dry_run=dry_run)


__all__ = ["router"]
