"""RBAC-gated CRUD for ui_component rows (migration 057, CHAMELEON).

Five endpoints under ``/agent/voice/breeze-buddy/ui-components``:

- ``POST   /ui-components``            — register (admin / scoped reseller)
- ``GET    /ui-components/list``       — list (RBAC-filtered)
- ``GET    /ui-components/{id}``       — get by id
- ``PUT    /ui-components/{id}``       — partial update (bumps version)
- ``DELETE /ui-components/{id}``       — delete (admin only)

Mirrors the widget_config router — thin routes that validate auth + RBAC,
then delegate to handlers. Every write runs the registration guards
(``chat/ui/custom_defs.validate_registration``): name shape + built-in
collision, JSON-Schema well-formedness, v1 flag rules, render_def lint.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.ui_component import (
    UiComponentCreate,
    UiComponentListResponse,
    UiComponentResponse,
    UiComponentUpdate,
)

from .handlers import (
    DeleteUiComponentResponse,
    create_ui_component_handler,
    delete_ui_component_handler,
    get_ui_component_by_id_handler,
    list_ui_components_handler,
    update_ui_component_handler,
)

router = APIRouter()


@router.post(
    "/ui-components",
    status_code=status.HTTP_201_CREATED,
    response_model=UiComponentResponse,
)
async def create_ui_component(
    body: UiComponentCreate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UiComponentResponse:
    """Register a custom component. 400 with the full guard-error list on
    any registration violation."""
    return await create_ui_component_handler(body, current_user)


@router.get("/ui-components/list", response_model=UiComponentListResponse)
async def list_ui_components(
    reseller_id: Optional[str] = Query(None),
    merchant_id: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UiComponentListResponse:
    """List ui_components visible to the caller (RBAC-filtered)."""
    return await list_ui_components_handler(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        include_inactive=include_inactive,
        page=page,
        limit=limit,
        current_user=current_user,
    )


@router.get("/ui-components/{ui_component_id}", response_model=UiComponentResponse)
async def get_ui_component(
    ui_component_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UiComponentResponse:
    """Get a ui_component by id (403 outside the caller's scope)."""
    return await get_ui_component_by_id_handler(ui_component_id, current_user)


@router.put("/ui-components/{ui_component_id}", response_model=UiComponentResponse)
async def update_ui_component(
    ui_component_id: str,
    body: UiComponentUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UiComponentResponse:
    """Partial update; every real change bumps ``version``. Guards re-run
    on the merged row."""
    return await update_ui_component_handler(ui_component_id, body, current_user)


@router.delete(
    "/ui-components/{ui_component_id}", response_model=DeleteUiComponentResponse
)
async def delete_ui_component(
    ui_component_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> DeleteUiComponentResponse:
    """Delete a ui_component row. Admin only (matches templates DELETE)."""
    require_admin(current_user)
    return await delete_ui_component_handler(ui_component_id, current_user)


__all__ = ["router"]
