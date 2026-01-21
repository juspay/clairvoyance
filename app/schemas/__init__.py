"""
Schemas package - organized by agent and use case.

This package contains all Pydantic models for API requests/responses,
organized hierarchically:
- breeze_buddy/: Breeze Buddy agent schemas
  - auth.py: Authentication & authorization models
  - analytics.py: Analytics request/response models
  - core.py: Core domain models (leads, numbers, configs)
- automatic_voice/: Automatic Voice agent schemas
"""

from app.schemas.automatic_voice.connection import (
    AutomaticVoiceTTSServiceConfig,
    AutomaticVoiceUserConnectRequest,
)
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
    UserCreate,
    UserInDB,
    UserInfo,
    UserRole,
    UserUpdate,
)
from app.schemas.breeze_buddy.connection import BreezeBuddyDailyConnectRequest
from app.schemas.breeze_buddy.core import (
    CallDirection,
    CallExecutionConfig,
    CallProvider,
    CreateCallExecutionConfigRequest,
    CreateOutboundNumberRequest,
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
    OutboundNumber,
    OutboundNumberStatus,
    UpdateCallExecutionConfigRequest,
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
    "CallDirection",
    "CallExecutionConfig",
    "CallProvider",
    "CreateCallExecutionConfigRequest",
    "CreateOutboundNumberRequest",
    "ExecutionMode",
    "LeadCallStatus",
    "LeadCallTracker",
    "OutboundNumber",
    "OutboundNumberStatus",
    "UpdateCallExecutionConfigRequest",
    # Connection
    "BreezeBuddyDailyConnectRequest",
    # Automatic Voice
    "AutomaticVoiceTTSServiceConfig",
    "AutomaticVoiceUserConnectRequest",
]
