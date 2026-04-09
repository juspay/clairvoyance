"""
API router for outbound number pool management.
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.routers.breeze_buddy.number_pools.handlers import (
    add_number_to_pool_handler,
    create_pool_handler,
    delete_pool_handler,
    get_pool_handler,
    list_pools_handler,
    remove_number_from_pool_handler,
    update_pool_handler,
)
from app.api.routers.breeze_buddy.number_pools.rbac import (
    filter_pools_by_rbac,
    require_admin_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import (
    CreateOutboundNumberPoolRequest,
    OutboundNumber,
    OutboundNumberPool,
    UpdateOutboundNumberPoolRequest,
)
from app.schemas.breeze_buddy.auth import UserInfo

router = APIRouter()


@router.post(
    "/number-pools",
    response_model=OutboundNumberPool,
    status_code=status.HTTP_201_CREATED,
)
async def create_pool(
    pool: CreateOutboundNumberPoolRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Create a new outbound number pool."""
    require_admin_access(current_user, "create number pools")
    return await create_pool_handler(pool, current_user)


@router.get(
    "/number-pools",
    response_model=List[OutboundNumberPool],
)
async def list_pools(
    reseller_id: Optional[str] = Query(None),
    merchant_id: Optional[str] = Query(None),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List all outbound number pools."""
    pools = await list_pools_handler(current_user, reseller_id, merchant_id)
    return filter_pools_by_rbac(pools, current_user)


@router.get(
    "/number-pools/{pool_id}",
    response_model=Dict,
)
async def get_pool(
    pool_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Get a pool by ID, including its numbers."""
    return await get_pool_handler(pool_id, current_user)


@router.put(
    "/number-pools/{pool_id}",
    response_model=OutboundNumberPool,
)
async def update_pool(
    pool_id: str,
    update: UpdateOutboundNumberPoolRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Update a pool's name or max_channels."""
    require_admin_access(current_user, "update number pools")
    return await update_pool_handler(pool_id, update, current_user)


@router.delete(
    "/number-pools/{pool_id}",
    response_model=OutboundNumberPool,
)
async def delete_pool(
    pool_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Soft-delete a pool by setting status to DISABLED."""
    require_admin_access(current_user, "delete number pools")
    return await delete_pool_handler(pool_id, current_user)


@router.post(
    "/number-pools/{pool_id}/numbers/{number_id}",
    response_model=OutboundNumber,
    status_code=status.HTTP_201_CREATED,
)
async def add_number_to_pool(
    pool_id: str,
    number_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Add an outbound number to a pool."""
    require_admin_access(current_user, "manage pool numbers")
    return await add_number_to_pool_handler(pool_id, number_id, current_user)


@router.delete(
    "/number-pools/{pool_id}/numbers/{number_id}",
    response_model=OutboundNumber,
)
async def remove_number_from_pool(
    pool_id: str,
    number_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Remove an outbound number from a pool."""
    require_admin_access(current_user, "manage pool numbers")
    return await remove_number_from_pool_handler(pool_id, number_id, current_user)
