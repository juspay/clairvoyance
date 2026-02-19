from typing import Optional

from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.exotel.exotel import (
    ExotelProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo import (
    PlivoProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.twilio.twilio import (
    TwilioProvider,
)
from app.schemas import CallProvider, TelephonyConfig


def get_voice_provider(
    provider_name: CallProvider,
    aiohttp_session,
    telephony_config: Optional[TelephonyConfig] = None,
) -> VoiceCallProvider:
    if provider_name == CallProvider.EXOTEL:
        return ExotelProvider(aiohttp_session, telephony_config)
    if provider_name == CallProvider.TWILIO:
        return TwilioProvider(aiohttp_session, telephony_config)
    if provider_name == CallProvider.PLIVO:
        return PlivoProvider(aiohttp_session, telephony_config)
    raise ValueError(f"Unsupported voice provider: {provider_name}")
