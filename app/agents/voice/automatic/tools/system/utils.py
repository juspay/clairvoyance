from datetime import datetime

import pytz
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.core.logger import logger


async def get_current_time(params: FunctionCallParams):
    timezone_str = params.arguments.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(timezone_str)
        current_time = datetime.now(tz).isoformat()
        await params.result_callback({"time": current_time})
    except Exception as e:
        logger.error(f"Tool Error: [get_current_time] Error getting current time: {e}")
        await params.result_callback(
            {"Tool Error: ": f"[get_current_time] Error: {str(e)}"}
        )


async def emit_pending_events(params: FunctionCallParams):
    """
    Simple no-op function to trigger RTVI event emission.

    This function does nothing but provides a trigger for the LLM spy processor
    to call emit_pending_rtvi_events() after function completion, which will
    emit any pending image or other ui-component events to the frontend.
    """
    try:
        logger.info(
            "Emit pending events triggered - RTVI events will be emitted by LLM spy processor"
        )

        await params.result_callback(
            {
                "success": True,
                "message": "Pending RTVI events emission triggered",
                "operation": "emit_pending_events",
            }
        )

    except Exception as e:
        logger.error(f"Error in emit_pending_events: {e}")
        await params.result_callback(
            {"success": False, "error": f"Failed to trigger event emission: {str(e)}"}
        )


get_current_time_function = FunctionSchema(
    name="get_current_time",
    description="Get the current time in a specific timezone.",
    properties={
        "timezone": {
            "type": "string",
            "description": "Timezone (e.g., 'Asia/Kolkata'). Defaults to 'Asia/Kolkata' if not specified.",
        }
    },
    required=[],
)

emit_pending_events_function = FunctionSchema(
    name="emit_pending_events",
    description="Trigger emission of pending RTVI events to the frontend. This is a system function used internally to ensure UI components (like images) are displayed after auto-continue workflows.",
    properties={},
    required=[],
)

# Build tools list conditionally
standard_tools_list = [get_current_time_function, emit_pending_events_function]

tools = ToolsSchema(standard_tools=standard_tools_list)

# Build tool functions dictionary conditionally
tool_functions = {
    "get_current_time": get_current_time,
    "emit_pending_events": emit_pending_events,
}
