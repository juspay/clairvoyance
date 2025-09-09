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

class CallDataCreate(BaseModel):
    id: str
    outcome: Optional[CallOutcome] = None
    transcription: Optional[Dict[str, Any]] = None
    call_start_time: str
    call_end_time: Optional[str] = None
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
    call_start_time: str
    call_end_time: Optional[str] = None
    call_id: Optional[str] = None
    provider: str
    status: str
    requested_by: str
    workflow: str
    call_payload: Optional[Dict[str, Any]] = None
    assigned_number: Optional[str] = None
    created_at: str
    updated_at: str


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
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


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
