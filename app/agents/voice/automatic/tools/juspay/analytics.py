import httpx
import json
import functools

from datetime import datetime
import pytz
from app.core.logger import logger
from app.core.config import GENIUS_API_URL
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from app.agents.voice.automatic.types.models import ApiFailure, ApiSuccess, GeniusApiResponse

# This token will be set when the tools are initialized
euler_token: str | None = None

def format_indian_currency(amount):
    """Formats a number into Indian currency style with commas."""
    s = str(amount)
    if len(s) <= 3:
        return s
    last_three = s[-3:]
    other_numbers = s[:-3]
    formatted_other_numbers = ""
    while other_numbers:
        if len(other_numbers) > 2:
            formatted_other_numbers = other_numbers[-2:] + "," + formatted_other_numbers
            other_numbers = other_numbers[:-2]
        else:
            formatted_other_numbers = other_numbers + "," + formatted_other_numbers
            other_numbers = ""
    return formatted_other_numbers + last_three


async def _make_genius_api_request(params: FunctionCallParams, payload_details: dict) -> GeniusApiResponse:
    """
    Generic helper to make requests to the Juspay Genius API.
    Returns a GeniusApiResponse object.
    """
    if not euler_token:
        logger.error("Juspay tool called without required euler_token.")
        return ApiFailure(error={"error": "Juspay tool is not configured."})

    start_time_str = params.arguments.get("startTime")
    end_time_str = params.arguments.get("endTime")

    try:
        ist = pytz.timezone("Asia/Kolkata")
        utc = pytz.utc
        if not start_time_str:
            now_ist = datetime.now(ist)
            start_time_ist = now_ist.replace(
                hour=0, minute=0, second=0, microsecond=0)
        else:
            start_time_ist = ist.localize(
                datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S'))
        start_time_utc = start_time_ist.astimezone(utc)

        if end_time_str:
            end_time_ist = ist.localize(
                datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S'))
        else:
            end_time_ist = datetime.now(ist)
        end_time_utc = end_time_ist.astimezone(utc)

        start_time_iso = start_time_utc.isoformat().replace('+00:00', 'Z')
        end_time_iso = end_time_utc.isoformat().replace('+00:00', 'Z')

    except Exception as e:
        logger.error(f"Error converting time for Juspay API: {e}")
        return ApiFailure(error={"error": f"Invalid time format provided. Please use 'YYYY-MM-DD HH:MM:SS' in IST. Error: {e}"})

    full_payload = {
        **payload_details,
        "interval": {"start": start_time_iso, "end": end_time_iso},
    }
    headers = {
        'Content-Type': 'application/json',
        'x-web-logintoken': euler_token,
        "user-agent": "ClairvoyanceApp/1.0"
    }

    logger.info(
        f"Requesting Juspay Genius API with payload: {json.dumps(full_payload)}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(GENIUS_API_URL, json=full_payload, headers=headers)
            response.raise_for_status()
            response_text = response.text
            logger.info(
                f"Received Raw Juspay API text response: {response_text}")
            return ApiSuccess(data=response_text)
    except httpx.TimeoutException:
        logger.error("Juspay API request timed out after 10 seconds.")
        return ApiFailure(error={"error": "It is taking too much time to process. Please try again."})
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error calling Juspay API: {e.response.status_code} - {e.response.text}")
        return ApiFailure(error={"error": f"Juspay API error: {e.response.status_code}", "details": e.response.text})
    except Exception as e:
        logger.error(f"Unexpected error calling Juspay API: {e}")
        return ApiFailure(error={"error": f"An unexpected error occurred: {e}"})


def handle_genius_response(func):
    """
    A decorator that takes a tool function, executes it, and handles the
    GeniusApiResponse, sending the result or error via the callback.
    """
    @functools.wraps(func)
    async def wrapper(params: FunctionCallParams):
        try:
            # The wrapped function will return an ApiSuccess or ApiFailure object
            result = await func(params)
            if isinstance(result, ApiSuccess):
                await params.result_callback({"data": result.data})
            else:
                await params.result_callback(result.error)
        except Exception as e:
            logger.error(f"Critical error in {func.__name__}: {e}", exc_info=True)
            await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})
    return wrapper


@handle_genius_response
def get_sr_success_rate_by_time(params: FunctionCallParams) -> GeniusApiResponse:
    logger.info(f"Fetching real-time SR success rate with params: {params.arguments}")
    payload_details = {
        "dimensions": [],
        "domain": "kvorders",
        "metric": "success_rate"
    }
    return _make_genius_api_request(params, payload_details)


async def get_payment_analytics_by_dimension(params: FunctionCallParams):
    try:
        input_dimension = params.arguments.get("dimension")
        logger.info(
            f"Fetching payment analytics for input dimension '{input_dimension}' with params: {params.arguments}")

        actual_dimensions = []
        if input_dimension == "payment_gateway":
            actual_dimensions = ["payment_gateway"]
        elif input_dimension == "payment_instrument_overview":
            actual_dimensions = ["payment_instrument_group"]
        elif input_dimension == "payment_instrument_breakdown":
            actual_dimensions = ["payment_method", "payment_method_subtype"]
        else:
            actual_dimensions = ["payment_method_type"]

        # Analytics data
        analytics_payload = {
            "metric": ["total_amount", "order_with_transactions",
                       "success_rate", "success_volume"],
            "dimensions": actual_dimensions,
            "domain": "kvorders",
            "sortedOn": {"sortDimension": "total_amount", "ordering": "Desc"},
        }
        analytics_result = await _make_genius_api_request(
            params, analytics_payload)
        if isinstance(analytics_result, ApiFailure):
            await params.result_callback(analytics_result.error)
            return

        # Error messages data
        errors_payload = {
            "metric": ["order_with_transactions"],
            "dimensions": actual_dimensions + ["error_message"],
            "domain": "kvorders",
        }
        errors_result = await _make_genius_api_request(params, errors_payload)
        if isinstance(errors_result, ApiFailure):
            await params.result_callback(errors_result.error)
            return

        # Combine responses
        combined_data = {
            "analytics": analytics_result.data,
            "error_messages": errors_result.data,
        }

        await params.result_callback({"data": json.dumps(combined_data)})

    except Exception as e:
        logger.error(
            f"Critical error in get_payment_analytics_by_dimension: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


@handle_genius_response
def get_failure_transactional_data_by_time(params: FunctionCallParams) -> GeniusApiResponse:
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
    return _make_genius_api_request(params, payload_details)


@handle_genius_response
def get_success_transactional_data_by_time(params: FunctionCallParams) -> GeniusApiResponse:
    logger.info(f"Fetching real-time success data with params: {params.arguments}")
    payload_details = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "filters": {"condition": "In", "field": "payment_status", "val": ["SUCCESS"]},
        "metric": "success_volume"
    }
    return _make_genius_api_request(params, payload_details)


async def get_gmv_order_value_payment_method_wise_by_time(params: FunctionCallParams):
    logger.info(f"Fetching real-time GMV with params: {params.arguments}")
    payload_details = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "metric": "total_amount"
    }
    try:
        result = await _make_genius_api_request(params, payload_details)
        if isinstance(result, ApiSuccess):
            processed_data = []
            for line in result.data.strip().split('\n'):
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if "total_amount" in item and isinstance(item["total_amount"], (int, float)):
                        item["total_amount"] = format_indian_currency(round(item["total_amount"]))
                    processed_data.append(item)
                except json.JSONDecodeError as e:
                    logger.error(f"Error decoding JSON line: {line}. Error: {e}")
                    continue

            total_gmv = sum(float(item["total_amount"].replace(",", "")) for item in processed_data if "total_amount" in item and isinstance(item["total_amount"], str))
            processed_data.append({"total_gmv": format_indian_currency(round(total_gmv))})

            logger.info(f"Processed GMV data: {processed_data}")
            await params.result_callback({"data": json.dumps(processed_data)})
        else:
            await params.result_callback(result.error)
    except Exception as e:
        logger.error(f"Unexpected error in get_gmv_order_value_payment_method_wise_by_time: {e}", exc_info=True)
        await params.result_callback({"data": json.dumps({"error": f"Unexpected error occurred in the tool function: {e}"})})


@handle_genius_response
def get_average_ticket_payment_wise_by_time(params: FunctionCallParams) -> GeniusApiResponse:
    logger.info(f"Fetching real-time average ticket size with params: {params.arguments}")
    payload_details = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "metric": "avg_ticket_size"
    }
    return _make_genius_api_request(params, payload_details)



async def merchant_offer_analytics(params: FunctionCallParams):
    try:
        logger.info(
            f"Fetching merchant offer analytics with params: {params.arguments}")

        # Analytics data
        analytics_payload = {
            "metric": ["total_volume", "success_volume",
                       "success_rate", "avg_ticket_size", "total_amount"],
            "dimensions": ["merchant_offer_code"],
            "domain": "kvoffers",
            "sortedOn": {"sortDimension": "total_amount", "ordering": "Desc"},
        }
        analytics_result = await _make_genius_api_request(
            params, analytics_payload)
        if isinstance(analytics_result, ApiFailure):
            await params.result_callback(analytics_result.error)
            return

        # Error messages data
        errors_payload = {
            "metric": "total_volume",
            "dimensions": ["error_message", "merchant_offer_code"],
            "domain": "kvoffers",
        }
        errors_result = await _make_genius_api_request(params, errors_payload)
        if isinstance(errors_result, ApiFailure):
            await params.result_callback(errors_result.error)
            return

        # Combine responses
        combined_data = {
            "analytics": analytics_result.data,
            "error_messages": errors_result.data,
        }

        await params.result_callback({"data": json.dumps(combined_data)})

    except Exception as e:
        logger.error(
            f"Critical error in merchant_offer_analytics: {e}", exc_info=True)
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
    "required": ["startTime", "endTime"]
}

get_sr_success_rate_function = FunctionSchema(
    name="get_sr_success_rate_by_time",
    description="Get the overall payment success rate for all transactions within a specified time range. Use this to understand the general health of the payment system.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

payment_analytics_by_dimension_function = FunctionSchema(
    name="get_payment_analytics_by_dimension",
    description="Retrieves time-bound KPIs—total transaction volume, success rate, and transaction count—broken down by the selected dimension. Useful to analyze performance by gateway, instrument category, or specific instrument type (e.g., Visa, Mastercard). Always aim to extract as many dimensions as possible for a comprehensive snapshot.",
    properties={
        **time_input_schema["properties"],
        "dimension": {
            "type": "string",
            "description": "How to slice the data: 'payment_gateway' for each gateway (Stripe, Razorpay), 'payment_instrument_overview' for high-level groups (Credit, Debit, UPI, Wallet), or 'payment_instrument_breakdown' for granular types (Visa, Mastercard, UPI-Collect, Rupay, etc.). Choose the most specific level containing the metric you need.",
            "enum": ["payment_gateway", "payment_instrument_overview", "payment_instrument_breakdown"],
        },
    },
    required=["startTime", "endTime", "dimension"],
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

merchant_offer_analytics_function = FunctionSchema(
    name="merchant_offer_analytics",
    description="Fetches a list of all active merchant offers and their performance data. Use this to find out what the current offers are, how they are performing, and to diagnose any errors related to offer application.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

tools = ToolsSchema(
    standard_tools=[
        get_sr_success_rate_function,
        payment_analytics_by_dimension_function,
        failure_transactional_data_function,
        success_transactional_data_function,
        gmv_order_value_payment_method_wise_function,
        average_ticket_payment_wise_function,
        merchant_offer_analytics_function,
    ]
)

tool_functions = {
    "get_sr_success_rate_by_time": get_sr_success_rate_by_time,
    "get_payment_analytics_by_dimension": get_payment_analytics_by_dimension,
    "get_failure_transactional_data_by_time": get_failure_transactional_data_by_time,
    "get_success_transactional_data_by_time": get_success_transactional_data_by_time,
    "get_gmv_order_value_payment_method_wise_by_time": get_gmv_order_value_payment_method_wise_by_time,
    "get_average_ticket_payment_wise_by_time": get_average_ticket_payment_wise_by_time,
    "merchant_offer_analytics": merchant_offer_analytics,
}