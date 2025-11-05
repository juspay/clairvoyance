from app.ai.voice.agents.automatic.types import TTSProvider


def get_tts_based_instructions(tts_provider: TTSProvider | None) -> str:
    """
    Returns TTS-specific instructions.
    """

    general_instructions = """
    PERCENTAGE HANDLING
    For percentages with decimals, ALWAYS replace the decimal symbol (.) with the word "point" in your actual text output. TTS engines cannot properly pronounce decimal symbols.
    - Example: "83.5%" → say "eighty-three point five percent"
    - Example: "12.75%" → say "twelve point seventy-five percent"
    - Example: "0.65%" → say "point sixty-five percent"
    - Example: "5%" → say "five percent"
    - Example: "95%" → say "ninety-five percent"
    """

    provider_specific_instructions = ""
    if tts_provider == TTSProvider.ELEVENLABS:
        provider_specific_instructions = """
            CURRENCY & NUMBER HANDLING
            Do not include any currency symbols (₹, $, etc.) in your spoken responses.

            For any number with more than two digits, expand it using a **digit-word hybrid format** for natural speech. Say numbers using digits for major units and words for place values.
            - Example: "322" → say "3 hundred 22 rupees"
            - Example: "45,099" → say "45 thousand 99 rupees"
        """

    return general_instructions + provider_specific_instructions
