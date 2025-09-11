
from app.agents.voice.breeze_buddy.services.telephony.exotel.exotel import ExotelProvider
from app.agents.voice.breeze_buddy.services.telephony.twilio.twillio import TwilioProvider
from app.agents.voice.breeze_buddy.services.telephony.base_provider import VoiceCallProvider

def get_voice_provider(provider_name: str, aiohttp_session) -> VoiceCallProvider:
    if provider_name == "EXOTEL":
        return ExotelProvider(aiohttp_session)
    if provider_name == "TWILIO":
        return TwilioProvider(aiohttp_session)
    raise ValueError(f"Unsupported voice provider: {provider_name}")
