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
from app.schemas.breeze_buddy.whatsapp import (
    WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME,
    SyncMerchantWhatsAppConnection,
    WhatsAppCredentialSecret,
)
from app.schemas.breeze_buddy.connectors import (
    Connector,
    ConnectorMetric,
    ConnectorMetricIncrement,
    ConnectorStatus,
    UpsertConnectorConnection,
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
    # Connectors
    "Connector",
    "ConnectorMetric",
    "ConnectorMetricIncrement",
    "ConnectorStatus",
    "UpsertConnectorConnection",
    # WhatsApp
    "SyncMerchantWhatsAppConnection",
    "WhatsAppCredentialSecret",
    "WHATSAPP_SYNC_TOKEN_ENCRYPTION_SCHEME",
]
