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
    return await get_config("BB_SARVAM_TTS_PACE", 1.0, float)


async def BB_TTS_SERVICE() -> str:
    """Returns BREEZE_BUDDY_TTS_SERVICE from Redis"""
    return await get_config("BREEZE_BUDDY_TTS_SERVICE", "elevenlabs", str)
