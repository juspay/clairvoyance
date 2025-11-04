from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.agents.voice.automatic.types.models import TTSProvider, VoiceName


class OutboundNumberStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    DISABLED = "DISABLED"


class CallProvider(str, Enum):
    TWILIO = "TWILIO"
    EXOTEL = "EXOTEL"


class RequestedBy(str, Enum):
    BREEZE = "breeze"
    SHOPIFY = "shopify"


class Workflow(str, Enum):
    ORDER_CONFIRMATION = "order-confirmation"


class LeadCallStatus(str, Enum):
    BACKLOG = "BACKLOG"
    PROCESSING = "PROCESSING"
    FINISHED = "FINISHED"
    RETRY = "RETRY"


class LeadCallOutcome(str, Enum):
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"
    CANCEL = "CANCEL"
    CONFIRM = "CONFIRM"
    ADDRESS_UPDATED = "ADDRESS_UPDATED"
    UNKNOWN = "UNKNOWN"


class LeadCallTracker(BaseModel):
    id: str
    outbound_number_id: Optional[str] = None
    merchant_id: RequestedBy
    workflow: Workflow
    shop_identifier: Optional[str] = None
    attempt_count: int = 0
    next_attempt_at: Optional[datetime] = None
    payload: Optional[Dict[str, Any]] = None
    metaData: Optional[Dict[str, Any]] = None
    recording_url: Optional[str] = None
    status: LeadCallStatus = LeadCallStatus.BACKLOG
    outcome: Optional[LeadCallOutcome] = None
    call_id: Optional[str] = None
    call_initiated_time: Optional[datetime] = None
    call_end_time: Optional[datetime] = None
    cost: Optional[float] = None
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
    workflow: Workflow
    shop_identifier: Optional[str] = None
    enable_international_call: bool = True


class CallExecutionConfig(BaseModel):
    id: str
    initial_offset: int
    retry_offset: int
    call_start_time: time
    call_end_time: time
    max_retry: int
    calling_provider: CallProvider
    merchant_id: str
    workflow: Workflow
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


class TokenData(BaseModel):
    """Token data model for JWT payload"""

    user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class Conversation(BaseModel):
    """Conversation model"""

    id: str
    session_id: str
    client_sid: Optional[str] = None
    merchant_id: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    shop_id: Optional[str] = None
    shop_url: Optional[str] = None
    reseller_id: Optional[str] = None
    mode: Optional[str] = None
    status: ConversationStatus = ConversationStatus.ACTIVE
    summary: Optional[str] = None
    message_count: int = 0
    started_at: datetime
    last_activity_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


class ConversationMessage(BaseModel):
    """Conversation message model"""

    id: str
    conversation_id: str
    role: MessageRole
    content: str
    sequence_number: int
    timestamp: datetime
    created_at: datetime
