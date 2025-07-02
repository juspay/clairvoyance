import httpx
import json

from datetime import datetime
import pytz
from app.core.logger import logger
from app.core.config import GENIUS_API_URL
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

# This token will be set when the tools are initialized
euler_token: str | None = None


async def _make_genius_api_request(params: FunctionCallParams, payload_details: dict):
    """
    Generic helper to make requests to the Juspay Genius API and return the raw text response.
    """
    if not euler_token:
        logger.error("Juspay tool called without required euler_token.")
        await params.result_callback({"error": "Juspay tool is not configured."})
        return

    start_time_str = params.arguments.get("startTime")
    end_time_str = params.arguments.get("endTime")

    try:
        ist = pytz.timezone("Asia/Kolkata")
        utc = pytz.utc

        # If startTime is not provided, default to the beginning of today in IST.
        if not start_time_str:
            now_ist = datetime.now(ist)
            start_time_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            # Assuming the provided string is in a format that can be parsed,
            # and we'll treat it as IST. For simplicity, this example assumes
            # a 'YYYY-MM-DD HH:MM:SS' format if a string is passed.
            # A more robust solution would handle various formats.
            start_time_ist = ist.localize(datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S'))

        # Convert start time to UTC
        start_time_utc = start_time_ist.astimezone(utc)

        # Handle end time
        if end_time_str:
            end_time_ist = ist.localize(datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S'))
        else:
            # If endTime is not provided, default to the current time in IST.
            end_time_ist = datetime.now(ist)
        
        # Convert end time to UTC
        end_time_utc = end_time_ist.astimezone(utc)

        # Format to ISO string with 'Z' required by the API
        start_time_iso = start_time_utc.isoformat().replace('+00:00', 'Z')
        end_time_iso = end_time_utc.isoformat().replace('+00:00', 'Z')

    except Exception as e:
        logger.error(f"Error converting time for Juspay API: {e}")
        await params.result_callback({"error": f"Invalid time format provided. Please use 'YYYY-MM-DD HH:MM:SS' in IST. Error: {e}"})
        return

    full_payload = {
        **payload_details,
        "interval": {"start": start_time_iso, "end": end_time_iso},
    }

    headers = {
        'Content-Type': 'application/json',
        'x-web-logintoken': euler_token,
        "user-agent": "ClairvoyanceApp/1.0"
    }

    logger.info(f"Requesting Juspay Genius API with payload: {json.dumps(full_payload)}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(GENIUS_API_URL, json=full_payload, headers=headers)
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
            
            # Return the raw text response as requested
            response_text = response.text
            logger.info(f"Raw Juspay API response: {response_text}")
            logger.info(f"Received Juspay API text response. Length: {len(response_text)}")
            await params.result_callback({"data": response_text})

    except httpx.TimeoutException:
        logger.error("Juspay API request timed out after 10 seconds.")
        await params.result_callback({"error": "It is taking so much time. Please try again."})
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error calling Juspay API: {e.response.status_code} - {e.response.text}")
        await params.result_callback({"error": f"Juspay API error: {e.response.status_code}", "details": e.response.text})
    except Exception as e:
        logger.error(f"Unexpected error calling Juspay API: {e}")
        await params.result_callback({"error": f"An unexpected error occurred: {e}"})


async def get_sr_success_rate_by_time(params: FunctionCallParams):
    try:
        logger.info(f"Fetching real-time SR success rate with params: {params.arguments}")
        payload_details = {
            "dimensions": [],
            "domain": "kvorders",
            "metric": "success_rate"
        }
        await _make_genius_api_request(params, payload_details)
    except Exception as e:
        logger.error(f"Critical error in get_sr_success_rate_by_time: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


async def get_payment_method_wise_sr_by_time(params: FunctionCallParams):
    try:
        logger.info(f"Fetching real-time payment method-wise SR with params: {params.arguments}")
        payload_details = {
            "dimensions": ["payment_method_type"],
            "domain": "kvorders",
            "metric": "success_rate"
        }
        await _make_genius_api_request(params, payload_details)
    except Exception as e:
        logger.error(f"Critical error in get_payment_method_wise_sr_by_time: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


async def get_failure_transactional_data_by_time(params: FunctionCallParams):
    try:
        logger.info(f"Fetching real-time failure data with params: {params.arguments}")
        payload_details = {
            "dimensions": ["error_message", "payment_method_type"],
            "domain": "kvorders",
            "filters": {
                "and": {
                    "left": {"condition": "NotIn", "field": "error_message", "val": [None]},
                    "right": {"condition": "In", "field": "error_message", "val": {"limit": 20, "sortedOn": {"ordering": "Desc", "sortDimension": "order_with_transactions"}}}
                }
            },
            "metric": "order_with_transactions"
        }
        await _make_genius_api_request(params, payload_details)
    except Exception as e:
        logger.error(f"Critical error in get_failure_transactional_data_by_time: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


async def get_success_transactional_data_by_time(params: FunctionCallParams):
    try:
        logger.info(f"Fetching real-time success data with params: {params.arguments}")
        payload_details = {
            "dimensions": ["payment_method_type"],
            "domain": "kvorders",
            "filters": {"condition": "In", "field": "payment_status", "val": ["SUCCESS"]},
            "metric": "success_volume"
        }
        await _make_genius_api_request(params, payload_details)
    except Exception as e:
        logger.error(f"Critical error in get_success_transactional_data_by_time: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


async def get_gmv_order_value_payment_method_wise_by_time(params: FunctionCallParams):
    try:
        logger.info(f"Fetching real-time GMV with params: {params.arguments}")
        payload_details = {
            "dimensions": ["payment_method_type"],
            "domain": "kvorders",
            "metric": "total_amount"
        }
        await _make_genius_api_request(params, payload_details)
    except Exception as e:
        logger.error(f"Critical error in get_gmv_order_value_payment_method_wise_by_time: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


async def get_average_ticket_payment_wise_by_time(params: FunctionCallParams):
    try:
        logger.info(f"Fetching real-time average ticket size with params: {params.arguments}")
        payload_details = {
            "dimensions": ["payment_method_type"],
            "domain": "kvorders",
            "metric": "avg_ticket_size"
        }
        await _make_genius_api_request(params, payload_details)
    except Exception as e:
        logger.error(f"Critical error in get_average_ticket_payment_wise_by_time: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


time_input_schema = {
    "type": "object",
    "properties": {
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. This is mandatory.",
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. Defaults to the current time if not provided.",
        },
    },
    "required": ["startTime", "endTime"],
}

get_sr_success_rate_function = FunctionSchema(
    name="get_sr_success_rate_by_time",
    description="Get the overall payment success rate for all transactions within a specified time range. Use this to understand the general health of the payment system.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

payment_method_wise_sr_function = FunctionSchema(
    name="get_payment_method_wise_sr_by_time",
    description="Get the payment success rate for each payment method (e.g., UPI, Cards, Netbanking) within a specified time range. Use this to identify which payment methods are performing well or poorly.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

failure_transactional_data_function = FunctionSchema(
    name="get_failure_transactional_data_by_time",
    description="Get a list of the top transaction failure reasons and the payment methods they occurred on within a specified time range. Use this to diagnose the most common payment issues.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

success_transactional_data_function = FunctionSchema(
    name="get_success_transactional_data_by_time",
    description="Get the total count of successful transactions for each payment method within a specified time range. Use this to see which payment methods are most popular.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

gmv_order_value_payment_method_wise_function = FunctionSchema(
    name="get_gmv_order_value_payment_method_wise_by_time",
    description="Get the total Gross Merchandise Value (GMV) for each payment method within a specified time range. The results can be summed to calculate the total payment method GMV/sales. Use this to understand the revenue contribution of each payment method and the overall sales performance.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

average_ticket_payment_wise_function = FunctionSchema(
    name="get_average_ticket_payment_wise_by_time",
    description="Get the average transaction value (ticket size) for each payment method within a specified time range. Use this to analyze customer spending habits across different payment options.",
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
    "get_failure_transactional_data_by_time": get_failure_transactional_data_by_time,
    "get_success_transactional_data_by_time": get_success_transactional_data_by_time,
    "get_gmv_order_value_payment_method_wise_by_time": get_gmv_order_value_payment_method_wise_by_time,
    "get_average_ticket_payment_wise_by_time": get_average_ticket_payment_wise_by_time,
}