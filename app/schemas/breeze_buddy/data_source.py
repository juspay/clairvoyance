"""
Pydantic schemas for the data_source REST API.
"""

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints

DataSourceName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")]
DataSourceType = Literal["google_sheet"]
DataSourceFormat = Literal["markdown_table", "csv", "json"]


class DataSourceCreate(BaseModel):
    """Request body for POST /data-sources"""

    reseller_id: str = Field(description="Reseller that owns this data source")
    merchant_id: Optional[str] = Field(
        None,
        description="Scope to a specific merchant. NULL = all merchants of reseller",
    )
    name: DataSourceName = Field(
        description="Human-readable name; also becomes the {variable_name} placeholder"
    )
    source_type: DataSourceType = Field(
        default="google_sheet", description="Currently: 'google_sheet'"
    )
    spreadsheet_url: str = Field(description="Full Google Sheets URL")
    sheet_name: Optional[str] = Field(
        None, description="Tab name. NULL = first tab in spreadsheet"
    )
    columns: Optional[List[str]] = Field(
        None, description="Columns to include. NULL = all columns"
    )
    format: DataSourceFormat = Field(
        default="markdown_table",
        description="Output format: 'markdown_table' | 'csv' | 'json'",
    )
    is_active: bool = Field(default=True)


class DataSourceUpdate(BaseModel):
    """Request body for PUT /data-sources/{id}"""

    name: Optional[DataSourceName] = None
    spreadsheet_url: Optional[str] = None
    sheet_name: Optional[str] = None
    columns: Optional[List[str]] = None
    format: Optional[DataSourceFormat] = None
    is_active: Optional[bool] = None


class DataSourceResponse(BaseModel):
    """Response shape for a single data source"""

    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    name: str
    source_type: DataSourceType
    spreadsheet_url: str
    spreadsheet_id: str
    sheet_name: Optional[str] = None
    columns: Optional[List[str]] = None
    format: DataSourceFormat
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DataSourceListResponse(BaseModel):
    """Response shape for GET /data-sources (paginated)"""

    data_sources: List[DataSourceResponse]
    total: int
    page: int = 1
    limit: int = 50
    total_pages: int = 1


class TabsResponse(BaseModel):
    """Response for GET /data-sources/sheets/tabs"""

    spreadsheet_id: str
    tabs: List[str]


class ColumnsResponse(BaseModel):
    """Response for GET /data-sources/sheets/columns"""

    spreadsheet_id: str
    sheet_name: str
    columns: List[str]


class PreviewResponse(BaseModel):
    """Response for GET /data-sources/sheets/preview"""

    spreadsheet_id: str
    sheet_name: Optional[str] = None
    columns: List[str]
    rows: List[Dict[str, Any]]
    total_rows: int
