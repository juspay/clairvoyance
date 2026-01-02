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
    CallExecutionConfig,
    CallProvider,
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
    "CallBasedAnalyticsResult",
    "CallDetailResult",
    "CallExecutionConfig",
    "CallProvider",
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
from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.ai.voice.agents.automatic.types.models import TTSProvider, VoiceName


class OutboundNumberStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    DISABLED = "DISABLED"


class CallProvider(str, Enum):
    TWILIO = "TWILIO"
    EXOTEL = "EXOTEL"


class LeadCallStatus(str, Enum):
    BACKLOG = "BACKLOG"
    PROCESSING = "PROCESSING"
    FINISHED = "FINISHED"
    RETRY = "RETRY"


class LeadCallTracker(BaseModel):
    id: str
    outbound_number_id: Optional[str] = None
    merchant_id: str
    template: str
    shop_identifier: Optional[str] = None
    request_id: Optional[str] = None
    attempt_count: int = 0
    next_attempt_at: Optional[datetime] = None
    payload: Optional[Dict[str, Any]] = None
    metaData: Optional[Dict[str, Any]] = None
    recording_url: Optional[str] = None
    status: LeadCallStatus = LeadCallStatus.BACKLOG
    outcome: Optional[str] = None
    call_id: Optional[str] = None
    call_initiated_time: Optional[datetime] = None
    call_end_time: Optional[datetime] = None
    cost: Optional[float] = None
    is_locked: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateOutboundNumberRequest(BaseModel):
    number: str
    provider: CallProvider
    status: OutboundNumberStatus = OutboundNumberStatus.AVAILABLE
    maximum_channels: Optional[int] = None


class OutboundNumber(BaseModel):
    id: str
    number: str
    provider: CallProvider
    status: OutboundNumberStatus
    channels: Optional[int] = None
    maximum_channels: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateCallExecutionConfigRequest(BaseModel):
    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    merchant_id: str
    workflow: str
    shop_identifier: Optional[str] = None
    enable_international_call: bool = True


class UpdateCallExecutionConfigRequest(BaseModel):
    merchant_id: str
    workflow: str
    shop_identifier: Optional[str] = None
    initial_offset: Optional[int] = None
    retry_offset: Optional[int] = None
    call_start_time: Optional[time] = None
    call_end_time: Optional[time] = None
    max_retry: Optional[int] = None
    calling_provider: Optional[CallProvider] = None
    enable_international_call: Optional[bool] = None


class CallExecutionConfig(BaseModel):
    id: str
    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    merchant_id: str
    template: str
    shop_identifier: Optional[str] = None
    enable_international_call: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AutomaticVoiceTTSServiceConfig(BaseModel):
    ttsProvider: TTSProvider
    voiceName: VoiceName


class AutomaticVoiceUserConnectRequest(BaseModel):
    sessionId: Optional[str] = None
    mode: Optional[str] = None
    eulerToken: Optional[str] = None
    breezeToken: Optional[str] = None
    shopUrl: Optional[str] = None
    shopId: Optional[str] = None
    shopType: Optional[str] = None
    userName: Optional[str] = None
    email: Optional[str] = None
    ttsService: Optional[AutomaticVoiceTTSServiceConfig] = None
    merchantId: Optional[str] = None
    platformIntegrations: Optional[List[str]] = None
    resellerId: Optional[str] = None
    customerId: Optional[str] = None
    shopifyConnectedShop: Optional[str] = None


class BreezeBuddyDailyConnectRequest(BaseModel):
    """Request model for Breeze Buddy Daily transport connection."""

    call_sid: Optional[str] = Field(
        None, description="Unique identifier for the call/session"
    )


class TokenData(BaseModel):
    """Token data model for JWT payload"""

    user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)
