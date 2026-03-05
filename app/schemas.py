"""
DEPRECATED: This module is kept for backward compatibility only.

Please use the new organized schema structure:
- app.schemas.breeze_buddy.auth - Authentication & authorization models
- app.schemas.breeze_buddy.analytics - Analytics request/response models
- app.schemas.breeze_buddy.core - Core domain models (leads, numbers, configs)
- app.schemas.automatic_voice.connection - Automatic Voice connection schemas
"""

from app.schemas.automatic_voice.connection import (
    AutomaticVoiceTTSServiceConfig,
    AutomaticVoiceUserConnectRequest,
)

# Re-export all schemas from the new organized structure for backward compatibility
# This allows existing code to continue using `from app.schemas import XYZ`
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
    BlacklistedNumber,
    CallExecutionConfig,
    CallProvider,
    CreateBlacklistNumberRequest,
    CreateCallExecutionConfigRequest,
    CreateOutboundNumberRequest,
    LeadCallStatus,
    LeadCallTracker,
    OutboundNumber,
    OutboundNumberStatus,
    UpdateCallExecutionConfigRequest,
)

__all__ = [
    "AnalyticsFilters",
    "AnalyticsOptions",
    "AnalyticsRequest",
    "AnalyticsResponse",
    "AnalyticsType",
    "AuthTokenData",
    "AutomaticVoiceTTSServiceConfig",
    "AutomaticVoiceUserConnectRequest",
    "BlacklistedNumber",
    "CallBasedAnalyticsResult",
    "CallDetailResult",
    "CallExecutionConfig",
    "CallProvider",
    "CreateBlacklistNumberRequest",
    "CreateCallExecutionConfigRequest",
    "CreateOutboundNumberRequest",
    "LeadCallStatus",
    "LeadCallTracker",
    "LoginRequest",
    "LoginResponse",
    "OutboundNumber",
    "OutboundNumberStat",
    "OutboundNumberStatus",
    "PaginationInfo",
    "Permission",
    "S2STokenRequest",
    "S2STokenResponse",
    "TimeGranularity",
    "TokenData",
    "TokenResponse",
    "TrendDataPoint",
    "UpdateCallExecutionConfigRequest",
    "User",
    "UserCreate",
    "UserInDB",
    "UserInfo",
    "UserRole",
    "UserUpdate",
]
