import httpx
import json
from loguru import logger
from datetime import datetime
import pytz

from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

# These will be set by the initializer
breeze_token: str | None = None
shop_id: str | None = None
shop_url: str | None = None
shop_type: str | None = None

async def _make_breeze_request(params: FunctionCallParams, operational_tab: str):
    """Generic helper to make requests to the Breeze analytics API."""
    if not all([breeze_token, shop_id, shop_url, shop_type]):
        logger.error("Breeze tool called without required context (token, shopId, shopUrl, shopType).")
        await params.result_callback({"error": "Breeze tool is not configured."})
        return

    start_time_ist_str = params.arguments.get("startTime")
    end_time_ist_str = params.arguments.get("endTime")

    if not start_time_ist_str:
        await params.result_callback({"error": "startTime is a required parameter."})
        return

    try:
        ist = pytz.timezone("Asia/Kolkata")
        utc = pytz.utc

        # Convert start time from IST string to UTC datetime object
        start_time_ist = ist.localize(datetime.strptime(start_time_ist_str, '%Y-%m-%d %H:%M:%S'))
        start_time_utc = start_time_ist.astimezone(utc)

        # Handle end time
        if end_time_ist_str:
            end_time_ist = ist.localize(datetime.strptime(end_time_ist_str, '%Y-%m-%d %H:%M:%S'))
        else:
            end_time_ist = datetime.now(ist)
        end_time_utc = end_time_ist.astimezone(utc)

        # Format to ISO string required by the API
        start_time_iso = start_time_utc.isoformat().replace('+00:00', 'Z')
        end_time_iso = end_time_utc.isoformat().replace('+00:00', 'Z')

    except Exception as e:
        logger.error(f"Error converting time: {e}")
        await params.result_callback({"error": f"Invalid time format. Please use 'YYYY-MM-DD HH:MM:SS'. Error: {e}"})
        return

    api_url = "https://portal.breeze.in/analytics"
    payload = {
        "shopIds": [shop_id],
        "shops": [shop_url],
        "startTime": start_time_iso,
        "endTime": end_time_iso,
        "operationalTab": operational_tab,
        "granularityFilter": {"timeGranularity": "DAILY", "paymentMethods": "ALL"},
        "shopType": shop_type
    }
    headers = {
        "Content-Type": "application/json",
        "accept": "*/*",
        "x-auth-token": breeze_token
    }

    logger.info(f"Requesting Breeze {operational_tab} data with payload: {json.dumps(payload)}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()  # Raise an exception for bad status codes
            response_json = response.json()
            logger.info(f"Received Breeze API response status: {response_json.get('statusCode')}")
            await params.result_callback(response_json.get("data", {"error": "No data field in response"}))

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error calling Breeze API: {e.response.status_code} - {e.response.text}")
        await params.result_callback({"error": f"Breeze API error: {e.response.status_code}", "details": e.response.text})
    except Exception as e:
        logger.error(f"Unexpected error calling Breeze API: {e}")
        await params.result_callback({"error": f"An unexpected error occurred: {e}"})


async def get_breeze_sales_data(params: FunctionCallParams):
    """Fetches sales data from the Breeze analytics API."""
    await _make_breeze_request(params, "SALES")


async def get_breeze_orders_data(params: FunctionCallParams):
    """Fetches order data from the Breeze analytics API."""
    await _make_breeze_request(params, "ORDERS")


async def get_breeze_checkout_data(params: FunctionCallParams):
    """Fetches checkout data from the Breeze analytics API."""
    await _make_breeze_request(params, "CHECKOUT")


async def get_breeze_conversion_data(params: FunctionCallParams):
    """Fetches conversion data from the Breeze analytics API."""
    await _make_breeze_request(params, "CONVERSIONS")


async def get_breeze_marketing_data(params: FunctionCallParams):
    """Fetches marketing attribution data from the Breeze analytics API."""
    if not all([breeze_token, shop_id, shop_url, shop_type]):
        logger.error("Breeze tool called without required context (token, shopId, shopUrl, shopType).")
        await params.result_callback({"error": "Breeze tool is not configured."})
        return

    start_time_ist_str = params.arguments.get("startTime")
    end_time_ist_str = params.arguments.get("endTime")

    if not start_time_ist_str:
        await params.result_callback({"error": "startTime is a required parameter."})
        return

    try:
        ist = pytz.timezone("Asia/Kolkata")
        utc = pytz.utc

        start_time_ist = ist.localize(datetime.strptime(start_time_ist_str, '%Y-%m-%d %H:%M:%S'))
        start_time_utc = start_time_ist.astimezone(utc)

        if end_time_ist_str:
            end_time_ist = ist.localize(datetime.strptime(end_time_ist_str, '%Y-%m-%d %H:%M:%S'))
        else:
            end_time_ist = datetime.now(ist)
        end_time_utc = end_time_ist.astimezone(utc)

        start_time_iso = start_time_utc.isoformat().replace('+00:00', 'Z')
        end_time_iso = end_time_utc.isoformat().replace('+00:00', 'Z')

    except Exception as e:
        logger.error(f"Error converting time: {e}")
        await params.result_callback({"error": f"Invalid time format. Please use 'YYYY-MM-DD HH:MM:SS'. Error: {e}"})
        return

    api_url = "https://portal.breeze.in/analytics/marketing"
    payload = {
        "shopIds": [shop_id],
        "shops": [shop_url],
        "startTime": start_time_iso,
        "endTime": end_time_iso,
        "shopType": shop_type
    }
    headers = {
        "Content-Type": "application/json",
        "accept": "*/*",
        "x-auth-token": breeze_token
    }

    logger.info(f"Requesting Breeze marketing data with payload: {json.dumps(payload)}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            response_json = response.json()
            logger.info(f"Received Breeze API response status: {response_json.get('statusCode')}")
            await params.result_callback(response_json.get("data", {"error": "No data field in response"}))

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error calling Breeze Marketing API: {e.response.status_code} - {e.response.text}")
        await params.result_callback({"error": f"Breeze API error: {e.response.status_code}", "details": e.response.text})
    except Exception as e:
        logger.error(f"Unexpected error calling Breeze Marketing API: {e}")
        await params.result_callback({"error": f"An unexpected error occurred: {e}"})


async def get_breeze_address_data(params: FunctionCallParams):
    """Fetches address-related analytics from the Breeze API."""
    if not all([breeze_token, shop_id, shop_url, shop_type]):
        logger.error("Breeze tool called without required context (token, shopId, shopUrl, shopType).")
        await params.result_callback({"error": "Breeze tool is not configured."})
        return

    start_time_ist_str = params.arguments.get("startTime")
    end_time_ist_str = params.arguments.get("endTime")

    if not start_time_ist_str:
        await params.result_callback({"error": "startTime is a required parameter."})
        return

    try:
        ist = pytz.timezone("Asia/Kolkata")
        utc = pytz.utc

        start_time_ist = ist.localize(datetime.strptime(start_time_ist_str, '%Y-%m-%d %H:%M:%S'))
        start_time_utc = start_time_ist.astimezone(utc)

        if end_time_ist_str:
            end_time_ist = ist.localize(datetime.strptime(end_time_ist_str, '%Y-%m-%d %H:%M:%S'))
        else:
            end_time_ist = datetime.now(ist)
        end_time_utc = end_time_ist.astimezone(utc)

        start_time_iso = start_time_utc.isoformat().replace('+00:00', 'Z')
        end_time_iso = end_time_utc.isoformat().replace('+00:00', 'Z')

    except Exception as e:
        logger.error(f"Error converting time: {e}")
        await params.result_callback({"error": f"Invalid time format. Please use 'YYYY-MM-DD HH:MM:SS'. Error: {e}"})
        return

    api_url = "https://portal.breeze.in/analytics/address"
    payload = {
        "shopIds": [shop_id],
        "shops": [shop_url],
        "startTime": start_time_iso,
        "endTime": end_time_iso,
    }
    headers = {
        "Content-Type": "application/json",
        "accept": "*/*",
        "x-auth-token": breeze_token
    }

    logger.info(f"Requesting Breeze address data with payload: {json.dumps(payload)}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            response_json = response.json()
            logger.info(f"Received Breeze API response status: {response_json.get('statusCode')}")
            await params.result_callback(response_json.get("data", {"error": "No data field in response"}))

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error calling Breeze Address API: {e.response.status_code} - {e.response.text}")
        await params.result_callback({"error": f"Breeze API error: {e.response.status_code}", "details": e.response.text})
    except Exception as e:
        logger.error(f"Unexpected error calling Breeze Address API: {e}")
        await params.result_callback({"error": f"An unexpected error occurred: {e}"})


get_breeze_sales_data_function = FunctionSchema(
    name="get_breeze_sales_data",
    description="Fetches sales data (gross sales, net sales, discounts, shipping, tax, total sales) from Breeze analytics for a given shop and time range. Time should be provided in IST (e.g., '2025-06-20 00:00:00').",
    properties={
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. This is mandatory.",
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. Defaults to the current time if not provided.",
        },
    },
    required=["startTime"],
)

get_breeze_orders_data_function = FunctionSchema(
    name="get_breeze_orders_data",
    description="Fetches order data (total orders, average order value, total sales) from Breeze analytics for a given shop and time range. Time should be provided in IST (e.g., '2025-06-20 00:00:00').",
    properties={
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. This is mandatory.",
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. Defaults to the current time if not provided.",
        },
    },
    required=["startTime"],
)

get_breeze_checkout_data_function = FunctionSchema(
    name="get_breeze_checkout_data",
    description="Fetches checkout conversion funnel data (e.g., clicked checkout, logged in, placed order) from Breeze analytics for a given shop and time range. Time should be provided in IST (e.g., '2025-06-20 00:00:00').",
    properties={
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. This is mandatory.",
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. Defaults to the current time if not provided.",
        },
    },
    required=["startTime"],
)

get_breeze_conversion_data_function = FunctionSchema(
    name="get_breeze_conversion_data",
    description="Fetches conversion rate data (total sessions, orders placed, conversion rate) from Breeze analytics for a given shop and time range. Time should be provided in IST (e.g., '2025-06-20 00:00:00').",
    properties={
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. This is mandatory.",
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. Defaults to the current time if not provided.",
        },
    },
    required=["startTime"],
)

get_breeze_marketing_data_function = FunctionSchema(
    name="get_breeze_marketing_data",
    description="Fetches marketing attribution data (UTM source, medium, campaign, etc.) from Breeze analytics for a given shop and time range. Time should be provided in IST (e.g., '2025-06-20 00:00:00').",
    properties={
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. This is mandatory.",
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. Defaults to the current time if not provided.",
        },
    },
    required=["startTime"],
)

get_breeze_address_data_function = FunctionSchema(
    name="get_breeze_address_data",
    description="Fetches address-related analytics (e.g., total logged-in users, users with prefilled addresses, address validation metrics) from Breeze analytics for a given shop and time range. Time should be provided in IST (e.g., '2025-06-20 00:00:00').",
    properties={
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. This is mandatory.",
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in IST format 'YYYY-MM-DD HH:MM:SS'. Defaults to the current time if not provided.",
        },
    },
    required=["startTime"],
)


tools = ToolsSchema(
    standard_tools=[
        get_breeze_sales_data_function,
        get_breeze_orders_data_function,
        get_breeze_checkout_data_function,
        get_breeze_conversion_data_function,
        get_breeze_marketing_data_function,
        get_breeze_address_data_function,
    ]
)

tool_functions = {
    "get_breeze_sales_data": get_breeze_sales_data,
    "get_breeze_orders_data": get_breeze_orders_data,
    "get_breeze_checkout_data": get_breeze_checkout_data,
    "get_breeze_conversion_data": get_breeze_conversion_data,
    "get_breeze_marketing_data": get_breeze_marketing_data,
    "get_breeze_address_data": get_breeze_address_data,
}