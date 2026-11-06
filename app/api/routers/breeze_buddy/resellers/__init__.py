"""
Reseller (umbrella) entity API endpoints.

The reseller entity is the umbrella itself (see migration 036); a users row
with the same id and role='reseller' is an optional login for it.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.resellers import (
    ResellerCreate,
    ResellerListResponse,
    ResellerResponse,
    ResellerUpdate,
)
from app.schemas.breeze_buddy.users import DeleteUserResponse

from .handlers import (
    create_reseller_handler,
    delete_reseller_handler,
    get_all_resellers_handler,
    get_reseller_by_id_handler,
    update_reseller_handler,
)

router = APIRouter()


@router.get("/resellers", response_model=ResellerListResponse)
async def list_resellers(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    search: Optional[str] = Query(
        None, description="Filter by id or name (partial match)"
    ),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    sort_by: str = Query(
        "created_at",
        description="Sort by field",
        pattern="^(id|name|created_at|updated_at)$",
    ),
    sort_order: str = Query("desc", description="Sort order", pattern="^(asc|desc)$"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List reseller entities with workspace/member counts.

    **RBAC rules:**
    - Admin: sees all umbrellas
    - Reseller: sees their own umbrella
    - Merchant/User: see umbrellas they hold grants on (display labels)
    """
    return await get_all_resellers_handler(
        page=page,
        limit=limit,
        search=search,
        is_active_filter=is_active,
        sort_by=sort_by,
        sort_order=sort_order,
        current_user=current_user,
    )


@router.post("/reseller", response_model=ResellerResponse, status_code=201)
async def create_reseller(
    reseller_data: ResellerCreate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Create a reseller (umbrella) entity. Admin only.

    Does NOT create a login — create a users account (role='reseller') with
    the same id through the users API if the umbrella needs one.

    Raises:
        403: If caller is not admin
        409: If the id is already taken
    """
    return await create_reseller_handler(reseller_data, current_user)


@router.get("/reseller/{reseller_id}", response_model=ResellerResponse)
async def get_reseller(
    reseller_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Get one reseller entity (admin, or a caller with umbrella access)."""
    return await get_reseller_by_id_handler(reseller_id, current_user)


@router.put("/reseller/{reseller_id}", response_model=ResellerResponse)
async def update_reseller(
    reseller_id: str,
    reseller_data: ResellerUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update a reseller entity.

    **RBAC rules:**
    - Admin: any field
    - Reseller: own umbrella only, name/description only
    """
    return await update_reseller_handler(reseller_id, reseller_data, current_user)


@router.delete("/reseller/{reseller_id}", response_model=DeleteUserResponse)
async def delete_reseller(
    reseller_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete a reseller entity. Admin only.

    Refused with 409 while workspaces still belong to the umbrella. Access
    grants cascade; a same-id login row is untouched (delete it via the
    users API if needed).
    """
    return await delete_reseller_handler(reseller_id, current_user)
