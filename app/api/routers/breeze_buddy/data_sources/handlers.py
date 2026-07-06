"""Discovery handlers for sheet exploration (admin-only)."""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas.breeze_buddy.auth import UserInfo
from app.schemas.breeze_buddy.data_source import (
    ColumnsResponse,
    PreviewResponse,
    TabsResponse,
)
from app.services.google.sheets import (
    extract_spreadsheet_id,
    fetch_sheet_data,
    get_column_headers,
    list_tabs,
)


def _log_caller(spreadsheet_id: str, current_user: UserInfo) -> None:
    logger.info(
        "Sheet discovery: spreadsheet_id=%s user=%s resellers=%s",
        spreadsheet_id,
        current_user.id,
        current_user.reseller_ids,
    )


async def list_tabs_handler(
    spreadsheet_url: str, current_user: UserInfo
) -> TabsResponse:
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
    if not spreadsheet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google Sheets URL",
        )
    _log_caller(spreadsheet_id, current_user)
    tabs = await list_tabs(spreadsheet_id)
    return TabsResponse(spreadsheet_id=spreadsheet_id, tabs=tabs)


async def list_columns_handler(
    spreadsheet_url: str,
    sheet_name: Optional[str] = None,
) -> ColumnsResponse:
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
    if not spreadsheet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google Sheets URL",
        )
    logger.info(
        "Listing columns for spreadsheet_id=%s tab=%s",
        spreadsheet_id,
        sheet_name or "(default)",
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
    spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
    if not spreadsheet_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google Sheets URL",
        )
    logger.info(
        "Previewing spreadsheet_id=%s tab=%s cols=%s rows=%d",
        spreadsheet_id,
        sheet_name or "(default)",
        len(columns) if columns else "all",
        max_rows,
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
