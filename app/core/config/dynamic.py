from app.services.live_config.store import get_config

# automatic -smart turn
ENABLE_FAL_SMART_TURN = get_config("ENABLE_FAL_SMART_TURN", False, bool)


# automatic - mcp
BREEZE_MCP_ENDPOINT_PATH = get_config("BREEZE_MCP_ENDPOINT_PATH", "/ai/neurolink", str)
