"""
Tools Utilities

Helper functions for managing and inspecting available tools.
"""

from typing import Any, Dict

from app.agents.voice.automatic.tools import initialize_tools


def get_available_tools(
    mode: str = "TEST", shop_id: str = None, user_email: str = None
) -> Dict[str, Any]:
    """Get information about available tools."""
    try:
        tools_schema, tool_functions = initialize_tools(
            mode=mode, shop_id=shop_id, user_email=user_email, session_id="debug"
        )

        return {
            "tools_count": len(tools_schema.standard_tools),
            "tool_names": list(tool_functions.keys()),
            "tools": [
                {"name": tool.name, "description": tool.description}
                for tool in tools_schema.standard_tools
            ],
        }
    except Exception as e:
        return {"error": str(e)}
