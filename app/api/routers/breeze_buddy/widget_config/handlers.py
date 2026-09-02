"""Business logic for widget_config CRUD (CHAT_MODE.md §14).

All handlers run after the route layer has validated the user's RBAC
token. Reseller/merchant scoping is enforced here via
``validate_template_access`` from the templates RBAC module — same
predicate semantics (admin → all; reseller owns reseller_id; merchant
owns merchant_id), so the two surfaces stay consistent.

``public_widget_key`` is generated server-side here and never read
from the request body, so a client cannot pre-commit a key it knows.
"""

import secrets
from math import ceil
from typing import Optional

from fastapi import HTTPException, status

from app.api.routers.breeze_buddy.templates.rbac import validate_template_access
from app.core.logger import logger
from app.database.accessor import get_template_by_id
from app.database.accessor.breeze_buddy.widget_config import (
    create_widget_config,
    delete_widget_config,
    get_widget_config_by_id,
    get_widget_config_by_reseller_merchant,
    list_widget_configs,
    update_widget_config,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.widget_config import (
    DeleteWidgetConfigResponse,
    WidgetConfigCreate,
    WidgetConfigListResponse,
    WidgetConfigResponse,
    WidgetConfigUpdate,
)

# 32 url-safe bytes → ~43 chars; well inside the column's varchar(128)
# and gives ~256 bits of entropy. Matches what major SaaS vendors hand
# out for embed keys.
_PUBLIC_KEY_NBYTES = 32


def _generate_public_widget_key() -> str:
    """Opaque, URL-safe, hard to guess. Never echo the prefix in 404s."""
    return secrets.token_urlsafe(_PUBLIC_KEY_NBYTES)


async def create_widget_config_handler(
    body: WidgetConfigCreate, current_user: UserInfo
) -> WidgetConfigResponse:
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) creating "
        f"widget_config for reseller={body.reseller_id} merchant={body.merchant_id}"
    )

    # RBAC: caller must own the reseller_id + merchant_id they're
    # configuring. Admin is allowed everywhere.
    validate_template_access(
        current_user,
        body.reseller_id,
        body.merchant_id,
        operation="create widget_config",
    )

    # Confirm the template exists and is in the same reseller/merchant
    # scope. The FK would catch a missing template_id at insert time, but
    # we want a clear 400 (not 500) for invalid input.
    template = await get_template_by_id(body.template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Template '{body.template_id}' not found",
        )
    if template.reseller_id != body.reseller_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template does not belong to the specified reseller",
        )
    # ``template.merchant_id is None`` means the template applies to all
    # merchants under that reseller — allowed for any merchant_id here.
    # Otherwise both must match.
    template_merchant = getattr(template, "merchant_id", None)
    if template_merchant and template_merchant != body.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template does not belong to the specified merchant",
        )

    existing = await get_widget_config_by_reseller_merchant(
        body.reseller_id, body.merchant_id
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "widget_config already exists for this reseller/merchant. "
                "Update or delete the existing row instead."
            ),
        )

    public_widget_key = _generate_public_widget_key()
    created = await create_widget_config(
        reseller_id=body.reseller_id,
        merchant_id=body.merchant_id,
        public_widget_key=public_widget_key,
        template_id=body.template_id,
        allowed_origins=body.allowed_origins,
        max_sessions_per_ip_hour=body.max_sessions_per_ip_hour,
        max_messages_per_ip_hour=body.max_messages_per_ip_hour,
        max_concurrent_per_ip=body.max_concurrent_per_ip,
        max_voice_sessions_per_ip_hour=body.max_voice_sessions_per_ip_hour,
        appearance=body.appearance,
        active=body.active,
    )
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create widget_config",
        )
    return created


async def list_widget_configs_handler(
    *,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    include_inactive: bool,
    page: int,
    limit: int,
    current_user: UserInfo,
) -> WidgetConfigListResponse:
    """RBAC-aware listing.

    Admins see all rows in scope. Non-admins are scoped to their
    accessible resellers / merchants; the query receives those arrays
    as ``reseller_ids`` / ``merchant_ids`` filters. A non-admin
    requesting a specific reseller_id / merchant_id they don't own
    gets 403.
    """
    accessible_reseller_ids: Optional[list] = None
    accessible_merchant_ids: Optional[list] = None

    if current_user.role != "admin":
        if "*" not in current_user.reseller_ids:
            accessible_reseller_ids = current_user.reseller_ids
            if reseller_id and reseller_id not in current_user.reseller_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to reseller {reseller_id}",
                )
        if "*" not in current_user.merchant_ids:
            accessible_merchant_ids = current_user.merchant_ids
            if merchant_id and merchant_id not in current_user.merchant_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {merchant_id}",
                )

    items, total = await list_widget_configs(
        page=page,
        limit=limit,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=accessible_reseller_ids,
        merchant_ids=accessible_merchant_ids,
        include_inactive=include_inactive,
    )
    return WidgetConfigListResponse(
        widget_configs=items,
        total=total,
        page=page,
        limit=limit,
    )


async def get_widget_config_by_id_handler(
    widget_config_id: str, current_user: UserInfo
) -> WidgetConfigResponse:
    cfg = await get_widget_config_by_id(widget_config_id)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"widget_config {widget_config_id} not found",
        )
    validate_template_access(
        current_user,
        cfg.reseller_id,
        cfg.merchant_id,
        operation="read widget_config",
    )
    return cfg


async def update_widget_config_handler(
    widget_config_id: str,
    body: WidgetConfigUpdate,
    current_user: UserInfo,
) -> WidgetConfigResponse:
    cfg = await get_widget_config_by_id(widget_config_id)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"widget_config {widget_config_id} not found",
        )
    validate_template_access(
        current_user,
        cfg.reseller_id,
        cfg.merchant_id,
        operation="update widget_config",
    )

    # If template_id is being changed, validate it exists and stays in
    # the same reseller/merchant scope as the widget_config.
    if body.template_id is not None and body.template_id != cfg.template_id:
        template = await get_template_by_id(body.template_id)
        if not template:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Template '{body.template_id}' not found",
            )
        if template.reseller_id != cfg.reseller_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Template does not belong to this widget's reseller",
            )
        template_merchant = getattr(template, "merchant_id", None)
        if template_merchant and template_merchant != cfg.merchant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Template does not belong to this widget's merchant",
            )

    updated = await update_widget_config(
        widget_config_id,
        template_id=body.template_id,
        allowed_origins=body.allowed_origins,
        max_sessions_per_ip_hour=body.max_sessions_per_ip_hour,
        max_messages_per_ip_hour=body.max_messages_per_ip_hour,
        max_concurrent_per_ip=body.max_concurrent_per_ip,
        max_voice_sessions_per_ip_hour=body.max_voice_sessions_per_ip_hour,
        appearance=body.appearance,
        active=body.active,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update widget_config",
        )
    return updated


async def delete_widget_config_handler(
    widget_config_id: str, current_user: UserInfo
) -> DeleteWidgetConfigResponse:
    cfg = await get_widget_config_by_id(widget_config_id)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"widget_config {widget_config_id} not found",
        )
    # Admin gate already happened at the route layer; we don't double-
    # check via validate_template_access because admin matches everything.
    deleted = await delete_widget_config(widget_config_id)
    if not deleted:
        # Race with a concurrent delete — treat as 404 for the caller.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"widget_config {widget_config_id} not found",
        )
    return DeleteWidgetConfigResponse(
        status="success",
        message=(
            f"widget_config {widget_config_id} (reseller={cfg.reseller_id}, "
            f"merchant={cfg.merchant_id}) deleted"
        ),
        deleted_id=widget_config_id,
    )


# Re-exported for callers that want to compute total_pages from a list
# response without importing math themselves.
def total_pages(total: int, limit: int) -> int:
    return max(ceil(total / limit), 1) if limit else 1
