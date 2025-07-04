from .models import TTSProvider, VoiceName, Mode

def decode_tts_provider(provider: str | None) -> TTSProvider:
    """Decodes the TTS provider string into a TTSProvider enum, with a default fallback."""
    if not provider:
        return TTSProvider.GOOGLE
    try:
        return TTSProvider(provider.upper())
    except ValueError:
        return TTSProvider.GOOGLE

def decode_voice_name(voice: str | None) -> VoiceName:
    """Decodes the voice name string into a VoiceName enum, with a default fallback."""
    if not voice:
        return VoiceName.BRET
    try:
        return VoiceName(voice.upper())
    except ValueError:
        return VoiceName.BRET

def decode_mode(mode: str | None) -> Mode:
    """Decodes the mode string into a Mode enum, with a default fallback to TEST."""
    if not mode:
        return Mode.TEST
    try:
        return Mode(mode.upper())
    except ValueError:
        return Mode.TEST