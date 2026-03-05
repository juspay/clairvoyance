"""
Get Current Time Handler

Returns the current time in Indian Standard Time (IST).
A simple built-in global function for reference and utility.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger

# IST is UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


async def get_current_time(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Return the current time in Indian Standard Time (IST).

    Args:
        context: Handler context with bot state access
        args: LLM function arguments (not used)

    Returns:
        Dict with current time details in IST
    """
    now_ist = datetime.now(IST)

    logger.info(f"[get_current_time] Returning IST time for call {context.call_sid}")

    return {
        "status": "success",
        "current_time": now_ist.strftime("%I:%M %p"),
        "date": now_ist.strftime("%A, %B %d, %Y"),
        "datetime_iso": now_ist.isoformat(),
        "timezone": "IST (UTC+5:30)",
    }
