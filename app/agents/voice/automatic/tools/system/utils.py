import pytz
from datetime import datetime

from app.core.logger import logger
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import ManuallySwitchServiceFrame
from pipecat.processors.frame_processor import FrameDirection

async def get_current_time(params: FunctionCallParams):
    timezone_str = params.arguments.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(timezone_str)
        current_time = datetime.now(tz).isoformat()
        await params.result_callback({"time": current_time})
    except Exception as e:
        await params.result_callback({"error": str(e)})


async def change_language(params: FunctionCallParams, context: dict = None):
    try:
        ctx = context or params.context
        stt_switcher = ctx["stt_switcher"]
        logger.info("Switching STT service language")
        await stt_switcher.push_frame(
            ManuallySwitchServiceFrame(service=stt_switcher.services[1]),
            direction=FrameDirection.UPSTREAM,
        )
        await params.result_callback({"status": "OK"})
    except Exception as e:
        await params.result_callback({"error": str(e)})


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

change_language_function = FunctionSchema(
    name="change_language",
    description="Change the language of the speech-to-text service.",
    properties={},
    required=[],
)

# Build tools list conditionally
standard_tools_list = [get_current_time_function, change_language_function]

tools = ToolsSchema(standard_tools=standard_tools_list)

# Build tool functions dictionary conditionally
tool_functions = {
    "get_current_time": get_current_time,
    "change_language": change_language,
}
