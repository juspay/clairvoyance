"""
Schemas package - organized by agent and use case.

This package contains all Pydantic models for API requests/responses,
organized hierarchically:
- breeze_buddy/: Breeze Buddy agent schemas
  - auth.py: Authentication & authorization models
  - analytics.py: Analytics request/response models
  - core.py: Core domain models (leads, numbers, configs)
"""

from app.schemas.breeze_buddy.analytics import (
    AnalyticsFilters,
    AnalyticsOptions,
    AnalyticsRequest,
    AnalyticsResponse,
    AnalyticsType,
    CallBasedAnalyticsResult,
    CallDetailGroupedResult,
    CallDetailResult,
    PaginationInfo,
    TelephonyNumberStat,
    TimeGranularity,
    TrendDataPoint,
)

# Re-export commonly used schemas for backward compatibility
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
    UserInDB,
    UserInfo,
    UserRole,
)
from app.schemas.breeze_buddy.connection import BreezeBuddyDailyConnectRequest
from app.schemas.breeze_buddy.core import (
    IVR_OPTIONS_TEMPLATE,
    TEMPLATELESS_PLACEHOLDER_TEMPLATES,
    UNKNOWN_TEMPLATE,
    BlacklistedNumber,
    CallDirection,
    CallExecutionConfig,
    CallProvider,
    CreateBlacklistNumberRequest,
    CreateCallExecutionConfigRequest,
    CreateTelephonyNumberRequest,
    ExecutionMode,
    InboundBlockAction,
    LeadCallStatus,
    LeadCallTracker,
    PreCheckConfig,
    PreCheckDefaultAction,
    PreCheckHttpRequest,
    PreCheckMatchType,
    PreCheckResponseConfig,
    PreCheckType,
    TelephonyConfig,
    TelephonyNumber,
    TelephonyNumberStatus,
    UpdateCallExecutionConfigRequest,
    UpdateTelephonyNumberRequest,
)
from app.schemas.breeze_buddy.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    UpdateCredentialRequest,
)

# Import UserCreate/UserUpdate from users.py (with proper validation)
# Note: auth.py has legacy versions without validation - use these instead
from app.schemas.breeze_buddy.users import (
    DeleteUserResponse,
    UserCreate,
    UserUpdate,
)
from app.schemas.feature_flags import (
    FeatureFlagDeleteResponse,
    FeatureFlagResponse,
    FeatureFlagUpdate,
    FeatureFlagUpdateResponse,
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
    "CallDetailGroupedResult",
    "TelephonyNumberStat",
    "PaginationInfo",
    "TimeGranularity",
    "TrendDataPoint",
    # Core
    "IVR_OPTIONS_TEMPLATE",
    "TEMPLATELESS_PLACEHOLDER_TEMPLATES",
    "UNKNOWN_TEMPLATE",
    "BlacklistedNumber",
    "CallDirection",
    "CallExecutionConfig",
    "CallProvider",
    "CreateBlacklistNumberRequest",
    "CreateCallExecutionConfigRequest",
    "CreateTelephonyNumberRequest",
    "UpdateTelephonyNumberRequest",
    "ExecutionMode",
    "InboundBlockAction",
    "LeadCallStatus",
    "LeadCallTracker",
    "TelephonyNumber",
    "TelephonyNumberStatus",
    "PreCheckConfig",
    "PreCheckDefaultAction",
    "PreCheckHttpRequest",
    "PreCheckMatchType",
    "PreCheckResponseConfig",
    "PreCheckType",
    "TelephonyConfig",
    "UpdateCallExecutionConfigRequest",
    # Credentials
    "CreateCredentialRequest",
    "Credential",
    "CredentialType",
    "UpdateCredentialRequest",
    # Connection
    "BreezeBuddyDailyConnectRequest",
    # User Management
    "DeleteUserResponse",
    # Feature Flags
    "FeatureFlagDeleteResponse",
    "FeatureFlagResponse",
    "FeatureFlagUpdate",
    "FeatureFlagUpdateResponse",
]
