"""
Default MCP configuration settings for Breeze Buddy.

These settings can be overridden per-template via the MCPConfiguration
in the template's configurations field.
"""

from app.core.config.static import MCP_CLIENT_TIMEOUT

DEFAULT_MCP_TIMEOUT = MCP_CLIENT_TIMEOUT
