"""Breeze Buddy data-source endpoints."""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.data_source import (
    CreateDataSourceRequest,
    DataSource,
    UpdateDataSourceRequest,
)
from app.services.data_sources import Discovery

from .handlers import (
    create_data_source_handler,
    delete_data_source_handler,
    discover_data_source_handler,
    get_data_source_handler,
    list_data_sources_handler,
    update_data_source_handler,
)

router = APIRouter()


@router.post(
    "/data-sources",
    response_model=DataSource,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_source_endpoint(
    req: CreateDataSourceRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Create a reusable data source."""
    return await create_data_source_handler(req, current_user)


@router.get("/data-sources", response_model=List[DataSource])
async def list_data_sources_endpoint(
    reseller_id: Optional[str] = Query(None),
    merchant_id: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List data sources visible to the current user."""
    return await list_data_sources_handler(
        current_user=current_user,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        include_inactive=include_inactive,
    )


@router.get("/data-sources/{data_source_id}/discover", response_model=Discovery)
async def discover_data_source_endpoint(
    data_source_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Discover source datasets, columns, and preview rows."""
    return await discover_data_source_handler(data_source_id, current_user)


@router.get("/data-sources/{data_source_id}", response_model=DataSource)
async def get_data_source_endpoint(
    data_source_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Get a data source by ID."""
    return await get_data_source_handler(data_source_id, current_user)


@router.put("/data-sources/{data_source_id}", response_model=DataSource)
async def update_data_source_endpoint(
    data_source_id: str,
    req: UpdateDataSourceRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Update a data source."""
    return await update_data_source_handler(data_source_id, req, current_user)


@router.delete(
    "/data-sources/{data_source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_data_source_endpoint(
    data_source_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Soft-delete a data source."""
    await delete_data_source_handler(data_source_id, current_user)
    return None
