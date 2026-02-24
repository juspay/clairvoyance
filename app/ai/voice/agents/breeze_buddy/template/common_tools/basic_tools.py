"""
Basic Tools

Common utility tools for date, time, and day of week.
All tools use IST timezone (UTC+5:30).

Tools provided:
    - get_current_datetime: Full date, time, day, timezone
    - get_current_date: Date and day of week
    - get_current_time: Time and timezone
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.template.common_tools.registry import (
    CommonTool,
    CommonToolRegistry,
    ToolCategory,
)
from app.core.logger import logger

# IST timezone: UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))


def register_basic_tools() -> None:
    """
    Register all BASIC category tools.

    Called automatically when the common_tools package is imported.
    """
    # Tool 1: get_current_datetime
    CommonToolRegistry.register(
        CommonTool(
            name="get_current_datetime",
            description=(
                "Get the current date, time, and day of the week in IST timezone. "
                "Use this when the user asks what today's date is, what time it is, "
                "or what day of the week it is."
            ),
            handler=_get_current_datetime_handler,
            category=ToolCategory.BASIC,
        )
    )

    # Tool 2: get_current_date
    CommonToolRegistry.register(
        CommonTool(
            name="get_current_date",
            description=(
                "Get today's date and the day of the week in IST timezone. "
                "Use this when the user asks about today's date or what day it is."
            ),
            handler=_get_current_date_handler,
            category=ToolCategory.BASIC,
        )
    )

    # Tool 3: get_current_time
    CommonToolRegistry.register(
        CommonTool(
            name="get_current_time",
            description=(
                "Get the current time in IST timezone. "
                "Use this when the user asks what time it is right now."
            ),
            handler=_get_current_time_handler,
            category=ToolCategory.BASIC,
        )
    )


# ---------------------------------------------------------------------------
# Tool Handlers
#
# Signature: async def handler(args, flow_manager) -> tuple[result_dict, None]
# Returns (result_dict, None) - None means no node transition
# ---------------------------------------------------------------------------


async def _get_current_datetime_handler(
    args: Dict[str, Any], flow_manager: Any
) -> tuple:
    """Return current date, time, day of week, and timezone in IST."""
    _ = flow_manager  # Unused but required by signature
    now = datetime.now(_IST)
    result = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "day_of_week": now.strftime("%A"),
        "timezone": "IST (Asia/Kolkata)",
    }
    logger.info("[COMMON_TOOL] get_current_datetime called")
    return (result, None)


async def _get_current_date_handler(args: Dict[str, Any], flow_manager: Any) -> tuple:
    """Return current date and day of week in IST."""
    _ = flow_manager  # Unused but required by signature
    now = datetime.now(_IST)
    result = {
        "date": now.strftime("%Y-%m-%d"),
        "day_of_week": now.strftime("%A"),
    }
    logger.info("[COMMON_TOOL] get_current_date called")
    return (result, None)


async def _get_current_time_handler(args: Dict[str, Any], flow_manager: Any) -> tuple:
    """Return current time in IST."""
    _ = flow_manager  # Unused but required by signature
    now = datetime.now(_IST)
    result = {
        "time": now.strftime("%H:%M:%S"),
        "timezone": "IST (Asia/Kolkata)",
    }
    logger.info("[COMMON_TOOL] get_current_time called")
    return (result, None)
