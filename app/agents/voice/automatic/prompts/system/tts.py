from app.agents.voice.automatic.types import TTSProvider


def get_tts_based_instructions(tts_provider: TTSProvider | None) -> str:
    """
    Returns TTS-specific instructions.
    """
    if tts_provider == TTSProvider.ELEVENLABS:
        return """
            CURRENCY & NUMBER HANDLING
            Do not include any currency symbols (₹, $, etc.) in your spoken responses.

            For any number with more than two digits, expand it using a **digit-word hybrid format** for natural speech. Say numbers using digits for major units and words for place values.
            - Example: “322” → say “3 hundred 22 rupees”
            - Example: “45,099” → say “45 thousand 99 rupees”
        """
    return ""
