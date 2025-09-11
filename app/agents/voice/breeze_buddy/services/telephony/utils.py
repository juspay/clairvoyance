
from app.agents.voice.breeze_buddy.services.telephony.exotel.exotel import ExotelProvider
from app.agents.voice.breeze_buddy.services.telephony.twilio.twilio import TwilioProvider
from app.agents.voice.breeze_buddy.services.telephony.base_provider import VoiceCallProvider
from app.schemas import CallProvider

def get_voice_provider(provider_name: CallProvider, aiohttp_session) -> VoiceCallProvider:
    if provider_name == CallProvider.EXOTEL:
        return ExotelProvider(aiohttp_session)
    if provider_name == CallProvider.TWILIO:
        return TwilioProvider(aiohttp_session)
    raise ValueError(f"Unsupported voice provider: {provider_name}")
