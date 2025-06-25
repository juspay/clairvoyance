import datetime
import pytz

from app.core.logger import logger

# ---- Function Declarations ----
get_current_time_declaration = {
    "name": "getCurrentTime",
    "description": "Get the current time in a specific timezone",
    "parameters": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "The timezone to get current time in (e.g., 'Asia/Kolkata', 'America/New_York'). Default is 'Asia/Kolkata' (India)"
            }
        },
        "required": []
    }
}

# ---- Tool Implementation Functions ----
def get_current_time(timezone="Asia/Kolkata"):
    """
    Get the current time in the specified timezone.
    """
    logger.info(f"SystemTool: getCurrentTime function called with timezone: {timezone}")
    try:
        tz = pytz.timezone(timezone)
        current_time = datetime.datetime.now(tz)
        logger.info(f"SystemTool: getCurrentTime result: {current_time.isoformat()}")
        return current_time.isoformat()
    except Exception as e:
        logger.error(f"SystemTool: Error in getCurrentTime: {e}")
        return f"Error: {str(e)}"

# ---- Rich Tool Definitions ----
system_tools_definitions = [
    {
        "declaration": get_current_time_declaration,
        "function": get_current_time,
        "required_context_params": [] # No extra context needed for this system tool
    }
    # Add more system tool definitions here in the future
]

__all__ = ["system_tools_definitions"]