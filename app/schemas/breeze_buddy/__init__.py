"""Breeze Buddy agent schemas."""

from app.schemas.breeze_buddy.analytics import (
    AnalyticsFilters,
    AnalyticsOptions,
    AnalyticsRequest,
    AnalyticsResponse,
    AnalyticsType,
    CallBasedAnalyticsResult,
    CallDetailResult,
    PaginationInfo,
    TelephonyNumberStat,
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
    CreateTelephonyNumberRequest,
    LeadCallStatus,
    LeadCallTracker,
    TelephonyConfig,
    TelephonyNumber,
    TelephonyNumberStatus,
    UpdateCallExecutionConfigRequest,
    UpdateTelephonyNumberRequest,
)
from app.schemas.breeze_buddy.merchants import (
    MerchantCreate,
    MerchantListResponse,
    MerchantResponse,
    MerchantUpdate,
)
from app.schemas.breeze_buddy.template import (
    TemplateListResponse,
    TemplateMetadata,
)
from app.schemas.breeze_buddy.users import (
    DeleteUserResponse,
    UserCreate as UserAccountCreate,
    UserListResponse,
    UserResponse,
    UserUpdate as UserAccountUpdate,
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
    "TelephonyNumberStat",
    "PaginationInfo",
    "TimeGranularity",
    "TrendDataPoint",
    # Core
    "CallExecutionConfig",
    "CallProvider",
    "CreateCallExecutionConfigRequest",
    "CreateTelephonyNumberRequest",
    "UpdateTelephonyNumberRequest",
    "LeadCallStatus",
    "LeadCallTracker",
    "TelephonyNumber",
    "TelephonyNumberStatus",
    "TelephonyConfig",
    "UpdateCallExecutionConfigRequest",
    # Template
    "TemplateListResponse",
    "TemplateMetadata",
    # Merchant Entities
    "MerchantCreate",
    "MerchantListResponse",
    "MerchantResponse",
    "MerchantUpdate",
    # User Accounts
    "UserAccountCreate",
    "UserAccountUpdate",
    "UserListResponse",
    "UserResponse",
    "DeleteUserResponse",
]
