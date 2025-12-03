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
