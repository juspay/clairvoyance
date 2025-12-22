"""Analytics schemas for Breeze Buddy."""

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class AnalyticsType(str, Enum):
    """Types of analytics queries supported"""

    SUMMARY = "summary"
    CALL_DETAILS = "call-details"
    LEAD_BASED = "lead-based"
    OUTBOUND_NUMBERS = "outbound-numbers"
    TRENDS = "trends"
    CONVERSION = "conversion"
    PERFORMANCE = "performance"


class TimeGranularity(str, Enum):
    """Time granularity for trend aggregation"""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class AnalyticsFilters(BaseModel):
    """Filters for analytics queries - all filters applied with AND logic"""

    template: Optional[str] = Field(
        None, description="Filter by template name (e.g., 'order-confirmation')"
    )
    shop_identifier: Optional[str] = Field(
        None, description="Filter by single shop identifier"
    )
    shop_identifiers: Optional[List[str]] = Field(
        None, description="Filter by multiple shop identifiers"
    )
    merchant_id: Optional[str] = Field(None, description="Filter by merchant ID")
    merchant_ids: Optional[List[str]] = Field(
        None, description="Filter by multiple merchant IDs"
    )
    status: Optional[str] = Field(
        None, description="Filter by call status (completed, failed, etc.)"
    )
    outcome: Optional[str] = Field(None, description="Filter by call outcome")
    request_id: Optional[str] = Field(None, description="Filter by request ID")
    date_from: Optional[date] = Field(
        None, description="Filter from date (ISO format: YYYY-MM-DD)"
    )
    date_to: Optional[date] = Field(
        None, description="Filter to date (ISO format: YYYY-MM-DD)"
    )
    call_duration_min: Optional[int] = Field(
        None, description="Minimum call duration in seconds", ge=0
    )
    call_duration_max: Optional[int] = Field(
        None, description="Maximum call duration in seconds", ge=0
    )
    customer_sentiment: Optional[str] = Field(
        None, description="Filter by sentiment (positive, neutral, negative)"
    )
    payload_filters: Optional[Dict[str, Any]] = Field(
        None,
        description="Filter by payload fields (e.g., {'shop_name': 'My Shop', 'customer_name': 'John'})",
    )


class AnalyticsOptions(BaseModel):
    """Options for formatting and paginating analytics results"""

    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    limit: int = Field(default=50, ge=1, le=1000, description="Items per page")
    group_by: Optional[str] = Field(
        None,
        description="Group results by field (template, shop_identifier, date, etc.)",
    )
    time_granularity: TimeGranularity = Field(
        default=TimeGranularity.DAY,
        description="Time aggregation granularity for trends",
    )
    sort_by: Optional[str] = Field(default="created_at", description="Field to sort by")
    sort_order: Literal["asc", "desc"] = Field(
        default="desc", description="Sort direction"
    )


class AnalyticsRequest(BaseModel):
    """Request model for analytics endpoint"""

    type: AnalyticsType = Field(..., description="Type of analytics to return")
    filters: AnalyticsFilters = Field(
        default_factory=AnalyticsFilters, description="Filters to apply (AND logic)"
    )
    options: AnalyticsOptions = Field(
        default_factory=AnalyticsOptions,
        description="Pagination and formatting options",
    )


class PaginationInfo(BaseModel):
    """Pagination information for analytics results"""

    page: int
    limit: int
    total: int
    total_pages: int


class SummaryAnalyticsResult(BaseModel):
    """Summary analytics result model"""

    total_calls: int
    completed_calls: int
    failed_calls: int
    success_rate: float
    average_duration: Optional[float] = None
    total_templates: Optional[int] = None
    total_shops: Optional[int] = None


class CallDetailResult(BaseModel):
    """Individual call detail result"""

    call_id: str
    lead_id: str
    order_id: Optional[str] = None
    template: str
    merchant_id: str
    shop_identifier: Optional[str] = None
    shop_name: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_mobile_number: Optional[str] = None
    status: str
    outcome: Optional[str] = None
    duration: Optional[int] = None  # seconds
    recording_url: Optional[str] = None
    transcript: Optional[str] = None
    calling_provider: Optional[str] = None
    attempt_count: Optional[int] = None
    cost: Optional[float] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class TrendDataPoint(BaseModel):
    """Single data point in trend analytics"""

    date: Optional[str] = None  # For daily trends
    week: Optional[str] = None  # For weekly trends (ISO format: 2025-W44)
    week_start: Optional[str] = None
    week_end: Optional[str] = None
    month: Optional[str] = None  # For monthly trends (YYYY-MM)
    month_name: Optional[str] = None
    total_calls: int
    average_duration: Optional[float] = None
    success_rate: Optional[float] = None


class OutboundNumberStat(BaseModel):
    """Statistics for a single outbound number"""

    number: str
    provider: str
    total_calls: int
    calls_picked: int
    calls_no_answer: int


class AnalyticsResponse(BaseModel):
    """Generic analytics response model"""

    success: bool = True
    data: Dict[str, Any] = Field(..., description="Analytics data payload")
    error: Optional[str] = None
