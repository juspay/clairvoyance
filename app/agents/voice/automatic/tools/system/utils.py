import pytz
from datetime import datetime

from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

async def get_current_time(params: FunctionCallParams):
    timezone_str = params.arguments.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(timezone_str)
        current_time = datetime.now(tz).isoformat()
        await params.result_callback({"time": current_time})
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

tools = ToolsSchema(
    standard_tools=[
        get_current_time_function,
    ]
)

tool_functions = {
    "get_current_time": get_current_time,
}