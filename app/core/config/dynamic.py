from app.services.live_config.store import get_config

# Smart Turn Configuration - These will be loaded lazily to avoid circular imports
ENABLE_FAL_SMART_TURN = get_config("ENABLE_FAL_SMART_TURN", False, bool)
BREEZE_MCP_ENDPOINT_PATH = get_config("BREEZE_MCP_ENDPOINT_PATH", "/ai/neurolink", str)
