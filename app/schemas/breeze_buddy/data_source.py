"""
Pydantic schemas for sheet discovery endpoints (admin-only utility).
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class TabsResponse(BaseModel):
    spreadsheet_id: str
    tabs: List[str]


class ColumnsResponse(BaseModel):
    spreadsheet_id: str
    sheet_name: str
    columns: List[str]


class PreviewResponse(BaseModel):
    spreadsheet_id: str
    sheet_name: Optional[str] = None
    columns: List[str]
    rows: List[Dict[str, Any]]
    total_rows: int
