from loguru import logger

from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from app.api.juspay_metrics import (
    get_success_rate,
    get_payment_method_wise_sr,
    get_failure_transactional_data,
    get_success_transactional_data,
    get_gmv_order_value_payment_method_wise,
    get_average_ticket_payment_wise,
)

# This token will be set when the tools are initialized
euler_token = None


async def get_sr_success_rate_by_time(params: FunctionCallParams):
    logger.info(f"Fetching real-time SR success rate with params: {params.arguments}")
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")
    result = await get_success_rate(euler_token, start_time, end_time)
    await params.result_callback(result)


async def get_payment_method_wise_sr_by_time(params: FunctionCallParams):
    logger.info(f"Fetching real-time payment method-wise SR with params: {params.arguments}")
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")
    result = await get_payment_method_wise_sr(euler_token, start_time, end_time)
    await params.result_callback(result)


async def get_failure_transactional_data_live(params: FunctionCallParams):
    logger.info(f"Fetching real-time failure data with params: {params.arguments}")
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")
    result = await get_failure_transactional_data(euler_token, start_time, end_time)
    await params.result_callback(result)


async def get_success_transactional_data_live(params: FunctionCallParams):
    logger.info(f"Fetching real-time success data with params: {params.arguments}")
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")
    result = await get_success_transactional_data(euler_token, start_time, end_time)
    await params.result_callback(result)


async def get_gmv_order_value_payment_method_wise_live(params: FunctionCallParams):
    logger.info(f"Fetching real-time GMV with params: {params.arguments}")
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")
    result = await get_gmv_order_value_payment_method_wise(euler_token, start_time, end_time)
    await params.result_callback(result)


async def get_average_ticket_payment_wise_live(params: FunctionCallParams):
    logger.info(f"Fetching real-time average ticket size with params: {params.arguments}")
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")
    result = await get_average_ticket_payment_wise(euler_token, start_time, end_time)
    await params.result_callback(result)


time_input_schema = {
    "type": "object",
    "properties": {
        "startTime": {
            "type": "string",
            "description": "Start time in ISO format (e.g., 2023-01-01T00:00:00Z)",
        },
        "endTime": {
            "type": "string",
            "description": "End time in ISO format (e.g., 2023-01-01T01:00:00Z)",
        },
    },
    "required": ["startTime", "endTime"],
}

get_sr_success_rate_function = FunctionSchema(
    name="get_sr_success_rate_by_time",
    description="Calculates overall success rate (SR) for transactions.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

payment_method_wise_sr_function = FunctionSchema(
    name="get_payment_method_wise_sr_by_time",
    description="Fetches success rate (SR) by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

failure_transactional_data_function = FunctionSchema(
    name="get_failure_transactional_data",
    description="Retrieves data for failed transactions.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

success_transactional_data_function = FunctionSchema(
    name="get_success_transactional_data",
    description="Retrieves count of successful transactions by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

gmv_order_value_payment_method_wise_function = FunctionSchema(
    name="get_gmv_order_value_payment_method_wise",
    description="Retrieves Gross Merchandise Value (GMV) by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

average_ticket_payment_wise_function = FunctionSchema(
    name="get_average_ticket_payment_wise",
    description="Calculates average ticket size by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

tools = ToolsSchema(
    standard_tools=[
        get_sr_success_rate_function,
        payment_method_wise_sr_function,
        failure_transactional_data_function,
        success_transactional_data_function,
        gmv_order_value_payment_method_wise_function,
        average_ticket_payment_wise_function,
    ]
)

tool_functions = {
    "get_sr_success_rate_by_time": get_sr_success_rate_by_time,
    "get_payment_method_wise_sr_by_time": get_payment_method_wise_sr_by_time,
    "get_failure_transactional_data": get_failure_transactional_data_live,
    "get_success_transactional_data": get_success_transactional_data_live,
    "get_gmv_order_value_payment_method_wise": get_gmv_order_value_payment_method_wise_live,
    "get_average_ticket_payment_wise": get_average_ticket_payment_wise_live,
}