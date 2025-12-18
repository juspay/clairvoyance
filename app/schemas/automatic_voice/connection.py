"""Automatic Voice connection and configuration schemas."""

from typing import List, Optional

from pydantic import BaseModel

from app.ai.voice.agents.automatic.types.models import TTSProvider, VoiceName


class AutomaticVoiceTTSServiceConfig(BaseModel):
    """TTS service configuration for Automatic Voice agent"""
    ttsProvider: TTSProvider
    voiceName: VoiceName


class AutomaticVoiceUserConnectRequest(BaseModel):
    """User connection request for Automatic Voice agent"""
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
