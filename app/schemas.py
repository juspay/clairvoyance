from typing import Optional, List
from pydantic import BaseModel
from app.agents.voice.automatic.types.models import TTSProvider, VoiceName

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