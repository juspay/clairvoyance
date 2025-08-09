from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from app.agents.voice.automatic.types.models import TTSProvider, VoiceName

class CallOutcome(str, Enum):
    CONFIRM = "CONFIRM"
    BUSY = "BUSY"
    CANCEL = "CANCEL"

class CallStatus(str, Enum):
    BACKLOG = "backlog"
    FINISHED = "finished"
    ONGOING = "ongoing"
    ERROR = "error"

class RequestedBy(str, Enum):
    BREEZE = "breeze"
    SHOPIFY = "shopify"

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
    call_payload: Optional[Dict[str, Any]] = None
    assigned_number: Optional[str] = None
    created_at: str
    updated_at: str

class AutomaticVoiceTTSServiceConfig(BaseModel):
    ttsProvider: TTSProvider
    voiceName: VoiceName

class AutomaticVoiceUserConnectRequest(BaseModel):
    mode: Optional[str] = None
    eulerToken: Optional[str] = None
    breezeToken: Optional[str] = None
    shopUrl: Optional[str] = None
    shopId: Optional[str] = None
    shopType: Optional[str] = None
    userName: Optional[str] = None
    ttsService: Optional[AutomaticVoiceTTSServiceConfig] = None
    merchantId: Optional[str] = None
    platformIntegrations: Optional[List[str]] = None
