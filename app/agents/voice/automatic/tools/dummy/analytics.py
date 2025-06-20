import json
import pytz

from datetime import datetime

from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from app.data.dummy.analytics_data import (
    dummy_juspay_analytics_today,
    dummy_breeze_analytics_today,
    dummy_juspay_analytics_weekly,
    dummy_breeze_analytics_weekly,
)

# Load dummy data
juspay_today = json.loads(dummy_juspay_analytics_today)
breeze_today = json.loads(dummy_breeze_analytics_today)
juspay_weekly = json.loads(dummy_juspay_analytics_weekly)
breeze_weekly = json.loads(dummy_breeze_analytics_weekly)


async def get_sr_success_rate_by_time(params: FunctionCallParams):
    await params.result_callback(juspay_today["overall_success_rate_data"])


async def get_payment_method_wise_sr_by_time(params: FunctionCallParams):
    await params.result_callback(juspay_today["payment_method_success_rates"])


async def get_failure_transactional_data(params: FunctionCallParams):
    await params.result_callback(juspay_today["failure_details"])


async def get_success_transactional_data(params: FunctionCallParams):
    await params.result_callback(juspay_today["success_volume_by_payment_method"])


async def get_gmv_order_value_payment_method_wise(params: FunctionCallParams):
    await params.result_callback(juspay_today["gmv_by_payment_method"])


async def get_average_ticket_payment_wise(params: FunctionCallParams):
    await params.result_callback(juspay_today["average_ticket_size_by_payment_method"])


async def get_weekly_sr_success_rate(params: FunctionCallParams):
    await params.result_callback(juspay_weekly["overall_success_rate_data"])


async def get_weekly_payment_method_wise_sr(params: FunctionCallParams):
    await params.result_callback(juspay_weekly["payment_method_success_rates"])


async def get_weekly_failure_transactional_data(params: FunctionCallParams):
    await params.result_callback(juspay_weekly["failure_details"])


async def get_weekly_success_transactional_data(params: FunctionCallParams):
    await params.result_callback(juspay_weekly["success_volume_by_payment_method"])


async def get_weekly_gmv_order_value_payment_method_wise(
        params: FunctionCallParams):
    await params.result_callback(juspay_weekly["gmv_by_payment_method"])


async def get_weekly_average_ticket_payment_wise(params: FunctionCallParams):
    await params.result_callback(
        juspay_weekly["average_ticket_size_by_payment_method"])


async def get_breeze_daily_sales_breakdown(params: FunctionCallParams):
    await params.result_callback(breeze_today["businessTotalSalesBreakdown"])


async def get_breeze_daily_orders_breakdown(params: FunctionCallParams):
    await params.result_callback(breeze_today["businessTotalOrdersBreakdown"])


async def get_breeze_daily_conversion_breakdown(params: FunctionCallParams):
    await params.result_callback(breeze_today["businessConversionBreakdown"])


async def get_breeze_daily_payment_success_rate(params: FunctionCallParams):
    await params.result_callback(breeze_today["paymentSuccessRate"])


async def get_breeze_daily_average_order_value(params: FunctionCallParams):
    await params.result_callback(breeze_today["averageOrderValue"])


async def get_breeze_weekly_sales_breakdown(params: FunctionCallParams):
    await params.result_callback(breeze_weekly["businessTotalSalesBreakdown"])


async def get_breeze_weekly_orders_breakdown(params: FunctionCallParams):
    await params.result_callback(breeze_weekly["businessTotalOrdersBreakdown"])


async def get_breeze_weekly_conversion_breakdown(params: FunctionCallParams):
    await params.result_callback(breeze_weekly["businessConversionBreakdown"])


async def get_breeze_weekly_payment_success_rate(params: FunctionCallParams):
    await params.result_callback(breeze_weekly["paymentSuccessRate"])


async def get_breeze_weekly_average_order_value(params: FunctionCallParams):
    await params.result_callback(breeze_weekly["averageOrderValue"])


async def get_breeze_weekly_ad_spend_and_roas(params: FunctionCallParams):
    await params.result_callback(breeze_weekly["adSpendAndRoas"])


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

get_weekly_sr_success_rate_function = FunctionSchema(
    name="get_weekly_sr_success_rate",
    description="Calculates weekly overall success rate (SR) for transactions.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

weekly_payment_method_wise_sr_function = FunctionSchema(
    name="get_weekly_payment_method_wise_sr",
    description="Fetches weekly success rate (SR) by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

weekly_failure_transactional_data_function = FunctionSchema(
    name="get_weekly_failure_transactional_data",
    description="Retrieves weekly data for failed transactions.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

weekly_success_transactional_data_function = FunctionSchema(
    name="get_weekly_success_transactional_data",
    description="Retrieves weekly count of successful transactions by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

weekly_gmv_order_value_payment_method_wise_function = FunctionSchema(
    name="get_weekly_gmv_order_value_payment_method_wise",
    description="Retrieves weekly Gross Merchandise Value (GMV) by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

weekly_average_ticket_payment_wise_function = FunctionSchema(
    name="get_weekly_average_ticket_payment_wise",
    description="Calculates weekly average ticket size by payment method.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_daily_sales_breakdown_function = FunctionSchema(
    name="get_breeze_daily_sales_breakdown",
    description="Retrieves the sales breakdown for today.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_daily_orders_breakdown_function = FunctionSchema(
    name="get_breeze_daily_orders_breakdown",
    description="Retrieves the orders breakdown for today.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_daily_conversion_breakdown_function = FunctionSchema(
    name="get_breeze_daily_conversion_breakdown",
    description="Retrieves the conversion breakdown for today.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_daily_payment_success_rate_function = FunctionSchema(
    name="get_breeze_daily_payment_success_rate",
    description="Retrieves the payment success rate for today.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_daily_average_order_value_function = FunctionSchema(
    name="get_breeze_daily_average_order_value",
    description="Retrieves the average order value for today.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_weekly_sales_breakdown_function = FunctionSchema(
    name="get_breeze_weekly_sales_breakdown",
    description="Retrieves the sales breakdown for the week.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_weekly_orders_breakdown_function = FunctionSchema(
    name="get_breeze_weekly_orders_breakdown",
    description="Retrieves the orders breakdown for the week.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_weekly_conversion_breakdown_function = FunctionSchema(
    name="get_breeze_weekly_conversion_breakdown",
    description="Retrieves the conversion breakdown for the week.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_weekly_payment_success_rate_function = FunctionSchema(
    name="get_breeze_weekly_payment_success_rate",
    description="Retrieves the payment success rate for the week.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_weekly_average_order_value_function = FunctionSchema(
    name="get_breeze_weekly_average_order_value",
    description="Retrieves the average order value for the week.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

breeze_weekly_ad_spend_and_roas_function = FunctionSchema(
    name="get_breeze_weekly_ad_spend_and_roas",
    description="Retrieves the ad spend and ROAS for the week.",
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
        get_weekly_sr_success_rate_function,
        weekly_payment_method_wise_sr_function,
        weekly_failure_transactional_data_function,
        weekly_success_transactional_data_function,
        weekly_gmv_order_value_payment_method_wise_function,
        weekly_average_ticket_payment_wise_function,
        breeze_daily_sales_breakdown_function,
        breeze_daily_orders_breakdown_function,
        breeze_daily_conversion_breakdown_function,
        breeze_daily_payment_success_rate_function,
        breeze_daily_average_order_value_function,
        breeze_weekly_sales_breakdown_function,
        breeze_weekly_orders_breakdown_function,
        breeze_weekly_conversion_breakdown_function,
        breeze_weekly_payment_success_rate_function,
        breeze_weekly_average_order_value_function,
        breeze_weekly_ad_spend_and_roas_function,
    ]
)

# A list of all tool functions for easy registration
tool_functions = {
    "get_sr_success_rate_by_time": get_sr_success_rate_by_time,
    "get_payment_method_wise_sr_by_time": get_payment_method_wise_sr_by_time,
    "get_failure_transactional_data": get_failure_transactional_data,
    "get_success_transactional_data": get_success_transactional_data,
    "get_gmv_order_value_payment_method_wise": get_gmv_order_value_payment_method_wise,
    "get_average_ticket_payment_wise": get_average_ticket_payment_wise,
    "get_weekly_sr_success_rate": get_weekly_sr_success_rate,
    "get_weekly_payment_method_wise_sr": get_weekly_payment_method_wise_sr,
    "get_weekly_failure_transactional_data": get_weekly_failure_transactional_data,
    "get_weekly_success_transactional_data": get_weekly_success_transactional_data,
    "get_weekly_gmv_order_value_payment_method_wise": get_weekly_gmv_order_value_payment_method_wise,
    "get_weekly_average_ticket_payment_wise": get_weekly_average_ticket_payment_wise,
    "get_breeze_daily_sales_breakdown": get_breeze_daily_sales_breakdown,
    "get_breeze_daily_orders_breakdown": get_breeze_daily_orders_breakdown,
    "get_breeze_daily_conversion_breakdown": get_breeze_daily_conversion_breakdown,
    "get_breeze_daily_payment_success_rate": get_breeze_daily_payment_success_rate,
    "get_breeze_daily_average_order_value": get_breeze_daily_average_order_value,
    "get_breeze_weekly_sales_breakdown": get_breeze_weekly_sales_breakdown,
    "get_breeze_weekly_orders_breakdown": get_breeze_weekly_orders_breakdown,
    "get_breeze_weekly_conversion_breakdown": get_breeze_weekly_conversion_breakdown,
    "get_breeze_weekly_payment_success_rate": get_breeze_weekly_payment_success_rate,
    "get_breeze_weekly_average_order_value": get_breeze_weekly_average_order_value,
    "get_breeze_weekly_ad_spend_and_roas": get_breeze_weekly_ad_spend_and_roas,
}