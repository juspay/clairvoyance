"""
Utility module for filtering tools based on environment configuration and user authorization.
"""

import re
from typing import Dict, List, Any
from app.core.config import WRITE_ACTIONS
from app.core.config import WRITE_ACTIONS_AUTHORIZED_USERS
from app.core.logger import logger

def is_user_authorized(email: str) -> bool:
    if not WRITE_ACTIONS_AUTHORIZED_USERS:
        return True

    if not email:
        return False
    
    return email in WRITE_ACTIONS_AUTHORIZED_USERS

unauthorized_error_message = "User is not authorized to perform this action."

def should_include_write_tools(email: str | None = None) -> bool:
    if is_user_authorized(email):
        logger.info(f"User {email} is authorized - including write tools")
        return True
    else:
        logger.info(f"User {email} is not authorized - excluding write tools")
        return False

def is_write_tool(tool_name: str) -> bool:
    if not tool_name:
        return False

    if tool_name in WRITE_ACTIONS:
        logger.info(f"Tool '{tool_name}' is not available for the given user")
        return True
    
    return False

def filter_tools_by_authorization(tools: List[Any], tool_functions: Dict[str, Any], 
                                email: str | None = None) -> tuple[List[Any], Dict[str, Any]]:
    if should_include_write_tools(email):
        return tools, tool_functions
    
    filtered_tools = []
    filtered_tool_functions = {}
    
    for tool in tools:
        tool_name = getattr(tool, 'name', None)
        if tool_name and not is_write_tool(tool_name):
            filtered_tools.append(tool)
        elif tool_name:
            logger.info(f"Excluding write tool schema: {tool_name}")
        else:
            # If tool has no name, include it (shouldn't happen but be safe)
            filtered_tools.append(tool)
    
    for tool_name, func in tool_functions.items():
        if not is_write_tool(tool_name):
            filtered_tool_functions[tool_name] = func
        else:
            logger.info(f"Excluding write tool function: {tool_name}")
    
    logger.info(f"Filtered tools: {len(filtered_tools)} out of {len(tools)} tools included")
    logger.info(f"Filtered Tools: {filtered_tools}")
    logger.info(f"Filtered Tool Functions: {filtered_tool_functions}")
    return filtered_tools, filtered_tool_functions
