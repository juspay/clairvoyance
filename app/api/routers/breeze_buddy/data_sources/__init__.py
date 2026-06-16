"""
Sheet discovery endpoints (admin-only utility).

Discovery routes declared BEFORE /{id} to avoid path conflict if CRUD endpoints
are added later.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.data_source import (
    ColumnsResponse,
    PreviewResponse,
    TabsResponse,
)

from .handlers import (
    list_columns_handler,
    list_tabs_handler,
    preview_handler,
)

router = APIRouter()


@router.get("/data-sources/sheets/tabs", response_model=TabsResponse)
async def get_sheet_tabs(
    spreadsheet_url: str = Query(..., description="Full Google Sheets URL"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List all tab names in a Google Spreadsheet (admin-only)."""
    require_admin(current_user)
    return await list_tabs_handler(spreadsheet_url, current_user)


@router.get("/data-sources/sheets/columns", response_model=ColumnsResponse)
async def get_sheet_columns(
    spreadsheet_url: str = Query(..., description="Full Google Sheets URL"),
    sheet_name: Optional[str] = Query(
        None, description="Tab name (default: first tab)"
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List column headers for a sheet tab (admin-only)."""
    require_admin(current_user)
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
    """Preview up to N rows from a sheet (admin-only)."""
    require_admin(current_user)
    return await preview_handler(spreadsheet_url, sheet_name, columns, max_rows)
