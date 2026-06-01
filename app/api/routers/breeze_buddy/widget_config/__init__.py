"""RBAC-gated CRUD for widget_config rows (CHAT_MODE.md §14).

Five endpoints under ``/agent/voice/breeze-buddy/widget-config``:

- ``POST   /widget-config``            — create (admin / scoped reseller)
- ``GET    /widget-config/list``       — list (RBAC-filtered)
- ``GET    /widget-config/{id}``       — get by id
- ``PUT    /widget-config/{id}``       — partial update
- ``DELETE /widget-config/{id}``       — delete (admin only)

Mirrors the templates router (see ``templates/__init__.py``) — thin
routes that validate auth + RBAC, then delegate to handlers.
``public_widget_key`` is generated server-side at create-time and never
accepted from the client.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.widget_config import (
    DeleteWidgetConfigResponse,
    WidgetConfigCreate,
    WidgetConfigListResponse,
    WidgetConfigResponse,
    WidgetConfigUpdate,
)

from .handlers import (
    create_widget_config_handler,
    delete_widget_config_handler,
    get_widget_config_by_id_handler,
    list_widget_configs_handler,
    update_widget_config_handler,
)

router = APIRouter()


@router.post(
    "/widget-config",
    status_code=status.HTTP_201_CREATED,
    response_model=WidgetConfigResponse,
)
async def create_widget_config(
    body: WidgetConfigCreate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> WidgetConfigResponse:
    """Create a widget_config row. ``public_widget_key`` is generated
    server-side; the request body cannot supply it.

    RBAC:
    - Admin: can create for any reseller/merchant.
    - Reseller / Merchant: must have access to the reseller_id and
      merchant_id in the body (403 otherwise).
    """
    return await create_widget_config_handler(body, current_user)


@router.get("/widget-config/list", response_model=WidgetConfigListResponse)
async def list_widget_configs(
    reseller_id: Optional[str] = Query(None),
    merchant_id: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> WidgetConfigListResponse:
    """List widget_configs visible to the caller.

    Filter by reseller_id / merchant_id (subject to RBAC) or pass
    nothing to get everything the caller can see. ``include_inactive``
    defaults to ``False``.
    """
    return await list_widget_configs_handler(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        include_inactive=include_inactive,
        page=page,
        limit=limit,
        current_user=current_user,
    )


@router.get("/widget-config/{widget_config_id}", response_model=WidgetConfigResponse)
async def get_widget_config(
    widget_config_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> WidgetConfigResponse:
    """Get a widget_config by id. RBAC: must have access to its
    reseller / merchant (403 otherwise)."""
    return await get_widget_config_by_id_handler(widget_config_id, current_user)


@router.put("/widget-config/{widget_config_id}", response_model=WidgetConfigResponse)
async def update_widget_config(
    widget_config_id: str,
    body: WidgetConfigUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> WidgetConfigResponse:
    """Partial update. Fields omitted from the body remain unchanged.
    ``public_widget_key`` cannot be set or rotated through this route."""
    return await update_widget_config_handler(widget_config_id, body, current_user)


@router.delete(
    "/widget-config/{widget_config_id}", response_model=DeleteWidgetConfigResponse
)
async def delete_widget_config(
    widget_config_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> DeleteWidgetConfigResponse:
    """Delete a widget_config row. Admin only (matches templates DELETE)."""
    require_admin(current_user)
    return await delete_widget_config_handler(widget_config_id, current_user)


__all__ = ["router"]
