"""
REST endpoints for data source management.

Endpoints:
- POST   /data-sources                       - Create data source
- GET    /data-sources                       - List data sources (paginated)
- GET    /data-sources/sheets/tabs           - List tabs in a Google Sheet (discovery)
- GET    /data-sources/sheets/columns        - List column headers (discovery)
- GET    /data-sources/sheets/preview        - Preview sheet data (discovery)
- GET    /data-sources/{id}                  - Get single data source
- PUT    /data-sources/{id}                  - Update data source
- DELETE /data-sources/{id}                  - Delete data source

IMPORTANT: discovery routes declared BEFORE /{id} to avoid FastAPI path conflict.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.data_source import (
    ColumnsResponse,
    DataSourceCreate,
    DataSourceListResponse,
    DataSourceResponse,
    DataSourceUpdate,
    PreviewResponse,
    TabsResponse,
)

from .handlers import (
    create_data_source_handler,
    delete_data_source_handler,
    get_data_source_handler,
    list_columns_handler,
    list_data_sources_handler,
    list_tabs_handler,
    preview_handler,
    update_data_source_handler,
)

router = APIRouter()


# ─── Discovery routes (must come before /{id}) ────────────────────────────────


@router.get("/data-sources/sheets/tabs", response_model=TabsResponse)
async def get_sheet_tabs(
    spreadsheet_url: str = Query(..., description="Full Google Sheets URL"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List all tab names in a Google Spreadsheet."""
    return await list_tabs_handler(spreadsheet_url)


@router.get("/data-sources/sheets/columns", response_model=ColumnsResponse)
async def get_sheet_columns(
    spreadsheet_url: str = Query(..., description="Full Google Sheets URL"),
    sheet_name: Optional[str] = Query(
        None, description="Tab name (default: first tab)"
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List column headers for a sheet tab."""
    return await list_columns_handler(spreadsheet_url, sheet_name)


@router.get("/data-sources/sheets/preview", response_model=PreviewResponse)
async def preview_sheet(
    spreadsheet_url: str = Query(..., description="Full Google Sheets URL"),
    sheet_name: Optional[str] = Query(
        None, description="Tab name (default: first tab)"
    ),
    columns: Optional[List[str]] = Query(None, description="Columns to include"),
    max_rows: int = Query(10, ge=1, le=100, description="Max rows to return"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Preview up to N rows from a sheet."""
    return await preview_handler(spreadsheet_url, sheet_name, columns, max_rows)


# ─── CRUD routes ──────────────────────────────────────────────────────────────


@router.post(
    "/data-sources",
    response_model=DataSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_source(
    req: DataSourceCreate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Create a new data source."""
    return await create_data_source_handler(req, current_user)


@router.get("/data-sources", response_model=DataSourceListResponse)
async def list_data_sources(
    reseller_id: Optional[str] = Query(None),
    merchant_id: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List data sources with optional filters."""
    return await list_data_sources_handler(
        current_user=current_user,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        is_active=is_active,
        page=page,
        limit=limit,
    )


@router.get("/data-sources/{data_source_id}", response_model=DataSourceResponse)
async def get_data_source(
    data_source_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Get a single data source by ID."""
    return await get_data_source_handler(data_source_id, current_user)


@router.put("/data-sources/{data_source_id}", response_model=DataSourceResponse)
async def update_data_source(
    data_source_id: str,
    req: DataSourceUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Update a data source."""
    return await update_data_source_handler(data_source_id, req, current_user)


@router.delete("/data-sources/{data_source_id}", status_code=status.HTTP_200_OK)
async def delete_data_source(
    data_source_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Delete a data source."""
    return await delete_data_source_handler(data_source_id, current_user)
