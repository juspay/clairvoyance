"""Pydantic schemas for Breeze Buddy data sources."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class DataSourceType(str, Enum):
    """Supported data-source adapters."""

    GOOGLE_SHEET = "google_sheet"


class DataSource(BaseModel):
    """Reusable external dataset definition."""

    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    name: str
    source_type: DataSourceType
    config: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateDataSourceRequest(BaseModel):
    """Request to create a data source."""

    reseller_id: str = Field(min_length=1)
    merchant_id: Optional[str] = None
    name: str = Field(min_length=1)
    source_type: DataSourceType = DataSourceType.GOOGLE_SHEET
    config: Dict[str, Any] = Field(default_factory=dict)


class UpdateDataSourceRequest(BaseModel):
    """Request to update a data source."""

    name: Optional[str] = Field(default=None, min_length=1)
    merchant_id: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
