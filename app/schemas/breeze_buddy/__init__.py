"""Breeze Buddy agent schemas."""

from app.schemas.breeze_buddy.analytics import (
    AnalyticsFilters,
    AnalyticsOptions,
    AnalyticsRequest,
    AnalyticsResponse,
    AnalyticsType,
    CallBasedAnalyticsResult,
    CallDetailResult,
    OutboundNumberStat,
    PaginationInfo,
    TimeGranularity,
    TrendDataPoint,
)
from app.schemas.breeze_buddy.auth import (
    AuthTokenData,
    LoginRequest,
    LoginResponse,
    Permission,
    S2STokenRequest,
    S2STokenResponse,
    TokenData,
    TokenResponse,
    User,
    UserCreate,
    UserInDB,
    UserInfo,
    UserRole,
    UserUpdate,
)
from app.schemas.breeze_buddy.core import (
    CallExecutionConfig,
    CallProvider,
    CreateCallExecutionConfigRequest,
    CreateOutboundNumberRequest,
    LeadCallStatus,
    LeadCallTracker,
    OutboundNumber,
    OutboundNumberStatus,
    TelephonyConfig,
    UpdateCallExecutionConfigRequest,
)
from app.schemas.breeze_buddy.merchants import MerchantsResponse
from app.schemas.breeze_buddy.template import (
    TemplateListResponse,
    TemplateMetadata,
)

__all__ = [
    # Auth
    "AuthTokenData",
    "LoginRequest",
    "LoginResponse",
    "Permission",
    "S2STokenRequest",
    "S2STokenResponse",
    "TokenData",
    "TokenResponse",
    "User",
    "UserCreate",
    "UserInDB",
    "UserInfo",
    "UserRole",
    "UserUpdate",
    # Analytics
    "AnalyticsFilters",
    "AnalyticsOptions",
    "AnalyticsRequest",
    "AnalyticsResponse",
    "AnalyticsType",
    "CallBasedAnalyticsResult",
    "CallDetailResult",
    "OutboundNumberStat",
    "PaginationInfo",
    "TimeGranularity",
    "TrendDataPoint",
    # Core
    "CallExecutionConfig",
    "CallProvider",
    "CreateCallExecutionConfigRequest",
    "CreateOutboundNumberRequest",
    "LeadCallStatus",
    "LeadCallTracker",
    "OutboundNumber",
    "OutboundNumberStatus",
    "TelephonyConfig",
    "UpdateCallExecutionConfigRequest",
    # Template
    "TemplateListResponse",
    "TemplateMetadata",
    # Merchants
    "MerchantsResponse",
]
