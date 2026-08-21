"""Handlers for template version (lineage) endpoints."""

import asyncpg
from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.template.cache import invalidate_template
from app.ai.voice.agents.breeze_buddy.utils.secrets import mask_template_secrets
from app.core.logger import logger
from app.database.accessor.breeze_buddy.template import get_template_by_id
from app.database.accessor.breeze_buddy.template_version import (
    get_template_version,
    list_template_versions,
    rollback_template_to_version,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template_version import (
    RollbackTemplateRequest,
    TemplateVersionDetailResponse,
    TemplateVersionListResponse,
)

from .rbac import require_admin_or_reseller_owner, validate_template_access


async def _load_and_authorize(template_id: str, current_user: UserInfo, operation: str):
    template = await get_template_by_id(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )
    validate_template_access(
        current_user, template.reseller_id, template.merchant_id, operation=operation
    )
    return template


async def list_template_versions_handler(
    template_id: str, limit: int, offset: int, current_user: UserInfo
) -> TemplateVersionListResponse:
    template = await _load_and_authorize(
        template_id, current_user, "list template versions"
    )
    versions, total = await list_template_versions(template_id, limit, offset)
    return TemplateVersionListResponse(
        template_id=template_id,
        current_version=template.current_version,
        versions=versions,
        total=total,
    )


async def get_template_version_handler(
    template_id: str, version: int, current_user: UserInfo
) -> TemplateVersionDetailResponse:
    await _load_and_authorize(template_id, current_user, "read template version")
    found = await get_template_version(template_id, version)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {version} not found for template {template_id}",
        )
    snapshot, meta = found
    return TemplateVersionDetailResponse(
        meta=meta, snapshot=mask_template_secrets(snapshot)
    )


async def rollback_template_handler(
    template_id: str, body: RollbackTemplateRequest, current_user: UserInfo
):
    template = await _load_and_authorize(template_id, current_user, "rollback template")
    require_admin_or_reseller_owner(
        current_user, template.reseller_id, operation="rollback template"
    )
    try:
        new_head = await rollback_template_to_version(
            template_id, body.version, changed_by=current_user.username
        )
    except (asyncpg.UniqueViolationError, asyncpg.ForeignKeyViolationError) as e:
        # loguru has no exc_info kwarg -- logger.opt(exception=...) is what
        # actually attaches the traceback. The asyncpg message can carry
        # constraint names and parameter values, so it stays in the log only.
        logger.opt(exception=e).error(f"Rollback failed for template {template_id}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Rollback to version {body.version} conflicts with current data "
                "(name uniqueness or telephony number)"
            ),
        ) from e
    except Exception as e:
        logger.opt(exception=e).error(f"Rollback failed for template {template_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rollback failed",
        ) from e
    if new_head is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {body.version} not found for template {template_id}",
        )
    try:
        await invalidate_template(template_id)
    except Exception as cache_exc:
        logger.warning(
            f"Template cache invalidation failed for {template_id}: {cache_exc}"
        )
    logger.info(
        f"User {current_user.username} rolled back template {template_id} "
        f"to version {body.version} (new head v{new_head.current_version})"
    )
    return mask_template_secrets(new_head)
