"""
ACME Analytics Tools
Specialized tools for ACME store with start/end time parameters
"""

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.agents.voice.automatic.data.dummy.acme import breeze_parser, juspay_parser
from app.core.logger import logger


# Time input schema for all ACME tools
time_input_schema = {
    "type": "object",
    "properties": {
        "startTime": {
            "type": "string",
            "description": "Start time in ISO format (e.g., 2023-01-01T00:00:00Z). Optional - defaults to today if not provided.",
        },
        "endTime": {
            "type": "string",
            "description": "End time in ISO format (e.g., 2023-01-01T01:00:00Z). Optional - defaults to today if not provided.",
        },
    },
    "required": [],  # Make time parameters optional
}


# =============================================================================
# ACME BREEZE TOOLS
# =============================================================================

async def get_acme_sales_breakdown(params: FunctionCallParams):
    """Get ACME sales breakdown with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME sales breakdown for time range: {start_time} to {end_time}")

    result = breeze_parser.get_sales_breakdown(start_time, end_time)
    await params.result_callback(result)


async def get_acme_orders_breakdown(params: FunctionCallParams):
    """Get ACME orders breakdown with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME orders breakdown for time range: {start_time} to {end_time}")

    result = breeze_parser.get_orders_breakdown(start_time, end_time)
    await params.result_callback(result)


async def get_acme_conversion_breakdown(params: FunctionCallParams):
    """Get ACME conversion breakdown with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME conversion breakdown for time range: {start_time} to {end_time}")

    result = breeze_parser.get_conversion_breakdown(start_time, end_time)
    await params.result_callback(result)


async def get_acme_payment_success_rate(params: FunctionCallParams):
    """Get ACME payment success rate with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME payment success rate for time range: {start_time} to {end_time}")

    result = breeze_parser.get_payment_success_rate(start_time, end_time)
    await params.result_callback(result)


async def get_acme_average_order_value(params: FunctionCallParams):
    """Get ACME average order value with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME average order value for time range: {start_time} to {end_time}")

    result = breeze_parser.get_average_order_value(start_time, end_time)
    await params.result_callback(result)


# =============================================================================
# ACME JUSPAY TOOLS
# =============================================================================

async def get_acme_juspay_success_rate(params: FunctionCallParams):
    """Get ACME Juspay success rate with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME Juspay success rate for time range: {start_time} to {end_time}")

    result = juspay_parser.get_success_rate(start_time, end_time)
    await params.result_callback(result)


async def get_acme_juspay_payment_method_sr(params: FunctionCallParams):
    """Get ACME Juspay payment method success rates with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME Juspay payment method SR for time range: {start_time} to {end_time}")

    result = juspay_parser.get_payment_method_sr(start_time, end_time)
    await params.result_callback(result)


async def get_acme_juspay_success_transactional_data(params: FunctionCallParams):
    """Get ACME Juspay success transactional data with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME Juspay success transactional data for time range: {start_time} to {end_time}")

    result = juspay_parser.get_success_transactional_data(start_time, end_time)
    await params.result_callback(result)


async def get_acme_juspay_failure_transactional_data(params: FunctionCallParams):
    """Get ACME Juspay failure transactional data with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME Juspay failure transactional data for time range: {start_time} to {end_time}")

    result = juspay_parser.get_failure_transactional_data(start_time, end_time)
    await params.result_callback(result)


async def get_acme_juspay_gmv_by_payment_method(params: FunctionCallParams):
    """Get ACME Juspay GMV by payment method with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME Juspay GMV by payment method for time range: {start_time} to {end_time}")

    result = juspay_parser.get_gmv_by_payment_method(start_time, end_time)
    await params.result_callback(result)


async def get_acme_juspay_average_ticket_size(params: FunctionCallParams):
    """Get ACME Juspay average ticket size with time range support"""
    start_time = params.arguments.get("startTime")
    end_time = params.arguments.get("endTime")

    logger.info(f"Retrieving ACME Juspay average ticket size for time range: {start_time} to {end_time}")

    result = juspay_parser.get_average_ticket_size(start_time, end_time)
    await params.result_callback(result)


# =============================================================================
# TOOL SCHEMAS
# =============================================================================

# Breeze tool schemas
acme_sales_breakdown_function = FunctionSchema(
    name="get_acme_sales_breakdown",
    description="Get ACME store sales breakdown data for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_orders_breakdown_function = FunctionSchema(
    name="get_acme_orders_breakdown",
    description="Get ACME store orders breakdown data for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_conversion_breakdown_function = FunctionSchema(
    name="get_acme_conversion_breakdown",
    description="Get ACME store conversion breakdown data for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_payment_success_rate_function = FunctionSchema(
    name="get_acme_payment_success_rate",
    description="Get ACME store payment success rate for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_average_order_value_function = FunctionSchema(
    name="get_acme_average_order_value",
    description="Get ACME store average order value for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

# Juspay tool schemas
acme_juspay_success_rate_function = FunctionSchema(
    name="get_acme_juspay_success_rate",
    description="Get ACME store Juspay success rate for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_juspay_payment_method_sr_function = FunctionSchema(
    name="get_acme_juspay_payment_method_sr",
    description="Get ACME store Juspay payment method success rates for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_juspay_success_transactional_function = FunctionSchema(
    name="get_acme_juspay_success_transactional_data",
    description="Get ACME store Juspay success transactional data for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_juspay_failure_transactional_function = FunctionSchema(
    name="get_acme_juspay_failure_transactional_data",
    description="Get ACME store Juspay failure transactional data for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_juspay_gmv_function = FunctionSchema(
    name="get_acme_juspay_gmv_by_payment_method",
    description="Get ACME store Juspay GMV by payment method for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

acme_juspay_ticket_size_function = FunctionSchema(
    name="get_acme_juspay_average_ticket_size",
    description="Get ACME store Juspay average ticket size for specified time range",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)


# Collect all ACME tools
acme_tools = ToolsSchema(
    standard_tools=[
        # Breeze tools
        acme_sales_breakdown_function,
        acme_orders_breakdown_function,
        acme_conversion_breakdown_function,
        acme_payment_success_rate_function,
        acme_average_order_value_function,

        # Juspay tools
        acme_juspay_success_rate_function,
        acme_juspay_payment_method_sr_function,
        acme_juspay_success_transactional_function,
        acme_juspay_failure_transactional_function,
        acme_juspay_gmv_function,
        acme_juspay_ticket_size_function,
    ]
)

# Tool functions mapping
acme_tool_functions = {
    # Breeze functions
    "get_acme_sales_breakdown": get_acme_sales_breakdown,
    "get_acme_orders_breakdown": get_acme_orders_breakdown,
    "get_acme_conversion_breakdown": get_acme_conversion_breakdown,
    "get_acme_payment_success_rate": get_acme_payment_success_rate,
    "get_acme_average_order_value": get_acme_average_order_value,

    # Juspay functions
    "get_acme_juspay_success_rate": get_acme_juspay_success_rate,
    "get_acme_juspay_payment_method_sr": get_acme_juspay_payment_method_sr,
    "get_acme_juspay_success_transactional_data": get_acme_juspay_success_transactional_data,
    "get_acme_juspay_failure_transactional_data": get_acme_juspay_failure_transactional_data,
    "get_acme_juspay_gmv_by_payment_method": get_acme_juspay_gmv_by_payment_method,
    "get_acme_juspay_average_ticket_size": get_acme_juspay_average_ticket_size,
}