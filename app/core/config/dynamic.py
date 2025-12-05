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
