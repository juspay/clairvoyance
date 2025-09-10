from datetime import time, datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum
from app.agents.voice.automatic.types.models import TTSProvider, VoiceName


class OutboundNumberStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    DISABLED = "DISABLED"


class CallProvider(str, Enum):
    TWILIO = "TWILIO"
    EXOTEL = "EXOTEL"

class CallOutcome(str, Enum):
    CONFIRM = "CONFIRM"
    BUSY = "BUSY"
    CANCEL = "CANCEL"
    NO_ANSWER = "NO_ANSWER"

class CallStatus(str, Enum):
    BACKLOG = "backlog"
    FINISHED = "finished"
    ONGOING = "ongoing"
    ERROR = "error"

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
    UNKNOWN = "UNKNOWN"

class LeadCallTracker(BaseModel):
    id: str
    outbound_number_id: Optional[str] = None
    merchant_id: RequestedBy
    workflow: Workflow
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

class CallDataCreate(BaseModel):
    id: str
    outcome: Optional[CallOutcome] = None
    transcription: Optional[Dict[str, Any]] = None
    call_start_time: datetime
    call_end_time: Optional[datetime] = None
    call_id: Optional[str] = None
    provider: str
    status: CallStatus = CallStatus.BACKLOG
    requested_by: RequestedBy
    workflow: Workflow
    call_payload: Optional[Dict[str, Any]] = None
    assigned_number: Optional[str] = None

class CallDataUpdate(BaseModel):
    outcome: Optional[CallOutcome] = None
    status: Optional[CallStatus] = None
    assigned_number: Optional[str] = None

class CallDataResponse(BaseModel):
    id: str
    outcome: Optional[str] = None
    transcription: Optional[Dict[str, Any]] = None
    call_start_time: datetime
    call_end_time: Optional[datetime] = None
    call_id: Optional[str] = None
    provider: str
    status: str
    requested_by: str
    workflow: str
    call_payload: Optional[Dict[str, Any]] = None
    assigned_number: Optional[str] = None
    created_at: datetime
    updated_at: datetime

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

class TokenData(BaseModel):
    """Token data model for JWT payload"""
    user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)
