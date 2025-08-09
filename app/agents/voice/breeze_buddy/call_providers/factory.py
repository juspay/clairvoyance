from app.agents.voice.breeze_buddy.call_providers.main import VoiceCallProvider
from app.agents.voice.breeze_buddy.call_providers.exotel import ExotelProvider
from app.agents.voice.breeze_buddy.call_providers.twillio import TwilioProvider

def get_voice_provider(provider_name: str, aiohttp_session) -> VoiceCallProvider:
    if provider_name == "exotel":
        return ExotelProvider(aiohttp_session)
    if provider_name == "twillio":
        return TwilioProvider(aiohttp_session)
    raise ValueError(f"Unsupported voice provider: {provider_name}")