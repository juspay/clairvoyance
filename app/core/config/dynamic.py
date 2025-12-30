from app.services.live_config.store import get_config

# -----------------------
# Dynamic runtime configs
# -----------------------


async def ENABLE_FAL_SMART_TURN() -> bool:
    """Returns ENABLE_FAL_SMART_TURN from Redis"""
    return await get_config("ENABLE_FAL_SMART_TURN", False, bool)


async def BREEZE_MCP_ENDPOINT_PATH() -> str:
    """Returns BREEZE_MCP_ENDPOINT_PATH from Redis"""
    return await get_config("BREEZE_MCP_ENDPOINT_PATH", "/ai/neurolink", str)


async def ENABLE_BACKGROUND_TASKS() -> bool:
    """Returns ENABLE_BACKGROUND_TASKS from Redis"""
    return await get_config("ENABLE_BACKGROUND_TASKS", "false", bool)


async def DAILY_SUMMARY_HOUR() -> int:
    """Returns DAILY_SUMMARY_HOUR from Redis (24-hour format: 0-23)"""
    return await get_config("DAILY_SUMMARY_HOUR", 21, int)


async def ENABLE_BREEZE_MCP() -> bool:
    """Returns ENABLE_BREEZE_MCP from Redis"""
    return await get_config("ENABLE_BREEZE_MCP", False, bool)


# --- Sarvam Configuration ---
async def SARVAM_STT_MODEL() -> str:
    """Returns SARVAM_STT_MODEL from Redis"""
    return await get_config("SARVAM_STT_MODEL", "saarika:v2.5", str)


async def SARVAM_STT_LANGUAGE_CODE() -> str:
    """Returns SARVAM_STT_LANGUAGE_CODE from Redis"""
    return await get_config("SARVAM_STT_LANGUAGE_CODE", "", str)


async def SARVAM_TTS_LANGUAGE_CODE() -> str:
    """Returns SARVAM_TTS_LANGUAGE_CODE from Redis"""
    return await get_config("SARVAM_TTS_LANGUAGE_CODE", "en-IN", str)


async def SARVAM_STT_PROMPT() -> str:
    """Returns SARVAM_STT_PROMPT from Redis"""
    return await get_config("SARVAM_STT_PROMPT", "", str)


async def SARVAM_STT_VAD_SIGNALS() -> bool:
    """Returns SARVAM_STT_VAD_SIGNALS from Redis"""
    return await get_config("SARVAM_STT_VAD_SIGNALS", True, bool)


async def SARVAM_STT_HIGH_VAD_SENSITIVITY() -> bool:
    """Returns SARVAM_STT_HIGH_VAD_SENSITIVITY from Redis"""
    return await get_config("SARVAM_STT_HIGH_VAD_SENSITIVITY", False, bool)


async def SARVAM_TTS_MODEL() -> str:
    """Returns SARVAM_TTS_MODEL from Redis"""
    return await get_config("SARVAM_TTS_MODEL", "bulbul:v2", str)


async def SARVAM_TTS_VOICE_ID() -> str:
    """Returns SARVAM_TTS_VOICE_ID from Redis"""
    return await get_config("SARVAM_TTS_VOICE_ID", "manisha", str)


async def SARVAM_TTS_PITCH() -> float:
    """Returns SARVAM_TTS_PITCH from Redis"""
    return await get_config("SARVAM_TTS_PITCH", 0.0, float)


async def SARVAM_TTS_PACE() -> float:
    """Returns SARVAM_TTS_PACE from Redis"""
    return await get_config("SARVAM_TTS_PACE", 1.0, float)


# --- Breeze Buddy Sarvam STT Configuration ---
async def BB_SARVAM_STT_MODEL() -> str:
    """Returns BB_SARVAM_STT_MODEL from Redis"""
    return await get_config("BB_SARVAM_STT_MODEL", "saarika:v2.5", str)


async def BB_SARVAM_STT_LANGUAGE_CODE() -> str:
    """Returns BB_SARVAM_STT_LANGUAGE_CODE from Redis"""
    return await get_config("BB_SARVAM_STT_LANGUAGE_CODE", "", str)


async def BB_SARVAM_STT_PROMPT() -> str:
    """Returns BB_SARVAM_STT_PROMPT from Redis"""
    return await get_config("BB_SARVAM_STT_PROMPT", "", str)


async def BB_SARVAM_STT_VAD_SIGNALS() -> bool:
    """Returns BB_SARVAM_STT_VAD_SIGNALS from Redis"""
    return await get_config("BB_SARVAM_STT_VAD_SIGNALS", True, bool)


async def BB_SARVAM_STT_HIGH_VAD_SENSITIVITY() -> bool:
    """Returns BB_SARVAM_STT_HIGH_VAD_SENSITIVITY from Redis"""
    return await get_config("BB_SARVAM_STT_HIGH_VAD_SENSITIVITY", False, bool)


async def BB_SARVAM_TTS_MODEL() -> str:
    """Returns BB_SARVAM_TTS_MODEL from Redis"""
    return await get_config("BB_SARVAM_TTS_MODEL", "bulbul:v2", str)


async def BB_SARVAM_TTS_VOICE_ID() -> str:
    """Returns BB_SARVAM_TTS_VOICE_ID from Redis"""
    return await get_config("BB_SARVAM_TTS_VOICE_ID", "manisha", str)


async def BB_SARVAM_TTS_LANGUAGE_CODE() -> str:
    """Returns BB_SARVAM_TTS_LANGUAGE_CODE from Redis"""
    return await get_config("BB_SARVAM_TTS_LANGUAGE_CODE", "en-IN", str)


async def BB_SARVAM_TTS_PITCH() -> float:
    """Returns BB_SARVAM_TTS_PITCH from Redis"""
    return await get_config("BB_SARVAM_TTS_PITCH", 0.0, float)


async def BB_SARVAM_TTS_PACE() -> float:
    """Returns BB_SARVAM_TTS_PACE from Redis"""
    return await get_config("BB_SARVAM_TTS_PACE", 0.9, float)


async def BB_SARVAM_TTS_ENABLE_PREPROCESSING() -> bool:
    """Returns BB_SARVAM_TTS_ENABLE_PREPROCESSING from Redis"""
    return await get_config("BB_SARVAM_TTS_ENABLE_PREPROCESSING", True, bool)


async def BB_TTS_SERVICE() -> str:
    """Returns BREEZE_BUDDY_TTS_SERVICE from Redis"""
    return await get_config("BREEZE_BUDDY_TTS_SERVICE", "elevenlabs", str)


async def SHOPS_FOR_TEMPLATE_FLOW() -> list[str]:
    """Returns SHOPS_FOR_TEMPLATE_FLOW from Redis as a list of shop identifiers"""
    config_value = await get_config("SHOPS_FOR_TEMPLATE_FLOW", "", str)
    return [shop.strip() for shop in config_value.split(",") if shop.strip()]


# --- Breeze Buddy Text Aggregation Configuration ---
async def BB_TEXT_AGGREGATION_TYPE() -> str:
    """
    Returns BB_TEXT_AGGREGATION_TYPE from Redis.

    Options:
    - "none": Use default Pipecat SimpleTextAggregator (sentence-only)
    - "hybrid": Use HybridTextAggregator (40-char + sentence boundaries)
    - "character_count": Use CharacterCountOnlyAggregator (pure 40-char buffering)

    Default: "hybrid" (recommended for balanced latency and quality)
    """
    return await get_config("BB_TEXT_AGGREGATION_TYPE", "hybrid", str)


async def BB_TEXT_AGGREGATION_MIN_CHARS() -> int:
    """
    Returns BB_TEXT_AGGREGATION_MIN_CHARS from Redis.
    Minimum characters before considering a split (used in hybrid and character_count modes).
    Default: 40 (matching Bolna's buffering)
    """
    return await get_config("BB_TEXT_AGGREGATION_MIN_CHARS", 40, int)


async def BB_TEXT_AGGREGATION_MAX_CHARS() -> int:
    """
    Returns BB_TEXT_AGGREGATION_MAX_CHARS from Redis.
    Maximum characters before forcing a split (safety net).
    Default: 200
    """
    return await get_config("BB_TEXT_AGGREGATION_MAX_CHARS", 200, int)


async def BB_TEXT_AGGREGATION_ENABLE_SENTENCE_DETECTION() -> bool:
    """
    Returns BB_TEXT_AGGREGATION_ENABLE_SENTENCE_DETECTION from Redis.
    Whether to split on sentence boundaries in hybrid mode.
    Default: True (recommended for natural audio)
    """
    return await get_config("BB_TEXT_AGGREGATION_ENABLE_SENTENCE_DETECTION", True, bool)


async def BB_TEXT_AGGREGATION_FIRST_CHUNK_MIN_CHARS() -> int:
    """
    Returns BB_TEXT_AGGREGATION_FIRST_CHUNK_MIN_CHARS from Redis.
    Minimum characters for the first chunk only (ultra-low initial latency).
    Default: 20 (faster initial response, then uses min_chars for subsequent chunks)
    """
    return await get_config("BB_TEXT_AGGREGATION_FIRST_CHUNK_MIN_CHARS", 20, int)
