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


# --- Breeze Buddy Cartesia TTS Configuration ---
async def BB_CARTESIA_VOICE_ID() -> str:
    """Returns BB_CARTESIA_VOICE_ID from Redis"""
    return await get_config(
        "BB_CARTESIA_VOICE_ID", "bec003e2-3cb3-429c-8468-206a393c67ad", str
    )


async def BB_CARTESIA_MODEL() -> str:
    """Returns BB_CARTESIA_MODEL from Redis"""
    return await get_config("BB_CARTESIA_MODEL", "sonic-3", str)


async def BB_CARTESIA_LANGUAGE() -> str:
    """Returns BB_CARTESIA_LANGUAGE from Redis (language code like 'en', 'es', 'hi')"""
    return await get_config("BB_CARTESIA_LANGUAGE", None, str)


async def BB_CARTESIA_GENERATION_VOLUME() -> float:
    """Returns BB_CARTESIA_GENERATION_VOLUME from Redis (range: 0.5-2.0)"""
    return await get_config("BB_CARTESIA_GENERATION_VOLUME", 1.5, float)


async def BB_CARTESIA_GENERATION_SPEED() -> float:
    """Returns BB_CARTESIA_GENERATION_SPEED from Redis (range: 0.6-1.5)"""
    return await get_config("BB_CARTESIA_GENERATION_SPEED", 1.0, float)


async def BB_CARTESIA_GENERATION_EMOTION() -> str:
    """Returns BB_CARTESIA_GENERATION_EMOTION from Redis"""
    return await get_config("BB_CARTESIA_GENERATION_EMOTION", "neutral", str)


async def BB_CARTESIA_AGGREGATE_SENTENCES() -> bool:
    """Returns BB_CARTESIA_AGGREGATE_SENTENCES from Redis"""
    return await get_config("BB_CARTESIA_AGGREGATE_SENTENCES", True, bool)


async def SHOPS_FOR_TEMPLATE_FLOW() -> list[str]:
    """Returns SHOPS_FOR_TEMPLATE_FLOW from Redis as a list of shop identifiers"""
    config_value = await get_config("SHOPS_FOR_TEMPLATE_FLOW", "", str)
    return [shop.strip() for shop in config_value.split(",") if shop.strip()]


# --- Breeze Buddy Azure LLM Configuration ---
async def BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS() -> int:
    """Returns BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS from Redis"""
    return await get_config("BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS", 50, int)


async def BREEZE_BUDDY_AZURE_TEMPERATURE() -> float:
    """Returns BREEZE_BUDDY_AZURE_TEMPERATURE from Redis"""
    return await get_config("BREEZE_BUDDY_AZURE_TEMPERATURE", 0.7, float)


async def BREEZE_BUDDY_LLM_AGGREGATION_TIMEOUT() -> float:
    """Returns BREEZE_BUDDY_LLM_AGGREGATION_TIMEOUT from Redis (0.0 = send immediately)"""
    return await get_config("BREEZE_BUDDY_LLM_AGGREGATION_TIMEOUT", 0.0, float)


# --- Daily Mode VAD Configuration (for web/mobile frontends) ---
async def BB_DAILY_VAD_CONFIDENCE() -> float:
    """Returns BB_DAILY_VAD_CONFIDENCE from Redis"""
    return await get_config("BB_DAILY_VAD_CONFIDENCE", 0.9, float)


async def BB_DAILY_VAD_START_SECS() -> float:
    """Returns BB_DAILY_VAD_START_SECS from Redis"""
    return await get_config("BB_DAILY_VAD_START_SECS", 0.25, float)


async def BB_DAILY_VAD_STOP_SECS() -> float:
    """Returns BB_DAILY_VAD_STOP_SECS from Redis"""
    return await get_config("BB_DAILY_VAD_STOP_SECS", 0.95, float)


async def BB_DAILY_VAD_MIN_VOLUME() -> float:
    """Returns BB_DAILY_VAD_MIN_VOLUME from Redis"""
    return await get_config("BB_DAILY_VAD_MIN_VOLUME", 0.75, float)
