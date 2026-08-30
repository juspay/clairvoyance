"""Business logic for ui_component CRUD (migration 057, CHAMELEON).

All handlers run after the route layer has validated the user's RBAC
token. Reseller/merchant scoping is enforced via
``validate_template_access`` (same predicate semantics as templates and
widget_config). Every write runs the registration guards from
``chat/ui/custom_defs.py`` — a def that fails them never reaches a
session.
"""

from typing import Optional

from fastapi import HTTPException, status
from pydantic import BaseModel

from app.ai.voice.agents.breeze_buddy.chat.ui.custom_defs import (
    validate_registration,
)
from app.api.routers.breeze_buddy.templates.rbac import validate_template_access
from app.core.logger import logger
from app.database.accessor.breeze_buddy.ui_component import (
    create_ui_component,
    delete_ui_component,
    get_ui_component_by_id,
    list_ui_components,
    update_ui_component,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.ui_component import (
    UiComponentCreate,
    UiComponentListResponse,
    UiComponentResponse,
    UiComponentUpdate,
)


class DeleteUiComponentResponse(BaseModel):
    deleted: bool
    id: str


async def create_ui_component_handler(
    body: UiComponentCreate, current_user: UserInfo
) -> UiComponentResponse:
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) registering "
        f"ui_component {body.name!r} for reseller={body.reseller_id} "
        f"merchant={body.merchant_id}"
    )
    validate_template_access(
        current_user,
        body.reseller_id,
        body.merchant_id,
        operation="create ui_component",
    )
    errors = validate_registration(
        name=body.name,
        props_schema=body.props_schema,
        flags=body.flags,
        render_def=body.render_def,
    )
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"registration_errors": errors},
        )
    try:
        created = await create_ui_component(
            reseller_id=body.reseller_id,
            merchant_id=body.merchant_id,
            name=body.name,
            props_schema=body.props_schema,
            flags=body.flags,
            render_def=body.render_def,
            prompt_hint=body.prompt_hint,
            is_active=body.is_active,
        )
    except Exception as exc:
        if "ui_component_scope_name_unique" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"ui_component {body.name!r} already exists in this "
                    "scope. Update the existing row instead."
                ),
            )
        raise
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create ui_component",
        )
    return created


async def _fetch_scoped(
    ui_component_id: str, current_user: UserInfo, operation: str
) -> UiComponentResponse:
    row = await get_ui_component_by_id(ui_component_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ui_component '{ui_component_id}' not found",
        )
    validate_template_access(
        current_user, row.reseller_id, row.merchant_id, operation=operation
    )
    return row


async def get_ui_component_by_id_handler(
    ui_component_id: str, current_user: UserInfo
) -> UiComponentResponse:
    return await _fetch_scoped(ui_component_id, current_user, "read ui_component")


async def list_ui_components_handler(
    *,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    include_inactive: bool,
    page: int,
    limit: int,
    current_user: UserInfo,
) -> UiComponentListResponse:
    accessible_reseller_ids: Optional[list] = None
    if current_user.role != "admin":
        if "*" not in current_user.reseller_ids:
            accessible_reseller_ids = current_user.reseller_ids
            if reseller_id and reseller_id not in current_user.reseller_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to reseller {reseller_id}",
                )
    items, total = await list_ui_components(
        page=page,
        limit=limit,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=accessible_reseller_ids,
        include_inactive=include_inactive,
    )
    return UiComponentListResponse(ui_components=items, total=total, page=page)


async def update_ui_component_handler(
    ui_component_id: str, body: UiComponentUpdate, current_user: UserInfo
) -> UiComponentResponse:
    row = await _fetch_scoped(ui_component_id, current_user, "update ui_component")
    # Guards re-run on the MERGED row — a partial update cannot sneak an
    # invalid schema or def past registration.
    errors = validate_registration(
        name=row.name,
        props_schema=(
            body.props_schema if body.props_schema is not None else row.props_schema
        ),
        flags=body.flags if body.flags is not None else row.flags,
        render_def=(body.render_def if body.render_def is not None else row.render_def),
    )
    # The stored name already passed the collision check at create time;
    # re-running it against the (now-registered) built-ins can only
    # re-flag a legitimate name if a flavor later ships the same one —
    # in that case the update is the right place to hear about it.
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"registration_errors": errors},
        )
    updated = await update_ui_component(
        ui_component_id,
        props_schema=body.props_schema,
        flags=body.flags,
        render_def=body.render_def,
        prompt_hint=body.prompt_hint,
        is_active=body.is_active,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update ui_component",
        )
    return updated


async def delete_ui_component_handler(
    ui_component_id: str, current_user: UserInfo
) -> DeleteUiComponentResponse:
    await _fetch_scoped(ui_component_id, current_user, "delete ui_component")
    deleted = await delete_ui_component(ui_component_id)
    return DeleteUiComponentResponse(deleted=deleted, id=ui_component_id)
