from typing import Dict, List, Any, Optional
from app.core.config import (
    AUTOMATIC_ACTIONS_REQUIRE_AUTH,
    AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS,
)
from app.core.logger import logger


def is_user_authorized_for_actions(email: Optional[str]) -> bool:
    if not AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS:
        return True
    return bool(email and email in AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS)


def is_tool_actionable(tool_name: Optional[str]) -> bool:
    return bool(tool_name and tool_name in AUTOMATIC_ACTIONS_REQUIRE_AUTH)


def filter_tools_by_authorization(
    tools: List[Any], tool_functions: Dict[str, Any], email: Optional[str] = None
) -> tuple[List[Any], Dict[str, Any]]:
    if is_user_authorized_for_actions(email):
        return tools, tool_functions

    filtered_tools = [
        tool for tool in tools if not is_tool_actionable(getattr(tool, "name", None))
    ]
    filtered_tool_functions = {
        name: fn for name, fn in tool_functions.items() if not is_tool_actionable(name)
    }

    logger.info(
        f"User {email} is not authorized for write tools. Filtered {len(tools) - len(filtered_tools)} tools."
    )
    return filtered_tools, filtered_tool_functions
