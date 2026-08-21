"""Routes for template version history and rollback."""

from fastapi import APIRouter, Depends, Query

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template_version import (
    RollbackTemplateRequest,
    TemplateVersionDetailResponse,
    TemplateVersionListResponse,
)

from .version_handlers import (
    get_template_version_handler,
    list_template_versions_handler,
    rollback_template_handler,
)

router = APIRouter()


@router.get(
    "/templates/{template_id}/versions",
    response_model=TemplateVersionListResponse,
)
async def list_template_versions(
    template_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Version history (metadata only). Newest first."""
    return await list_template_versions_handler(
        template_id, limit, offset, current_user
    )


@router.get(
    "/templates/{template_id}/versions/{version}",
    response_model=TemplateVersionDetailResponse,
)
async def get_template_version(
    template_id: str,
    version: int,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Full snapshot of one version (secrets masked)."""
    return await get_template_version_handler(template_id, version, current_user)


@router.post("/templates/{template_id}/rollback", response_model=TemplateModel)
async def rollback_template(
    template_id: str,
    body: RollbackTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Restore an older version as the NEW head (appends a version; history
    is never rewritten). Admin or reseller owner."""
    return await rollback_template_handler(template_id, body, current_user)
