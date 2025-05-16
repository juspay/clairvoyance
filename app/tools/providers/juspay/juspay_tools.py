import asyncio
import json
import logging
import datetime
import pytz
import aiohttp
# No direct import of types.Tool here, only declarations for Gemini are needed by the service layer
# from google.genai import types # Not strictly needed here anymore for Tool object creation

from app.core.config import GENIUS_API_URL

logger = logging.getLogger(__name__)

# ---- Function Declarations (as before) ----
# get_current_time_declaration removed from here

time_input_schema = {
    "type": "object",
    "properties": {
        "startTime": {
            "type": "string",
            "description": "The start time for the analysis in ISO format (e.g., 2023-01-01T00:00:00Z). Defaults to beginning of the current day (midnight) if not provided."
        },
        "endTime": {
            "type": "string",
            "description": "The end time for the analysis in ISO format (e.g., 2023-01-01T01:00:00Z). Defaults to current time if not provided."
        }
    },
    "required": ["startTime", "endTime"],
}

get_sr_success_rate_declaration = {
    "name": "getSRSuccessRateByTime",
    "description": "This tool calculates the overall success rate (SR) for transactions over a specified time interval.",
    "parameters": time_input_schema
}

payment_method_wise_sr_declaration = {
    "name": "getPaymentMethodWiseSRByTime",
    "description": "This tool fetches a breakdown of the success rate (SR) by payment method over a specified time interval.",
    "parameters": time_input_schema
}

failure_transactional_data_declaration = {
    "name": "getFailureTransactionalData",
    "description": "This tool retrieves transactional data for failed transactions. The returned data highlights the top failure reasons and their associated payment methods.",
    "parameters": time_input_schema
}

success_transactional_data_declaration = {
    "name": "getSuccessTransactionalData",
    "description": "This tool retrieves the count of successful transactions (i.e. those with a payment_status of SUCCESS) for each payment method over a specified time interval.",
    "parameters": time_input_schema
}

gmv_order_value_payment_method_wise_declaration = {
    "name": "getGMVOrderValuePaymentMethodWise",
    "description": "This tool retrieves the Gross Merchandise Value (GMV) for each payment method over a specified time interval.",
    "parameters": time_input_schema
}

average_ticket_payment_wise_declaration = {
    "name": "getAverageTicketPaymentWise",
    "description": "This tool calculates the average ticket size for each payment method over a specified time interval.",
    "parameters": time_input_schema
}

# ---- Tool Implementation Functions (as before) ----
# get_current_time function removed from here

def get_formatted_time_range(input_data):
    start_time = input_data.get("startTime")
    end_time = input_data.get("endTime")
    if not start_time:
        tz = pytz.timezone("Asia/Kolkata")
        now = datetime.datetime.now(tz)
        start_time = datetime.datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz).isoformat()
    if not end_time:
        tz = pytz.timezone("Asia/Kolkata")
        end_time = datetime.datetime.now(tz).isoformat()
    return {"formattedStartTime": start_time, "formattedEndTime": end_time}

async def make_genius_api_request(payload, juspay_token, session_id=None):
    session_prefix = f"[{session_id}] " if session_id else ""
    logger.info(f"{session_prefix}Genius API request: {GENIUS_API_URL}, metric: {payload.get('metric')}, domain: {payload.get('domain')}")
    try:
        headers = {'Content-Type': 'application/json', 'x-web-logintoken': juspay_token}
        logger.debug(f"{session_prefix}Request payload: {json.dumps(payload)}")
        async with aiohttp.ClientSession() as session:
            async with session.post(GENIUS_API_URL, headers=headers, json=payload) as response:
                response_text = await response.text()
                if response.status == 200:
                    logger.info(f"{session_prefix}Genius API success. Response: {response_text[:200]}...")
                    return response_text
                else:
                    logger.error(f"{session_prefix}Genius API failed: {response.status}, Body: {response_text}")
                    return f"API Error: {response.status} {response_text}"
    except Exception as e:
        logger.error(f"{session_prefix}Genius API request error: {str(e)}")
        return f"Failed to fetch data: {str(e)}"

async def get_sr_success_rate_by_time(startTime, endTime=None, juspay_token=None, session_id=None):
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    payload = {"dimensions": [], "domain": "kvorders", "interval": {"start": time_range["formattedStartTime"], "end": time_range["formattedEndTime"]}, "message": "Fetching SR.", "metric": "success_rate"}
    return await make_genius_api_request(payload, juspay_token, session_id)

async def get_payment_method_wise_sr_by_time(startTime, endTime=None, juspay_token=None, session_id=None):
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    payload = {"dimensions": ["payment_method_type"], "domain": "kvorders", "interval": {"start": time_range["formattedStartTime"], "end": time_range["formattedEndTime"]}, "message": "Fetching PM wise SR.", "metric": "success_rate"}
    return await make_genius_api_request(payload, juspay_token, session_id)

async def get_failure_transactional_data(startTime, endTime=None, juspay_token=None, session_id=None):
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    payload = {"dimensions": ["error_message", "payment_method_type"], "domain": "kvorders", "filters": {"and": {"left": {"condition": "NotIn", "field": "error_message", "val": [None]}, "right": {"condition": "In", "field": "error_message", "val": {"limit": 20, "sortedOn": {"ordering": "Desc", "sortDimension": "order_with_transactions"}}}}}, "interval": {"start": time_range["formattedStartTime"], "end": time_range["formattedEndTime"]}, "message": "Fetching failure data.", "metric": "order_with_transactions"}
    return await make_genius_api_request(payload, juspay_token, session_id)

async def get_success_transactional_data(startTime, endTime=None, juspay_token=None, session_id=None):
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    payload = {"dimensions": ["payment_method_type"], "domain": "kvorders", "filters": {"condition": "In", "field": "payment_status", "val": ["SUCCESS"]}, "interval": {"start": time_range["formattedStartTime"], "end": time_range["formattedEndTime"]}, "message": "Fetching success data.", "metric": "success_volume"}
    return await make_genius_api_request(payload, juspay_token, session_id)

async def get_gmv_order_value_payment_method_wise(startTime, endTime=None, juspay_token=None, session_id=None):
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    payload = {"dimensions": ["payment_method_type"], "domain": "kvorders", "interval": {"start": time_range["formattedStartTime"], "end": time_range["formattedEndTime"]}, "message": "Fetching GMV.", "metric": "total_amount"}
    return await make_genius_api_request(payload, juspay_token, session_id)

async def get_average_ticket_payment_wise(startTime, endTime=None, juspay_token=None, session_id=None):
    input_data = {"startTime": startTime, "endTime": endTime}
    time_range = get_formatted_time_range(input_data)
    payload = {"dimensions": ["payment_method_type"], "domain": "kvorders", "interval": {"start": time_range["formattedStartTime"], "end": time_range["formattedEndTime"]}, "message": "Fetching avg ticket size.", "metric": "avg_ticket_size"}
    return await make_genius_api_request(payload, juspay_token, session_id)


# ---- Rich Tool Definitions ----
# Each definition includes the declaration for Gemini, the function to call,
# and any specific context parameters required by that function.

juspay_context_params = ["juspay_token", "session_id"]

juspay_tools_definitions = [
    # getCurrentTime tool definition removed from here
    {
        "declaration": get_sr_success_rate_declaration,
        "function": get_sr_success_rate_by_time,
        "required_context_params": juspay_context_params
    },
    {
        "declaration": payment_method_wise_sr_declaration,
        "function": get_payment_method_wise_sr_by_time,
        "required_context_params": juspay_context_params
    },
    {
        "declaration": failure_transactional_data_declaration,
        "function": get_failure_transactional_data,
        "required_context_params": juspay_context_params
    },
    {
        "declaration": success_transactional_data_declaration,
        "function": get_success_transactional_data,
        "required_context_params": juspay_context_params
    },
    {
        "declaration": gmv_order_value_payment_method_wise_declaration,
        "function": get_gmv_order_value_payment_method_wise,
        "required_context_params": juspay_context_params
    },
    {
        "declaration": average_ticket_payment_wise_declaration,
        "function": get_average_ticket_payment_wise,
        "required_context_params": juspay_context_params
    }
]

__all__ = ["juspay_tools_definitions"]