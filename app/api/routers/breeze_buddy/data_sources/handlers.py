"""
Business logic handlers for data source operations.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.data_source import (
    create_data_source,
    delete_data_source,
    get_data_source_by_id,
    list_data_sources,
    update_data_source,
)
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
from app.services.google.sheets import (
    extract_spreadsheet_id,
    fetch_sheet_data,
    get_column_headers,
    list_tabs,
)


def _resolve_reseller_ids(current_user: UserInfo) -> List[str]:
    """Return the reseller IDs the caller is allowed to access."""
    from app.schemas.breeze_buddy.auth import UserRole

    if current_user.role == UserRole.ADMIN:
        return []  # admin can see all — no filter applied in list_data_sources
    return current_user.reseller_ids


async def create_data_source_handler(
    req: DataSourceCreate, current_user: UserInfo
) -> DataSourceResponse:
    """Create a new data source."""
    from app.schemas.breeze_buddy.auth import UserRole

    # Non-admin users can only create under their own reseller IDs
    if current_user.role != UserRole.ADMIN:
        if req.reseller_id not in current_user.reseller_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not allowed to create data source for this reseller",
            )

    logger.info(
        f"User {current_user.username} creating data source '{req.name}' "
        f"for reseller={req.reseller_id}"
    )

    ds = await create_data_source(
        reseller_id=req.reseller_id,
        merchant_id=req.merchant_id,
        name=req.name,
        source_type=req.source_type,
        spreadsheet_url=req.spreadsheet_url,
        sheet_name=req.sheet_name,
        columns=req.columns,
        format=req.format,
        is_active=req.is_active,
    )

    if not ds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create data source. URL may be invalid.",
        )

    return ds


async def list_data_sources_handler(
    current_user: UserInfo,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    page: int = 1,
    limit: int = 50,
) -> DataSourceListResponse:
    """List data sources with RBAC filtering."""
    from app.schemas.breeze_buddy.auth import UserRole

    if current_user.role == UserRole.ADMIN:
        effective_reseller_ids = [reseller_id] if reseller_id else None
        effective_reseller_id = reseller_id
    else:
        allowed = current_user.reseller_ids
        if reseller_id:
            if reseller_id not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not allowed to list data sources for this reseller",
                )
            effective_reseller_ids = [reseller_id]
            effective_reseller_id = reseller_id
        else:
            effective_reseller_ids = allowed
            effective_reseller_id = None

    rows, total = await list_data_sources(
        page=page,
        limit=limit,
        reseller_id=effective_reseller_id,
        reseller_ids=effective_reseller_ids if not effective_reseller_id else None,
        merchant_id=merchant_id,
        is_active=is_active,
    )

    import math

    total_pages = max(1, math.ceil(total / limit)) if total else 1

    return DataSourceListResponse(
        data_sources=rows,
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )


async def get_data_source_handler(
    data_source_id: str, current_user: UserInfo
) -> DataSourceResponse:
    """Get a single data source by ID."""
    from app.schemas.breeze_buddy.auth import UserRole

    ds = await get_data_source_by_id(data_source_id)
    if not ds:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data source {data_source_id} not found",
        )

    if current_user.role != UserRole.ADMIN:
        if ds.reseller_id not in current_user.reseller_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not allowed to access this data source",
            )

    return ds


async def update_data_source_handler(
    data_source_id: str, req: DataSourceUpdate, current_user: UserInfo
) -> DataSourceResponse:
    """Update a data source."""
    from app.schemas.breeze_buddy.auth import UserRole

    existing = await get_data_source_by_id(data_source_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data source {data_source_id} not found",
        )

    if current_user.role != UserRole.ADMIN:
        if existing.reseller_id not in current_user.reseller_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not allowed to update this data source",
            )

    updated = await update_data_source(
        data_source_id=data_source_id,
        name=req.name,
        spreadsheet_url=req.spreadsheet_url,
        sheet_name=req.sheet_name,
        columns=req.columns,
        format=req.format,
        is_active=req.is_active,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to update data source",
        )

    return updated


async def delete_data_source_handler(
    data_source_id: str, current_user: UserInfo
) -> dict:
    """Delete a data source."""
    from app.schemas.breeze_buddy.auth import UserRole

    existing = await get_data_source_by_id(data_source_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data source {data_source_id} not found",
        )

    if current_user.role != UserRole.ADMIN:
        if existing.reseller_id not in current_user.reseller_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not allowed to delete this data source",
            )

    deleted = await delete_data_source(data_source_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete data source",
        )

    return {"status": "success", "id": data_source_id}


async def list_tabs_handler(spreadsheet_url: str) -> TabsResponse:
    """List tabs in a Google Sheet."""
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
    if not spreadsheet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google Sheets URL",
        )

    tabs = await list_tabs(spreadsheet_id)
    return TabsResponse(spreadsheet_id=spreadsheet_id, tabs=tabs)


async def list_columns_handler(
    spreadsheet_url: str, sheet_name: Optional[str] = None
) -> ColumnsResponse:
    """List column headers for a sheet tab."""
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
    if not spreadsheet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google Sheets URL",
        )

    columns = await get_column_headers(spreadsheet_id, sheet_name)
    return ColumnsResponse(
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name or "",
        columns=columns,
    )


async def preview_handler(
    spreadsheet_url: str,
    sheet_name: Optional[str] = None,
    columns: Optional[List[str]] = None,
    max_rows: int = 10,
) -> PreviewResponse:
    """Preview sheet data (first N rows)."""
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
    if not spreadsheet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google Sheets URL",
        )

    rows = await fetch_sheet_data(
        spreadsheet_id, sheet_name, columns, max_rows=max_rows
    )
    col_names = list(rows[0].keys()) if rows else []

    return PreviewResponse(
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name,
        columns=col_names,
        rows=rows,
        total_rows=len(rows),
    )
