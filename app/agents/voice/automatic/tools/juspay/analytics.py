import httpx
import json
import functools
from difflib import SequenceMatcher
import requests
import logging
from typing import get_args, Optional, Dict, Any, Union, List, Mapping
from dateutil import parser

from datetime import datetime
import pytz
from app.core.logger import logger
from app.core.config import GENIUS_API_URL, EULER_DASHBOARD_API_URL
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from app.agents.voice.automatic.types.models import (
    ApiFailure, ApiSuccess, GeniusApiResponse, CardinalityDimension,
    DimensionLookupRequest, DimensionLookupResult, FieldLookupBatchResponse,
    # Q API Types
    DimensionObject, DimensionString, FlatFilter, Interval, Metric,
    QApiResponse, QApiSuccessResponse, QApiErrorResponse, QApiPayload,
    MetricEnum, MetricFilter
)
from .field_value_config import DIMENSION_ALIASES, FIELD_GUARDS
from .aliases import (
    resolve_metric_alias,
    resolve_dimension_alias,
    reverse_metric_alias,
    reverse_dimension_alias,
)
from app.utils.time import ist_to_utc, convert_utc_to_ist_in_qapi_response
from app.utils.datetime_serialization import json_dumps_with_datetime
from app.utils.auth import get_authorized_merchants, validate_and_extract_fields
from app.utils.qapi.filters_flat_to_tree import flat_filter_to_tree

# This token will be set when the tools are initialized
euler_token: str | None = None
merchant_id: str | None = None


def validate_token(token: str) -> Dict[str, Any]:
    """
    Validate the token by calling the Juspay token validation endpoint.
    
    Args:
        token: The euler_token to validate
        
    Returns:
        Dict containing the token response with user context and authorization info
        
    Raises:
        Exception: If token validation fails
    """
    try:
        logger.info("Validating euler_token with Juspay API")
        response = requests.post(
            "https://portal.juspay.in/ec/v1/validate/token",
            json={"token": token},
            timeout=10.0
        )
        response.raise_for_status()
        
        token_response = response.json()
        logger.info(f"Token validation successful for user: {token_response.get('username', 'unknown')}")
        return token_response
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to validate token: {e}")
        raise Exception(f"Token validation failed: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error during token validation: {e}")
        raise Exception(f"Token validation error: {str(e)}")


# Define required fields for offer creation - shared between function and schema
OFFER_REQUIRED_KEYS = [
    "offerCode", "offerType", "offerTitle", 
    "discountValue", "startDate", "endDate", 
    "offerDescription"
]

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


def resolve_dimension_alias(dimension_name: str) -> str:
    """Convert LLM-known dimension name to actual system dimension name."""
    return DIMENSION_ALIASES.get(dimension_name, dimension_name)


def simple_fuzzy_score(query: str, candidate: str) -> float:
    """
    Enhanced fuzzy matching score using SequenceMatcher for better similarity detection.
    Returns a score between 0.0 and 1.0, with 1.0 being an exact match.
    """
    return SequenceMatcher(None, query.lower(), candidate.lower()).ratio()


async def get_field_values_from_api(dimension: str) -> list[str]:
    """
    Get field values for a dimension from the Juspay QAPI filters endpoint.
    This uses the same API endpoint as fetch_field_values_from_qapi for consistency.
    """
    if not euler_token:
        logger.warning("No euler_token available for field value discovery")
        return []
    
    try:
        # Default to last 24 hours for the time interval
        from datetime import datetime, timedelta
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=1)
        
        # Use payload format consistent with QAPI filters endpoint
        payload = {
            "domain": "kvorders",
            "metric": ["order_with_transactions"],
            "dimensions": [dimension],
            "interval": {
                "start": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            },
            "sortedOn": {"sortDimension": "order_with_transactions", "ordering": "Desc"}
        }
        
        headers = {
            'Content-Type': 'application/json',
            'X-Web-LoginToken': euler_token
        }
        
        logger.info(f"Fetching field values for dimension '{dimension}' from QAPI filters endpoint")
        
        # Use the QAPI filters URL instead of GENIUS_API_URL
        qapi_filters_url = "https://portal.juspay.in/api/q/query?api=filters"
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(qapi_filters_url, json=payload, headers=headers)
            response.raise_for_status()
            
            # Parse response and extract unique field values
            values = set()
            for line in response.text.strip().split('\n'):
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if dimension in item and item[dimension] is not None:
                        values.add(str(item[dimension]))
                except json.JSONDecodeError:
                    continue
            
            result = list(values)[:50]  # Limit to 50 values
            logger.info(f"Retrieved {len(result)} field values for dimension '{dimension}'")
            return result
            
    except Exception as e:
        logger.error(f"Failed to fetch field values for dimension '{dimension}': {e}")
        return []


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


async def create_euler_offer(params: FunctionCallParams):
    """
    Creates discount offers, cashbacks, and other promotional offers in the platform. IMPORTANT: Before calling this function, you MUST first present all the offer details to the user in a clear, formatted way and explicitly ask for their confirmation. Only proceed with calling this function after the user has explicitly confirmed they want to create the offer. Do not call this function without explicit user confirmation. To set the offer's active period, always use the get_current_time() tool for accurate start and end times in IST.
    """
    try:
        # Define required fields
        required_fields = {
            key: params.arguments.get(key) for key in OFFER_REQUIRED_KEYS
        }
        
        # Find missing ones
        missing_fields = [key for key, value in required_fields.items() if not value]
        
        if missing_fields:
            await params.result_callback({
                "error": f"Missing required fields: {', '.join(missing_fields)}"
            })
            return

        # Get merchantId from global context variable (set during tool initialization)
        if not merchant_id:
            await params.result_callback({"error": "Merchant ID not available in session context. Cannot create offer."})
            return

        # Authentication check
        if not euler_token:
            await params.result_callback({"error": "Authentication token is missing. Cannot create offer."})
            return

        # Extract validated required parameters
        offer_code = required_fields["offerCode"]
        offer_type = required_fields["offerType"]
        offer_title = required_fields["offerTitle"]
        discount_value = required_fields["discountValue"]
        start_date = required_fields["startDate"]
        end_date = required_fields["endDate"]
        offer_description = required_fields["offerDescription"]

        logger.info(f"Creating Euler offer with code '{offer_code}' for merchant '{merchant_id}'")

        # Get optional parameters with defaults
        min_order_amount = params.arguments.get("minOrderAmount", 1)
        if min_order_amount is None:
            min_order_amount = 1
        max_discount_amount = params.arguments.get("maxDiscountAmount")
        calculation_type = params.arguments.get("calculationType", "ABSOLUTE")
        is_coupon_based = params.arguments.get("isCouponBased", True)
        sponsored_by = params.arguments.get("sponsoredBy", "BREEZE")
        payment_instruments = params.arguments.get("paymentInstruments", [])

        # Payment instrument mapping
        instrument_map = {
            "CARD": {
                "payment_method_type": "CARD",
                "payment_method": [],
                "app": [],
                "type": [],
                "issuer": [],
                "variant": []
            },
            "NB": {
                "payment_method_type": "NB",
                "payment_method": [],
                "app": [],
                "type": [],
                "issuer": [],
                "variant": []
            },
            "WALLET": {
                "payment_method_type": "WALLET",
                "payment_method": [],
                "app": [],
                "type": [],
                "issuer": [],
                "variant": []
            },
            "CONSUMER_FINANCE": {
                "payment_method_type": "CONSUMER_FINANCE",
                "payment_method": [],
                "app": [],
                "type": [],
                "issuer": [],
                "variant": []
            },
            "REWARD": {
                "payment_method_type": "REWARD",
                "payment_method": [],
                "app": [],
                "type": [],
                "issuer": [],
                "variant": []
            },
            "CASH": {
                "payment_method_type": "CASH",
                "payment_method": ["CASH"],
                "app": [],
                "type": [],
                "issuer": [],
                "variant": []
            },
            "UPI": {
                "payment_method_type": "UPI",
                "payment_method": [],
                "app": [],
                "type": ["UPI_COLLECT", "UPI_PAY", "UPI_QR", "UPI_INAPP"],
                "issuer": [],
                "variant": []
            }
        }

        # Convert IST dates to ISO format for API payload
        try:
            ist = pytz.timezone("Asia/Kolkata")
            
            # Parse start_date from IST format and convert to ISO
            start_date_ist = ist.localize(datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S'))
            start_date_iso = start_date_ist.isoformat()
            
            # Parse end_date from IST format and convert to ISO
            end_date_ist = ist.localize(datetime.strptime(end_date, '%Y-%m-%d %H:%M:%S'))
            end_date_iso = end_date_ist.isoformat()
            
        except Exception as e:
            logger.error(f"Error converting date format for offer creation: {e}")
            await params.result_callback({"error": f"Invalid date format provided. Please use 'YYYY-MM-DD HH:MM:SS' in IST. Error: {e}"})
            return

        # Build payment instruments payload
        if payment_instruments:
            payment_instruments_payload = [
                instrument_map[instrument] for instrument in payment_instruments
                if instrument in instrument_map
            ]
        else:
            payment_instruments_payload = list(instrument_map.values())

        # Construct the API payload
        api_payload = {
            "application_mode": "ORDER",
            "merchant_id": merchant_id,
            "offer_code": offer_code,
            "batch_id": "",
            "offer_description": {
                "title": offer_title,
                "description": offer_description,
                "tnc": "",
                "sponsored_by": sponsored_by,
                "display_title": offer_title
            },
            "ui_configs": {
                "is_hidden": "false",
                "should_validate": "true",
                "auto_apply": "false" if is_coupon_based else "true",
                "offer_display_priority": 0,
                "payment_method_label": ""
            },
            "rule_dsl": {
                "order": {
                    "max_quantity": None,
                    "min_quantity": None,
                    "max_order_amount": None,
                    "min_order_amount": str(min_order_amount),
                    "currency": "INR",
                    "amount_info": []
                },
                "additional_payment_filters": None,
                "payment_instrument": payment_instruments_payload,
                "counters": [],
                "payment_channel": [],
                "benefits": [
                    {
                        "type": offer_type,
                        "calculation_rule": calculation_type,
                        "value": discount_value,
                        "amount_info": [],
                        "max_amount": max_discount_amount,
                        "global_max_amount": None
                    }
                ],
                "filters": {
                    "blacklist": [],
                    "whitelist": []
                }
            },
            "status": "ACTIVE",
            "start_time": start_date_iso,
            "end_time": end_date_iso,
            "metadata": {
                "analytics_offer_code": offer_code,
                "customerResetPeriodType": "offerPeriod",
                "cardResetPeriodType": "offerPeriod",
                "productCustomerResetPeriodType": "offerPeriod",
                "productCardResetPeriodType": "offerPeriod",
                "upiResetPeriodType": "offerPeriod",
                "productUpiResetPeriodType": "offerPeriod",
                "start_date": start_date_iso,
                "end_date": end_date_iso
            },
            "udf1": None,
            "udf2": None,
            "udf3": None,
            "udf4": None,
            "udf5": None,
            "udf6": None,
            "udf7": None,
            "udf8": None,
            "udf9": None,
            "udf10": None,
            "minOfferBreakupCheckbox": False,
            "offerBreakupBool": False,
            "benefitsAmountInfo": [],
            "has_multi_codes": False
        }

        # Make API request
        endpoint = f"{EULER_DASHBOARD_API_URL}/api/offers/dashboard/create?merchant_id={merchant_id}"
        headers = {
            'Content-Type': 'application/json',
            'x-web-logintoken': euler_token
        }

        logger.info(f"Making offer creation request to: {endpoint} | Payload: {json.dumps(api_payload, indent=2)}")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(endpoint, json=api_payload, headers=headers)
            
            if response.status_code == 200:
                response_data = response.json()
                offer_id = response_data.get("offer_id")
                
                if offer_id:
                    success_result = {
                        "status": "success",
                        "offerId": offer_id,
                        "message": f"Successfully created offer {offer_code}",
                        "details": {
                            "offerCode": offer_code,
                            "type": offer_type,
                            "value": discount_value,
                            "validFrom": start_date,
                            "validTo": end_date,
                            "minAmount": min_order_amount,
                            "sponsoredBy": sponsored_by,
                            "paymentMethods": payment_instruments if payment_instruments else "All payment methods"
                        }
                    }
                    await params.result_callback({"data": json.dumps(success_result)})
                else:
                    error_message = response_data.get("error_message", "API call failed to return an offer ID.")
                    await params.result_callback({"error": f"Failed to create offer: {error_message}"})
            else:
                error_text = response.text
                logger.error(f"Offer creation failed: {response.status_code} - {error_text}")
                await params.result_callback({"error": f"Failed to create offer: HTTP {response.status_code}"})

    except httpx.TimeoutException:
        logger.error("Offer creation request timed out after 30 seconds.")
        await params.result_callback({"error": "Request timed out. Please try again."})
    except Exception as e:
        logger.error(f"Critical error in create_euler_offer: {e}", exc_info=True)
        await params.result_callback({"error": f"An unexpected error occurred: {str(e)}"})


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


async def field_value_discovery(params: FunctionCallParams):
    """
    Unified batch field-value discovery tool for cardinality dimensions.
    Discovers and validates field values for analytics dimensions using fuzzy matching.
    
    This tool helps users find valid values for dimension filters by:
    1. Fetching available field values from the API 
    2. Using fallback values when API is unavailable
    3. Performing fuzzy search to match user queries
    4. Returning ranked results for each dimension
    """
    try:
        # Extract parameters
        requests_data = params.arguments.get("requests", [])
        default_limit = params.arguments.get("default_limit", 10)
        
        # Check if default_limit is set and exceeds 50
        if default_limit is not None and default_limit > 50:
            return FieldLookupBatchResponse(
                error="default_limit cannot be greater than 50."
            )
        
        logger.info(f"Field value discovery for {len(requests_data)} dimensions, default_limit: {default_limit}")
        
        # Parse requests
        requests = []
        try:
            for req_data in requests_data:
                requests.append(DimensionLookupRequest(**req_data))
        except Exception as e:
            logger.error(f"Failed to parse dimension lookup requests: {e}")
            return FieldLookupBatchResponse(
                error=f"Invalid request format: {str(e)}"
            )
        
        # Get supported dimensions from CardinalityDimension
        supported_dimensions = set(get_args(CardinalityDimension))
        
        # Process each dimension request
        results = []
        for req in requests:
            original_dim = req.dimension
            # Resolve aliases to get actual system dimension name  
            resolved_dim = resolve_dimension_alias(req.dimension)
            
            # Log if an alias was resolved
            if resolved_dim != original_dim:
                logger.info(f"Resolved dimension alias: '{original_dim}' -> '{resolved_dim}'")
            
            # Check if dimension is supported
            if original_dim not in supported_dimensions and resolved_dim not in supported_dimensions:
                logger.info(f"Dimension '{original_dim}' is not supported by field_value_discovery")
                unsupported_msg = (
                    f"The dimension '{original_dim}' is not supported by field_value_discovery. "
                    f"Please use the value directly in your q_api filter without validation. "
                    f"Field value discovery only supports the following dimensions: "
                    f"{', '.join(sorted(supported_dimensions)[:5])}... and others."
                )
                results.append(DimensionLookupResult(
                    dimension=original_dim,
                    results=[],
                    unsupported_message=unsupported_msg
                ))
                continue
            
            queries = req.queries or []
            max_results = req.max_results or default_limit
            
            # Get field values - try API first, then fallback to guards
            candidates = []
            
            try:
                # Try to get values from API
                api_candidates = await get_field_values_from_api(resolved_dim)
                if api_candidates:
                    candidates.extend(api_candidates)
                    logger.info(f"Retrieved {len(api_candidates)} API values for {resolved_dim}")
                
                # Add fallback guard values
                guard_values = FIELD_GUARDS.get(resolved_dim, [])
                for guard_val in guard_values:
                    if guard_val not in candidates:
                        candidates.append(guard_val)
                
                if guard_values:
                    logger.info(f"Added {len(guard_values)} guard values for {resolved_dim}")
                    
            except Exception as e:
                logger.error(f"Error getting field values for {resolved_dim}: {e}")
                # Use only guard values as fallback
                candidates = FIELD_GUARDS.get(resolved_dim, [])
                logger.info(f"Using fallback guard values only for {resolved_dim}: {len(candidates)} values")
            
            # Process queries or return top values
            dim_results = []
            if not candidates:
                # If no candidates, return empty for all queries
                dim_results = [[] for _ in (queries or [None])]
            elif queries:
                # Fuzzy search for each query
                for query in queries:
                    if not query.strip():
                        # Empty query returns first N values
                        dim_results.append(candidates[:max_results])
                    else:
                        # Rank candidates by fuzzy match score
                        scored_candidates = [
                            (candidate, simple_fuzzy_score(query, candidate))
                            for candidate in candidates
                        ]
                        # Sort by score (descending) then alphabetically
                        scored_candidates.sort(key=lambda x: (-x[1], x[0]))
                        ranked_results = [candidate for candidate, _ in scored_candidates[:max_results]]
                        dim_results.append(ranked_results)
            else:
                # No queries: return first N values
                dim_results.append(candidates[:max_results])
            
            # Use original dimension name in response
            results.append(DimensionLookupResult(
                dimension=original_dim,
                results=dim_results
            ))
        
        # Create response
        response = FieldLookupBatchResponse(results=results)
        
        # Send response via callback
        await params.result_callback({"data": response.model_dump_json()})
        
    except Exception as e:
        logger.error(f"Critical error in field_value_discovery: {e}", exc_info=True)
        await params.result_callback({"error": f"A critical error occurred in the tool function: {e}"})


# Metrics that represent monetary values and require currency dimension
AMOUNT_BASED_METRICS = {"total_amount", "avg_ticket_size", "saved_orders_amount", "saved_orders_amount_gateway"}


def ensure_currency_dimension_for_amounts(
    metric: Union[str, List[str]], 
    dimensions: Optional[List[Union[DimensionString, DimensionObject]]] = None
) -> List[Union[DimensionString, DimensionObject]]:
    """
    Ensures that ord_currency dimension is included when querying amount-based metrics.
    
    This prevents meaningless cross-currency aggregations like summing INR + USD + AED values.
    
    Args:
        metric: Single metric string or list of metrics being queried
        dimensions: Current list of dimensions (if any)
        
    Returns:
        Modified dimensions list with ord_currency added if needed
    """
    # Normalize metric to list for easier processing
    metrics_list = metric if isinstance(metric, list) else [metric]
    
    # Early return if no amount-based metrics are being queried
    amount_metrics_in_query = [m for m in metrics_list if m in AMOUNT_BASED_METRICS]
    if not amount_metrics_in_query:
        return dimensions if dimensions else []
    
    # Initialize dimensions list if not provided
    modified_dimensions = dimensions.copy() if dimensions else []
    
    # Extract dimension names for checking
    dimension_names = []
    has_object_dimension = False
    for dim in modified_dimensions:
        if isinstance(dim, str):
            dimension_names.append(dim)
        elif isinstance(dim, dict) and "dimension" in dim:
            dimension_names.append(dim["dimension"])
        elif isinstance(dim, DimensionObject):
            has_object_dimension = True
    
    # Add ord_currency only if not already present AND all dimensions are strings
    if "ord_currency" not in dimension_names and not has_object_dimension:
        logger.info(
            f"Auto-including ord_currency dimension for amount-based metrics: {amount_metrics_in_query}"
        )
        modified_dimensions.append("ord_currency")
    
    return modified_dimensions


def resolve_aliases_in_payload(
    metric: Metric, 
    dimensions: List[Union[DimensionString, DimensionObject]], 
    filters: Optional[FlatFilter], 
    sortedOn: Optional[Dict[str, Any]]
) -> tuple:
    """
    Resolve aliases in the metric, dimensions, filters, and sortedOn.
    Returns resolved (metric, dimensions, filters, sortedOn, transformation_map).
    """
    # Track transformations separately for each type
    transformation_map = {
        "metrics": {},      # original_metric -> resolved_metric
        "dimensions": {},   # original_dimension -> resolved_dimension
    }
    
    # Resolve metric aliases
    if isinstance(metric, list):
        resolved_metric = []
        for m in metric:
            resolved = resolve_metric_alias(m)
            if resolved != m:
                transformation_map["metrics"][m] = resolved
            resolved_metric.append(resolved)
    else:
        resolved_metric = resolve_metric_alias(metric)
        if resolved_metric != metric:
            transformation_map["metrics"][metric] = resolved_metric
    
    # Resolve dimension aliases
    resolved_dimensions = []
    for dim in dimensions:
        if isinstance(dim, str):
            resolved = resolve_dimension_alias(dim)
            if resolved != dim:
                transformation_map["dimensions"][dim] = resolved
            resolved_dimensions.append(resolved)
        else:
            # DimensionObject - no aliasing needed
            resolved_dimensions.append(dim)
    
    # Resolve aliases in filters
    resolved_filters = filters
    if filters and filters.clauses:
        # Create a new filter with resolved field names
        resolved_clauses = []
        for clause in filters.clauses:
            resolved_clause = clause.model_copy()
            resolved_clause.field = resolve_dimension_alias(clause.field)
            resolved_clauses.append(resolved_clause)
        
        resolved_filters = FlatFilter(
            clauses=resolved_clauses,
            logic=filters.logic
        )
    
    # Resolve aliases in sortedOn
    resolved_sortedOn = sortedOn
    if sortedOn and isinstance(sortedOn, dict) and "sortDimension" in sortedOn:
        # Create a copy of sortedOn and resolve the sortDimension
        resolved_sortedOn = sortedOn.copy()
        resolved_sortedOn["sortDimension"] = resolve_metric_alias(sortedOn["sortDimension"])
    
    return resolved_metric, resolved_dimensions, resolved_filters, resolved_sortedOn, transformation_map


def reverse_aliases_in_response(response: QApiResponse, transformation_map: Dict[str, Dict[str, str]]) -> QApiResponse:
    """
    Reverse aliases in the response to convert back to LLM-known names.
    Only reverses transformations that were actually applied during forward aliasing.
    """
    if isinstance(response, QApiErrorResponse):
        return response
    
    # Create reverse maps for each type of transformation
    reverse_metrics = {v: k for k, v in transformation_map.get("metrics", {}).items()}
    reverse_dimensions = {v: k for k, v in transformation_map.get("dimensions", {}).items()}
    
    # Combine all reverse maps, with proper precedence
    all_reverse_transformations = {}
    all_reverse_transformations.update(reverse_dimensions)
    all_reverse_transformations.update(reverse_metrics)
    
    # For success response, we need to reverse aliases in the row keys
    if isinstance(response, QApiSuccessResponse):
        reversed_rows = []
        for row in response.root:
            reversed_row_data = {}
            
            # Get all fields from the row (both defined fields and extra fields)
            row_dict = row.model_dump()
            
            for key, value in row_dict.items():
                # Only reverse if this key was actually transformed
                if key in all_reverse_transformations:
                    reversed_row_data[all_reverse_transformations[key]] = value
                else:
                    # Keep the key as-is if it wasn't transformed
                    reversed_row_data[key] = value
            
            reversed_rows.append(reversed_row_data)
        
        # Create a new QApiSuccessResponse with reversed aliases
        return QApiSuccessResponse.model_validate(reversed_rows)
    
    return response


def is_dimension_supported_by_field_discovery(dimension: str) -> bool:
    """
    Check if a dimension is supported by field_value_discovery tool.
    
    Args:
        dimension: The dimension name to check
        
    Returns:
        True if the dimension is supported, False otherwise
    """
    # Get all supported dimensions from CardinalityDimension
    supported_dimensions = set(get_args(CardinalityDimension))
    
    # Check both original and resolved dimension names
    resolved_dimension = resolve_dimension_alias(dimension)
    
    return dimension in supported_dimensions or resolved_dimension in supported_dimensions


def extract_filter_fields_and_values(filters: Optional[FlatFilter]) -> Dict[str, List[Any]]:
    """
    Extract field names and their corresponding values from a FlatFilter object,
    but only for dimensions supported by field_value_discovery.
    
    Args:
        filters: The FlatFilter object containing clauses
        
    Returns:
        Dictionary mapping field names to lists of values used in the filter
        (only includes dimensions supported by field_value_discovery)
    """
    if not filters or not filters.clauses:
        return {}
    
    field_values = {}
    skipped_fields = []
    
    for clause in filters.clauses:
        if clause.field and clause.val is not None:
            # Check if this dimension is supported by field_value_discovery
            if is_dimension_supported_by_field_discovery(clause.field):
                if clause.field not in field_values:
                    field_values[clause.field] = []
                
                # Handle different value types
                if isinstance(clause.val, list):
                    field_values[clause.field].extend(clause.val)
                else:
                    field_values[clause.field].append(clause.val)
            else:
                # Log that we're skipping this field
                if clause.field not in skipped_fields:
                    skipped_fields.append(clause.field)
    
    if skipped_fields:
        logger.info(f"Skipping validation for unsupported dimensions: {skipped_fields}")
    
    # Remove duplicates while preserving order
    for field in field_values:
        field_values[field] = list(dict.fromkeys(field_values[field]))
    
    return field_values


async def call_query_api(payload: QApiPayload, web_login_token: str, token_response: dict[str, Any]) -> QApiResponse:
    """
    Utility function to call the query API with the provided payload.

    Args:
        payload: The payload to send to the query API (QApiPayload model)
        web_login_token: Authentication token for the API
        token_response: Token response for authorization checks

    Returns:
        The parsed response from the API as QApiResponse (either QApiSuccessResponse or QApiErrorResponse)
    """
    try:
        # Validate parameters first
        payload_dict = payload.model_dump()
        try:
            _validate_params(payload_dict)
        except ValueError as e:
            # Validation error - return the detailed error
            error_result = json.loads(str(e))
            print("[Q API TOOL] Parameter validation failed")
            return QApiErrorResponse(
                error=error_result,
                payload_attempted=payload_dict,
            )
        
        # Validate filter values if filters are provided
        if payload_dict.get("filters"):
            validation_result = await _validate_filter_values(payload_dict["filters"])
            if not validation_result["valid"]:
                print("[Q API TOOL] Filter value validation failed")
                error_result = {
                    "error": "Invalid filter values",
                    "validation_errors": validation_result["errors"],
                    "suggestions": validation_result["suggestions"],
                    "fix_instructions": {
                        "issue": "One or more filter values are not valid for their respective fields",
                        "solution": "Use the suggested values or run field_value_discovery tool to find valid values",
                        "example_fix": "If you used 'Razerpay', change it to 'RAZORPAY'"
                    },
                    "provided_filters": payload_dict["filters"]
                }
                return QApiErrorResponse(
                    error=error_result,
                    payload_attempted=payload_dict,
                )
        
        # Create a serialized copy of the payload for the API
        serialized_payload = {}

        # Add domain and metric
        serialized_payload["domain"] = payload.domain
        serialized_payload["metric"] = payload.metric

        # Process interval - ensure we convert datetimes to strings
        logger.info(
            f"QAPI Input: Original interval (IST expected): Start={payload.interval.start}, End={payload.interval.end}"
        )
        interval_dict = {}
        
        # Handle datetime conversion properly for IST format
        def convert_to_utc_string(time_input):
            """Convert various time formats to UTC string"""
            if isinstance(time_input, str):
                # Handle IST timezone format (+05:30)
                if '+05:30' in time_input:
                    # Parse IST datetime and convert to UTC
                    from dateutil import parser
                    dt = parser.parse(time_input)
                    utc_dt = dt.astimezone(pytz.UTC)
                    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    # Try to use the existing ist_to_utc function
                    return ist_to_utc(time_input)
            else:
                # It's likely a datetime object
                return ist_to_utc(time_input)
        
        interval_dict["start"] = convert_to_utc_string(payload.interval.start)
        interval_dict["end"] = convert_to_utc_string(payload.interval.end)
        logger.info(
            f"QAPI Call: Converted interval (UTC): Start={interval_dict['start']}, End={interval_dict['end']}"
        )
        serialized_payload["interval"] = interval_dict

        # Process filters if present
        if payload.filters:
            try:
                # Handle single clause filters - no tree conversion needed
                if len(payload.filters.clauses) == 1:
                    # For single clause, just use the clause directly
                    clause = payload.filters.clauses[0]
                    serialized_payload["filters"] = {
                        "condition": clause.condition,
                        "field": clause.field,
                        "val": clause.val
                    }
                else:
                    # For multiple clauses, use the tree conversion
                    serialized_payload["filters"] = flat_filter_to_tree(payload.filters)
            except Exception as e:
                return QApiErrorResponse(
                    error=f"Failed to process filters: {str(e)}, Please check the logic in your filter. Eg. \"AND\" is not a correct value for logic, whereas \"(0 AND 1)\" is a correct value for logic.",
                    payload_attempted=payload.model_dump(),
                )

        # Process dimensions
        if isinstance(payload.dimensions, list):
            # If dimensions is a list, convert it to a list of strings
            serialized_payload["dimensions"] = [
                dim.model_dump(mode="json", by_alias=True) if isinstance(dim, DimensionObject) else dim
                for dim in payload.dimensions
            ]

        # Process sortedOn if present
        if payload.sortedOn:
            serialized_payload["sortedOn"] = payload.sortedOn
        
        # Handle metric filters if present
        if payload.metric_filters:
            metric_filters = payload.metric_filters
            print(f"[Q API TOOL] Processing {len(metric_filters)} metric filters")
            
            # Prepare the metric array with having conditions
            metrics_with_having = []
            
            # Step 1: Gather all required metrics (from metric parameter + metric_filters)
            requested_metrics = set()
            
            # Add metrics from the main metric parameter
            if isinstance(payload.metric, str):
                requested_metrics.add(payload.metric)
            elif isinstance(payload.metric, list):
                requested_metrics.update(payload.metric)
            else:
                requested_metrics.add(str(payload.metric))
            
            # Add metrics from filters (in case they reference metrics not in main parameter)
            for mf in metric_filters:
                requested_metrics.add(mf.metric)
            
            # Step 2: Group all filters by metric name
            filters_by_metric = {}
            for mf in metric_filters:
                if mf.metric not in filters_by_metric:
                    filters_by_metric[mf.metric] = []
                filters_by_metric[mf.metric].append({
                    "field": mf.metric,
                    "val": mf.value,
                    "condition": mf.condition
                })
            
            # Step 3: Build the final metrics array
            for metric_name in requested_metrics:
                if metric_name in filters_by_metric:
                    # Add one metric object for each having condition
                    for having_condition in filters_by_metric[metric_name]:
                        metrics_with_having.append({
                            "name": metric_name,
                            "having": having_condition
                        })
                else:
                    # Add metric without having condition
                    metrics_with_having.append({"name": metric_name})
            
            # Override the metric field with the array format
            serialized_payload["metric"] = metrics_with_having
            print(f"[Q API TOOL] Applied metric filters: {json.dumps(metrics_with_having, indent=2)}")
        
        # Check authorized merchants
        authorized_merchants = get_authorized_merchants(token_response)
        if authorized_merchants is not None:
            logger.info(
                f"Authorized merchants: {json.dumps(authorized_merchants)}"
            )
        
            filters = serialized_payload.get("filters", {})
            if filters:
                allowed_values = {"merchant_id": authorized_merchants}
                unauthorized_fields = validate_and_extract_fields(
                    filters, allowed_values
                )
                if unauthorized_fields.get("merchant_id"):
                    # Unauthorized Access Detected
                    unauthorized_access_message = (
                        "You are attempting to query data for a merchant ID that you are not authorized to access. "
                        "Please ensure that your request is aligned with your current merchant account permissions."
                    )
                    return QApiErrorResponse(
                        error=unauthorized_access_message,
                        payload_attempted=serialized_payload,
                    )
                
        # Call the internal analytics API
        logger.debug(f"QAPI Call: Sending payload: {serialized_payload}")
        response = requests.post(
            "https://portal.juspay.in/api/q/query",
            data=json_dumps_with_datetime(serialized_payload),
            headers={
                "X-Web-LoginToken": web_login_token,
                "Content-Type": "application/json",
            },
        )
        text = response.text
        
        try:
            err_obj = json.loads(text)
            if isinstance(err_obj, dict) and "error" in err_obj:
                # flatten nested error → string
                err_msg = err_obj["error"]
                if not isinstance(err_msg, str):
                    err_msg = json.dumps(err_msg)
                return QApiErrorResponse(
                    error=err_msg,
                    payload_attempted=serialized_payload,
                )
        except ValueError:
            pass
        
        logger.info(f"QAPI Response Raw (IST expected): {text}")
        response.raise_for_status()  # Raise exception for HTTP errors

        # Parse JSONL response
        response_json = [json.loads(line) for line in text.splitlines()]
        validated_response = QApiSuccessResponse.model_validate(response_json)
        logger.info(
            f"QAPI Return: Parsed response (IST expected): {validated_response}"
        )
        return validated_response
    except Exception as e:
        logger.error(f"Error calling query API: {str(e)}")
        return QApiErrorResponse(
            error=f"Failed to execute query: {str(e)}",
            payload_attempted=(
                serialized_payload
                if "serialized_payload" in locals()
                else payload.model_dump()
            ),
        )


def _validate_params(payload_dict: Mapping[str, Any]) -> None:
    """Validate all parameters and raise detailed errors if invalid.
    
    Args:
        payload_dict: The arguments mapping to validate
        
    Raises:
        ValueError: With detailed error information if validation fails
    """
    # Define valid metrics and dimensions for validation
    valid_metrics = [
        "total_amount", "success_volume", "success_rate", "avg_ticket_size",
        "conflict_txn_rate", "average_latency", "order_with_transactions", "order_with_transactions_gmv"
    ]
    
    # All valid dimensions - comprehensive list
    valid_dimensions = {
        "entire_payment_flow", "merchant_id", "ord_currency", "payment_gateway",
        "payment_instrument_group", "payment_method_type", "actual_order_status",
        "actual_payment_status", "allowed_requeue", "auth_type", "bank", "bank_name",
        "business_region", "card_bin", "card_brand", "card_exp_month", "card_exp_year",
        "card_issuer_country", "card_last_four_digits", "card_sub_type", "card_type",
        "consent_page", "create_order_api_tag", "currency", "emi", "emi_bank",
        "emi_tenure", "emi_type", "error_message", "gateway", "gateway_before_evaluation",
        "gateway_reference_id", "godel_server_flow_state", "godel_server_scrape_state",
        "industry", "is_business_retry", "is_cvv_less_txn", "is_gateway_switched",
        "is_internally_retried", "is_notification_retried", "is_offer_txn",
        "is_requeued_order", "is_retargeted_order", "is_retried_order",
        "is_technical_retry", "is_token_bin", "is_tokenized", "is_upicc",
        "issuer_token_reference", "issuer_tokenization_consent_failure_reason",
        "juspay_bank_code", "juspay_dotp", "juspay_error_code", "juspay_error_message",
        "juspay_response_code", "juspay_response_message", "lob",
        "mandate_execute_req_to_pg", "mandate_execute_retried", "mandate_feature",
        "mandate_frequency", "mandate_source_object", "mandate_status", "merchant_name",
        "notification_req_to_pg", "notification_status", "notification_status_source",
        "order_fulfillment_status", "order_source_object", "order_source_object_id",
        "order_status", "order_type", "original_card_isin", "os", "payment_flow",
        "payment_link_channels", "payment_link_sent", "payment_method",
        "payment_method_subtype", "payment_status", "platform", "prev_gateway_resp_code",
        "prev_gateway_resp_message", "prev_order_status", "prev_txn_status",
        "previous_gateway_resp_code", "previous_gateway_resp_message",
        "previous_order_status", "previous_txn_status", "priority_logic_tag",
        "requeue_count", "reseller_id", "resp_code", "resp_message", "run_day_ist",
        "run_hour_ist", "run_month_ist", "run_week_ist", "source_object",
        "status_sync_source", "stored_card_vault_provider", "ticket_size",
        "token_reference", "token_repeat", "tokenization_consent",
        "tokenization_consent_failure_reason", "tokenization_consent_ui_presented",
        "tokenization_eligibility", "tokenized_flow", "txn_conflict", "txn_flow_type",
        "txn_last_updated", "txn_latency_enum", "txn_object_type", "txn_source_object",
        "txn_type", "udf1", "udf10", "udf2", "udf3", "udf4", "udf5", "udf6", "udf7",
        "udf8", "udf9", "unified_response_category", "use_merchant_proxy", "user_opt_in",
        "using_stored_card", "using_token",
    }
    
    # Validate interval is provided
    interval = payload_dict.get("interval")
    if not interval:
        error_msg = "interval is required but not provided"
        print(f"[Q API TOOL] Validation error: {error_msg}")
        detailed_error = {
            "error": "Missing required parameter",
            "details": error_msg,
            "fix_instructions": {
                "issue": "Interval is mandatory for all queries",
                "solution": "Provide an interval with start and end timestamps",
                "example": {
                    "interval": {
                        "start": "2024-05-15T00:00:00Z",
                        "end": "2024-05-15T23:59:59Z"
                    }
                },
                "note": "If user doesn't specify time, use 12am of the same day to current time"
            }
        }
        raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Validate interval has both start and end
    if not interval.get("start") or not interval.get("end"):
        error_msg = "interval must have both 'start' and 'end' timestamps"
        print(f"[Q API TOOL] Validation error: {error_msg}")
        detailed_error = {
            "error": "Invalid interval format",
            "details": error_msg,
            "fix_instructions": {
                "issue": "Interval is missing start or end timestamp",
                "solution": "Both start and end are required",
                "provided": interval,
                "example": {
                    "interval": {
                        "start": "2024-05-15T00:00:00Z",
                        "end": "2024-05-15T23:59:59Z"
                    }
                }
            }
        }
        raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Validate interval end is not before start
    try:
        from dateutil.parser import parse as parse_date
        start_time = parse_date(interval["start"])
        end_time = parse_date(interval["end"])
        
        if end_time < start_time:
            error_msg = "interval end time cannot be before start time"
            print(f"[Q API TOOL] Validation error: {error_msg}")
            detailed_error = {
                "error": "Invalid interval range",
                "details": error_msg,
                "fix_instructions": {
                    "issue": "The end timestamp is before the start timestamp",
                    "solution": "Ensure the end timestamp is after the start timestamp",
                    "provided": {
                        "start": interval["start"],
                        "end": interval["end"]
                    },
                    "example": {
                        "interval": {
                            "start": "2024-05-15T00:00:00Z",
                            "end": "2024-05-15T23:59:59Z"
                        }
                    },
                    "note": "Start time should be earlier than end time"
                }
            }
            raise ValueError(json.dumps(detailed_error, indent=2))
    except ValueError as ve:
        # Re-raise our validation errors
        if "Invalid interval range" in str(ve):
            raise
        # Handle date parsing errors
        error_msg = f"invalid timestamp format in interval: {str(ve)}"
        print(f"[Q API TOOL] Validation error: {error_msg}")
        detailed_error = {
            "error": "Invalid timestamp format",
            "details": error_msg,
            "fix_instructions": {
                "issue": "One or both timestamps in the interval are not in a valid format",
                "solution": "Use ISO 8601 format for timestamps",
                "provided": interval,
                "example": {
                    "interval": {
                        "start": "2024-05-15T00:00:00Z",
                        "end": "2024-05-15T23:59:59Z"
                    }
                },
                "note": "Timestamps should be in ISO 8601 format with timezone (Z for UTC)"
            }
        }
        raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Validate metric is provided
    metric = payload_dict.get("metric")
    if not metric:
        error_msg = "metric is required but not provided"
        print(f"[Q API TOOL] Validation error: {error_msg}")
        detailed_error = {
            "error": "Missing required parameter",
            "details": error_msg,
            "fix_instructions": {
                "issue": "Metric is mandatory for all queries",
                "solution": "Provide one of the valid metrics",
                "valid_metrics": valid_metrics,
                "example": {
                    "metric": "order_with_transactions"
                }
            }
        }
        raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Handle both single metric and list of metrics
    metrics_to_check = [metric] if not isinstance(metric, list) else metric
    
    # Validate each metric is actually a metric and not a dimension
    for single_metric in metrics_to_check:
        if single_metric not in valid_metrics:
            # Check if they passed a dimension as metric
            if single_metric in valid_dimensions:
                error_msg = f"'{single_metric}' is a dimension, not a metric"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Invalid metric - dimension used instead",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": f"You used '{single_metric}' as a metric, but it's actually a dimension field",
                        "solution": "Use a valid metric for aggregation and move dimension fields to 'dimensions' array or 'filters'",
                        "valid_metrics": valid_metrics,
                        "example_correction": {
                            "wrong": {"metric": single_metric},
                            "correct": {
                                "metric": "order_with_transactions",
                                "dimensions": [single_metric]
                            }
                        },
                        "note": "Metrics are aggregated values (sums, rates, averages), while dimensions are fields you group by or filter on"
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
            else:
                # Unknown metric
                error_msg = f"'{single_metric}' is not a valid metric"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Invalid metric",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": f"'{single_metric}' is not recognized as a valid metric",
                        "solution": "Use one of the valid metrics",
                        "valid_metrics": valid_metrics,
                        "example": {
                            "metric": "order_with_transactions"
                        }
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Validate dimensions array if provided
    if payload_dict.get("dimensions"):
        dimensions = payload_dict["dimensions"]
        if not isinstance(dimensions, list):
            error_msg = "dimensions must be a list/array"
            print(f"[Q API TOOL] Validation error: {error_msg}")
            detailed_error = {
                "error": "Invalid dimensions format",
                "details": error_msg,
                "fix_instructions": {
                    "issue": "Dimensions must be provided as a list",
                    "solution": "Use an array format for dimensions",
                    "example": {
                        "dimensions": ["payment_gateway", "card_brand"]
                    }
                }
            }
            raise ValueError(json.dumps(detailed_error, indent=2))
        
        # Check each dimension
        invalid_dimensions = []
        metrics_in_dimensions = []
        
        for dim in dimensions:
            if isinstance(dim, str):
                if dim in valid_metrics:
                    metrics_in_dimensions.append(dim)
                elif dim not in valid_dimensions:
                    invalid_dimensions.append(dim)
        
        if metrics_in_dimensions:
            error_msg = f"Metrics cannot be used as dimensions: {metrics_in_dimensions}"
            print(f"[Q API TOOL] Validation error: {error_msg}")
            detailed_error = {
                "error": "Invalid dimensions - metrics used instead",
                "details": error_msg,
                "fix_instructions": {
                    "issue": f"You included metrics ({metrics_in_dimensions}) in the dimensions array",
                    "solution": "Metrics are aggregated values and cannot be used as dimensions. Remove them from dimensions array.",
                    "example_correction": {
                        "wrong": {
                            "metric": "order_with_transactions",
                            "dimensions": dimensions
                        },
                        "correct": {
                            "metric": "order_with_transactions",
                            "dimensions": [d for d in dimensions if d not in metrics_in_dimensions]
                        }
                    },
                    "note": "Dimensions are fields you group by (like payment_gateway, card_brand). Metrics are the values you aggregate (like total_amount, success_rate)."
                },
                "metrics_found": metrics_in_dimensions,
                "valid_dimensions_examples": ["payment_gateway", "card_brand", "merchant_id", "bank", "error_message"]
            }
            raise ValueError(json.dumps(detailed_error, indent=2))
        
        if invalid_dimensions:
            error_msg = f"Invalid dimensions: {invalid_dimensions}"
            print(f"[Q API TOOL] Validation error: {error_msg}")
            detailed_error = {
                "error": "Invalid dimensions",
                "details": error_msg,
                "fix_instructions": {
                    "issue": f"The following dimensions are not valid: {invalid_dimensions}",
                    "solution": "Use valid dimension fields or run field_value_discovery tool to find available dimensions",
                    "common_dimensions": ["payment_gateway", "card_brand", "merchant_id", "bank", "payment_method_type", "error_message"],
                    "hint": "Check spelling and case sensitivity. Use field_value_discovery tool to explore available fields."
                }
            }
            raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Validate filters structure if provided - for flat filters
    if payload_dict.get("filters"):
        filters = payload_dict["filters"]
        if not isinstance(filters, dict):
            error_msg = "filters must be a dictionary"
            print(f"[Q API TOOL] Validation error: {error_msg}")
            detailed_error = {
                "error": "Invalid filter format",
                "details": error_msg,
                "fix_instructions": {
                    "issue": "Filters must be in FlatFilter format",
                    "solution": "Use the correct structure with 'clauses' and 'logic'",
                    "example": {
                        "filters": {
                            "clauses": [
                                {"field": "payment_gateway", "condition": "In", "val": ["RAZORPAY"]}
                            ],
                            "logic": "0"
                        }
                    }
                }
            }
            raise ValueError(json.dumps(detailed_error, indent=2))
        
        # Check for required fields in filter
        if "clauses" not in filters or "logic" not in filters:
            error_msg = "filters must have both 'clauses' and 'logic' fields"
            print(f"[Q API TOOL] Validation error: {error_msg}")
            detailed_error = {
                "error": "Invalid filter structure",
                "details": error_msg,
                "fix_instructions": {
                    "issue": "Filter is missing required fields",
                    "solution": "Include both 'clauses' array and 'logic' string",
                    "provided": filters,
                    "example": {
                        "filters": {
                            "clauses": [
                                {"field": "card_brand", "condition": "In", "val": ["VISA"]}
                            ],
                            "logic": "0"
                        }
                    }
                }
            }
            raise ValueError(json.dumps(detailed_error, indent=2))
        
        # Validate each clause to ensure no metrics are used as filter fields
        if isinstance(filters.get("clauses"), list):
            for idx, clause in enumerate(filters["clauses"]):
                if isinstance(clause, dict) and "field" in clause:
                    field = clause["field"]
                    
                    # Check if a metric is being used as a filter field
                    if field in valid_metrics:
                        error_msg = f"Metric '{field}' cannot be used as a filter field in clause {idx}"
                        print(f"[Q API TOOL] Validation error: {error_msg}")
                        detailed_error = {
                            "error": "Invalid filter field - metric used instead of dimension",
                            "details": error_msg,
                            "fix_instructions": {
                                "issue": f"You used metric '{field}' as a filter field, but metrics cannot be filtered directly",
                                "solution": "Use dimension fields for filtering. Metrics are aggregated results.",
                                "explanation": {
                                    "metrics": "Aggregated values like total_amount, success_rate - these are calculated results",
                                    "dimensions": "Fields you can filter/group by like payment_gateway, card_brand, merchant_id"
                                },
                                "example_correction": {
                                    "wrong": {
                                        "field": field,
                                        "condition": clause.get("condition"),
                                        "val": clause.get("val")
                                    },
                                    "correct_examples": [
                                        {"field": "payment_gateway", "condition": "In", "val": ["RAZORPAY"]},
                                        {"field": "ticket_size", "condition": "Greater", "val": 1000},
                                        {"field": "merchant_id", "condition": "In", "val": ["merchant_123"]}
                                    ]
                                },
                                "hint": "If you want to filter by transaction amounts, use 'ticket_size' dimension instead"
                            },
                            "clause_index": idx,
                            "provided_clause": clause
                        }
                        raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Validate sortedOn if provided
    if payload_dict.get("sortedOn"):
        sorted_on = payload_dict["sortedOn"]
        if isinstance(sorted_on, dict) and "sortDimension" in sorted_on:
            sort_dimension = sorted_on["sortDimension"]
            
            # Check if sortDimension is actually a metric
            if sort_dimension not in valid_metrics:
                # Check if they used a dimension instead
                if sort_dimension in valid_dimensions:
                    error_msg = f"sortDimension '{sort_dimension}' is a dimension field, not a metric"
                    print(f"[Q API TOOL] Validation error: {error_msg}")
                    detailed_error = {
                        "error": "Invalid sortDimension - dimension used instead of metric",
                        "details": error_msg,
                        "fix_instructions": {
                            "issue": f"You used dimension '{sort_dimension}' for sorting, but sortDimension must be a metric",
                            "solution": "Use a metric for sortDimension. Results are sorted by aggregated metric values.",
                            "valid_metrics": valid_metrics,
                            "example_correction": {
                                "wrong": {
                                    "sortedOn": {
                                        "sortDimension": sort_dimension,
                                        "ordering": sorted_on.get("ordering", "Desc")
                                    }
                                },
                                "correct": {
                                    "sortedOn": {
                                        "sortDimension": "order_with_transactions",
                                        "ordering": "Desc"
                                    }
                                }
                            },
                            "note": "Sort by metrics (aggregated values) not dimensions (grouping fields)"
                        }
                    }
                    raise ValueError(json.dumps(detailed_error, indent=2))
    
    # Validate metric_filters if provided
    if payload_dict.get("metric_filters"):
        metric_filters = payload_dict["metric_filters"]
        if not isinstance(metric_filters, list):
            error_msg = "metric_filters must be a list/array"
            print(f"[Q API TOOL] Validation error: {error_msg}")
            detailed_error = {
                "error": "Invalid metric_filters format",
                "details": error_msg,
                "fix_instructions": {
                    "issue": "metric_filters must be provided as a list",
                    "solution": "Use an array format for metric filters",
                    "example": {
                        "metric_filters": [
                            {"metric": "success_rate", "condition": "Less", "value": 80.0}
                        ]
                    }
                }
            }
            raise ValueError(json.dumps(detailed_error, indent=2))
        
        # Validate each metric filter
        for idx, mf in enumerate(metric_filters):
            if not isinstance(mf, dict):
                error_msg = f"metric_filter at index {idx} must be a dictionary"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Invalid metric filter format",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": f"Metric filter at index {idx} is not properly formatted",
                        "solution": "Each metric filter must be an object with metric, condition, and value",
                        "example": {
                            "metric": "success_rate",
                            "condition": "Less",
                            "value": 80.0
                        }
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
            
            # Check required fields
            metric_filter_metric = mf.get("metric")
            condition = mf.get("condition")
            value = mf.get("value")
            
            if not metric_filter_metric:
                error_msg = f"metric_filter at index {idx} is missing 'metric' field"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Missing required field in metric filter",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": "Every metric filter must specify which metric to filter on",
                        "solution": "Add the 'metric' field",
                        "valid_metrics": valid_metrics,
                        "example": {
                            "metric": "success_rate",
                            "condition": "Less",
                            "value": 80.0
                        }
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
            
            if not condition:
                error_msg = f"metric_filter at index {idx} is missing 'condition' field"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Missing required field in metric filter",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": "Every metric filter must specify a comparison condition",
                        "solution": "Add the 'condition' field",
                        "valid_conditions": ["Greater", "GreaterThanEqual", "Less", "LessThanEqual"],
                        "example": {
                            "metric": "success_rate",
                            "condition": "Less",
                            "value": 80.0
                        }
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
            
            if value is None:
                error_msg = f"metric_filter at index {idx} is missing 'value' field"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Missing required field in metric filter",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": "Every metric filter must specify a value to compare against",
                        "solution": "Add the 'value' field",
                        "example": {
                            "metric": "success_rate",
                            "condition": "Less",
                            "value": 80.0
                        }
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
            
            # Validate metric is actually a metric
            if metric_filter_metric not in valid_metrics:
                # Check if they passed a dimension as metric
                if metric_filter_metric in valid_dimensions:
                    error_msg = f"'{metric_filter_metric}' is a dimension, not a metric (in metric_filter {idx})"
                    print(f"[Q API TOOL] Validation error: {error_msg}")
                    detailed_error = {
                        "error": "Invalid metric in metric_filter - dimension used instead",
                        "details": error_msg,
                        "fix_instructions": {
                            "issue": f"You used '{metric_filter_metric}' as a metric in metric_filter, but it's actually a dimension field",
                            "solution": "metric_filters can only filter on aggregated metric values, not dimensions",
                            "valid_metrics": valid_metrics,
                            "example_correction": {
                                "wrong": {"metric": metric_filter_metric, "condition": condition, "value": value},
                                "note": "To filter on dimensions, use the 'filters' parameter instead"
                            }
                        }
                    }
                    raise ValueError(json.dumps(detailed_error, indent=2))
                else:
                    error_msg = f"'{metric_filter_metric}' is not a valid metric (in metric_filter {idx})"
                    print(f"[Q API TOOL] Validation error: {error_msg}")
                    detailed_error = {
                        "error": "Invalid metric in metric_filter",
                        "details": error_msg,
                        "fix_instructions": {
                            "issue": f"'{metric_filter_metric}' is not recognized as a valid metric",
                            "solution": "Use one of the valid metrics",
                            "valid_metrics": valid_metrics,
                            "example": {
                                "metric": "success_rate",
                                "condition": "Less",
                                "value": 80.0
                            }
                        }
                    }
                    raise ValueError(json.dumps(detailed_error, indent=2))
            
            # Validate condition
            valid_metric_conditions = ["Greater", "GreaterThanEqual", "Less", "LessThanEqual"]
            if condition not in valid_metric_conditions:
                error_msg = f"'{condition}' is not a valid condition for metric filters (in metric_filter {idx})"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Invalid condition in metric_filter",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": f"'{condition}' is not a valid comparison operator for metric filters",
                        "solution": "Use one of the valid numeric comparison conditions",
                        "valid_conditions": valid_metric_conditions,
                        "example": {
                            "metric": "success_rate",
                            "condition": "Less",
                            "value": 80.0
                        },
                        "note": "Metric filters only support numeric comparisons since they filter on aggregated values"
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
            
            # Validate value is numeric
            if not isinstance(value, (int, float)):
                error_msg = f"value must be a number in metric_filter {idx}"
                print(f"[Q API TOOL] Validation error: {error_msg}")
                detailed_error = {
                    "error": "Invalid value type in metric_filter",
                    "details": error_msg,
                    "fix_instructions": {
                        "issue": f"Value '{value}' is not a number",
                        "solution": "metric_filter values must be numeric",
                        "example": {
                            "metric": "success_rate",
                            "condition": "Less",
                            "value": 80.0
                        }
                    }
                }
                raise ValueError(json.dumps(detailed_error, indent=2))
            
            # Validate percentage metrics have values between 0-100
            percentage_metrics = ["success_rate", "conflict_txn_rate"]
            if metric_filter_metric in percentage_metrics:
                if not (0 <= value <= 100):
                    error_msg = f"{metric_filter_metric} value must be between 0 and 100 (got {value} in metric_filter {idx})"
                    print(f"[Q API TOOL] Validation error: {error_msg}")
                    detailed_error = {
                        "error": "Invalid percentage value in metric_filter",
                        "details": error_msg,
                        "fix_instructions": {
                            "issue": f"{metric_filter_metric} is a percentage metric but value {value} is outside 0-100 range",
                            "solution": "Use values between 0 and 100 for percentage metrics",
                            "example": {
                                "metric": metric_filter_metric,
                                "condition": condition,
                                "value": min(100, max(0, value))
                            }
                        }
                    }
                    raise ValueError(json.dumps(detailed_error, indent=2))


# Helper function to get all metric and dimension names for the prompt
async def _validate_filter_values(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Validate filter values using field guards and fuzzy matching.
    
    Returns a dict with validation results:
    {
        "valid": bool,
        "errors": [list of error messages],
        "suggestions": {field: [suggested values]}
    }
    """
    logger.info("[Q API TOOL] Validating filter values")
    
    # Get field guards from the field_guards module
    try:
        from .field_value_config import FIELD_GUARDS
        field_guards = FIELD_GUARDS
        logger.info(f"[Q API TOOL] Using field guards: {len(field_guards)} fields")
    except ImportError:
        logger.warning("[Q API TOOL] Failed to import field guards, skipping validation")
        return {"valid": True, "errors": [], "suggestions": {}}
    
    # Extract all field-value pairs from clauses
    field_values_to_check = []
    
    if "clauses" in filters:
        for idx, clause in enumerate(filters["clauses"]):
            field = clause.get("field")
            condition = clause.get("condition")
            val = clause.get("val")
            
            # Only validate "In" conditions with string array values
            if field and condition == "In":
                # Check if this field has known values
                if field in field_guards:
                    # Handle both simple array and ValObject format
                    if isinstance(val, list):
                        # Simple array of values
                        field_values_to_check.append({
                            "clause_idx": idx,
                            "field": field,
                            "values": val,
                            "valid_values": field_guards[field]
                        })
                    elif isinstance(val, dict) and "limit" in val:
                        # ValObject format for top-N - no validation needed
                        logger.info(f"[Q API TOOL] Skipping validation for top-N filter on field '{field}'")
    
    # If no fields to validate, return as valid
    if not field_values_to_check:
        return {"valid": True, "errors": [], "suggestions": {}}
    
    validation_result = {
        "valid": True,
        "errors": [],
        "suggestions": {}
    }
    
    def find_suggestions(query: str, candidates: list, max_results: int = 5) -> list:
        """Find best matching values using fuzzy matching."""
        # Convert to lowercase for case-insensitive matching
        query_lower = query.lower()
        
        # Calculate similarity scores
        scored_candidates = []
        for candidate in candidates:
            if isinstance(candidate, str):
                candidate_lower = candidate.lower()
                # Check for exact substring match first (highest priority)
                if query_lower in candidate_lower:
                    # Boost score based on position (earlier matches are better)
                    position_boost = 1.0 - (candidate_lower.index(query_lower) / len(candidate_lower))
                    score = 0.8 + (0.2 * position_boost)
                else:
                    # Use SequenceMatcher for fuzzy matching
                    score = simple_fuzzy_score(query, candidate)
                scored_candidates.append((candidate, score))
        
        # Sort by score (descending) and take top N
        ranked = sorted(scored_candidates, key=lambda x: x[1], reverse=True)
        return [c[0] for c in ranked[:max_results]]
    
    # Check each field's values
    for check in field_values_to_check:
        field = check["field"]
        provided_values = check["values"]
        valid_values = check["valid_values"]
        clause_idx = check["clause_idx"]
        
        # Quick check if all values are valid
        invalid_values = []
        for value in provided_values:
            # Skip null values (they're valid for NotIn conditions)
            if value is None:
                continue
            if value not in valid_values:
                invalid_values.append(value)
        
        if invalid_values:
            logger.info(f"[Q API TOOL] Found invalid values for field '{field}': {invalid_values}")
            validation_result["valid"] = False
            
            # Use fuzzy matching to find suggestions
            all_suggestions = []
            for invalid_val in invalid_values:
                if isinstance(invalid_val, str):
                    suggestions = find_suggestions(invalid_val, valid_values, max_results=5)
                    all_suggestions.extend(suggestions)
            
            # Deduplicate suggestions while preserving order
            unique_suggestions = []
            seen = set()
            for suggestion in all_suggestions:
                if suggestion not in seen:
                    seen.add(suggestion)
                    unique_suggestions.append(suggestion)
            
            # Limit to top 5 unique suggestions
            unique_suggestions = unique_suggestions[:5]
            
            # Add error with suggestions
            error_msg = f"Invalid values for field '{field}' in clause {clause_idx}: {invalid_values}"
            if unique_suggestions:
                error_msg += f". Did you mean one of these? {unique_suggestions}"
                validation_result["suggestions"][field] = unique_suggestions
            else:
                # Show some valid examples from the field guards
                examples = valid_values[:10] if len(valid_values) > 10 else valid_values
                error_msg += f". Valid values include: {examples}"
                validation_result["suggestions"][field] = examples
            
            validation_result["errors"].append(error_msg)
    
    return validation_result


def get_metric_and_dimension_lists():
    """Get complete lists of metrics and dimensions including aliases."""
    # Get all metric values from the Literal type
    metric_list = list(MetricEnum.__args__)
    
    # Get all dimension values from the Literal type
    dimension_list = list(DimensionString.__args__)
    
    return {
        "metricList": metric_list,
        "dimensionList": dimension_list
    }



async def q_api(
    params: FunctionCallParams
) -> QApiResponse:
    """
    Calls an internal /q analytics API with the provided analytics payload. 
    REMEMBER! try to apply all required the filters, dimensions, etc. in least amount of function tool calls.
    CAN do more calls if all the filters, dimensions, etc. in the query's context are not possible in a single call.

    IMPORTANT: Before using q_api with filters, ALWAYS call field_value_discovery first!
    - If your query uses ANY filter on supported dimensions (like bank, payment_gateway, card_brand, platform, order_status, payment_status, actual_order_status, actual_payment_status, etc.), you MUST call field_value_discovery tool first to validate the filter values.
    - Only skip field_value_discovery for static identifiers like merchant_id, error_message, card_bin.
    - This ensures your filter values are correct and prevents validation errors.

    USEFUL SYNONYMS:
      Revenue = Processed amount = GMV = total_amount (successful orders only)
      Netbanking, NB
      BNPL, Pay Later
      EMI, Instalments
      THREE_DS , 3DS
      THREE_DS_2, 3DS
      UPI QR, QR , Scan n Pay
      UPI COLLECT , COLLECT
      Wallets, Prepaid Instrument , PPI
      Network, Card Brand, Brand, Card network
      UPI INTENT , INTENT , PAY, Pay using App, UPI_PAY (If user is asking about UPI intent / intent transactions, always set payment_method_subtype to UPI_PAY)
      Payment Gateway, Gateway , Aggregator, PG
      Auto Pay, Mandate, subscriptions, recurring payment
      Payment Instrument, Payment Instrument Group, Payment Containers
      Success rate, Conversion rate , SR, S.R , Payment SR , Order SR , Order Success Rate

    Args:
        filters: A dict representing the 'filters' section with valid field values from the schema.
                REQUIRED FORMAT: Filters must be in FlatFilter format with 'clauses' array and 'logic' string:
                {
                  "clauses": [
                    {"field": "payment_gateway", "condition": "In", "val": ["RAZORPAY"]},
                    {"field": "card_brand", "condition": "In", "val": ["VISA"]}
                  ],
                  "logic": "0 AND 1"
                }
                
                For single filter: {"clauses": [{"field": "payment_gateway", "condition": "In", "val": ["RAZORPAY"]}], "logic": "0"}
                
                IMPORTANT NOTES:
                  - Using "limit" in Filters:
                    -> If query asks to *list* or *give* or *show* the possible values/enums of a "dimension", then **always** apply a limit = 10 for limiting the number of rows.
                    -> Add \`limit\` in the filter when the query requests to limit the number of rows by otuput, i.e., "top 'n'..." in the user query means apply limit as 'n'. However if user is just asking for top, then assume limit as 1 (VERY IMPORTANT) 
                    -> For example, the query: _"Give me a breakdown of the transaction volume by payment method for the top error message"_ or _"Give me the top error message yesterday"_ requires filtering for the top error message while providing the breakdown by payment method. In such cases:
                    -> if anywhere in the query "top 3", "top 2", "top", etc are present, ALWAYS APPLY \`limit\` in "filter". 
                    -> Use \`limit\` to restrict the filter to the top error message, e.g.,:
                  \`\`\`json
                     # Example filter for top error message 
                        {
                            "filters": {
                            "and": {
                                "left": {
                                "condition": "In",
                                "field": "error_message",
                                "val": {
                                    "sortedOn": {
                                    "sortDimension": "order_with_transactions",
                                    "ordering": "Desc"
                                    },
                                    "limit": Int
                                }
                                },
                                "right": {
                                "condition": "NotIn",
                                "field": "error_message",          
                                "val": [null]
                                }
                            }
                            }
                        }                     
                  \`\`\`
                 - ALWAYS add a filter to exclude null values when querying for top values of any dimension/field. This ensures that null values don't appear in the top results. For example, when asked for "top payment gateways", always include a filter like \`"condition": "NotIn", "field": "payment_gateway", "val": [null]\` in combination with the limit filter. _MAKE SURE TO ALWAYS FILTER OUT NULL VALUES NOT EMPTY STRING ""_
                 - Consider Conversational Context: Carefully examine if the current user query is a continuation or refinement of a previous query within the ongoing conversation. If the current query lacks specific filter details but appears to build upon earlier messages, actively infer the necessary filters from the established conversational context. For example, if the user first asks "What is the SR for Razorpay?" and then follows up with "Break it down by card type", the second query implicitly requires the \`payment_gateway\` filter for "RAZORPAY" from the first query.
                 - Use payment_instrument_group, payment_method_subtype, payment_method_type to find out type of payment instrument used For eg. credit card, debit card, upi, etc..
                 - You are not allowed to use any field apart from the provided possible enum values in the JSON schema.
                 - Do not return an empty filter object.
                 - After generating the filter, check each key and match it with the allowed JSON schema. Do not return filters outside of the JSON schema.
                 - Return only the JSON filter in the output; do not return any other text apart from the generated filter.
                 - If the query asks details about a specific merchant, add the filter for merchant_id. (Note: merchant_id should be lowercase and without spaces)
                 - If the query specifies EMI transactions, always set filter for emi_bank to be not null!
                 - To filter transactions for a specific card type (Credit Card/Debit Card) filter on "payment_instrument_group"!
                 - NOTE: For handling queries regarding payments through UPI apps, set payment_method_subtype filter on UPI_PAY. UPI App name is stored in the "bank" field/dimension. 
                 - When asked about payments through UPI handle/VPA/UPI ID/UPI Address (eg. @icici, @okicici, @okhdfcbank, @ptyes), set payment_method_subtype filter on UPI_COLLECT. UPI handle is stored in "bank" field. (example - 'paytm handle' in the query refers to "Paytm" in the "bank" fieldDimensionEnum and set payment_method_subtype filter on UPI_COLLECT)
                 - When asked about transactions going through a specific wallet, set payment_method_type filter on WALLET and the wallet name is stored in "bank" field.
                 - NOTE: When asked to filter on order success/failure, always use "payment_status" in dimensions or filter fields. If the user wants more fine grained filtering then use actual_payment_status otherwise always default to "payment_status". Supported values for payment_status: ["SUCCESS", "FAILURE", "PENDING"]
                 - NOTE: When asked for upi credit card transactions, NEVER \`payment_instrument_group\` = \`CREDIT CARD\`, always use \`is_upicc\` = \`true\`!!!
                 - You should not generate filters for time intervals! Time intervals are handled by interval section of the payload, not filters!
                 - When calculating the success rate, do not apply a filter for payment_status: SUCCESS. The success rate is a metric that should be handled by another component of the system. Avoid adding any filters specifically for success rate calculations. However, you may apply filters for other parts of the query as needed. For example, for a query like "Can you provide the success rate for Visa cards segmented by payment gateway?", the filter should include only "card_brand" in ["VISA"] and not "payment_status" in ["SUCCESS"]. Remember this important instruction!
                 - When asked about payment failure reason, refer to the error_message field.
        metric: A string or list of strings representing metric(s) to be queried. 
               IMPORTANT NOTES:
                 [
                  "total_amount", // total amount (in amount value) of success orders ONLY, ALSO KNOWN AS GMV or Processed Amount (Successful orders only)
                  "success_volume", // total number of success orders (Total number of orders against which a transaction has been attempted by the customer and any one of the transactions was successful)
                  "success_rate", // total number of success orders / total number of orders
                  // Use "total_amount" only for successful transactions. If the query doesn't specify success explicitly, still assume success and choose "total_amount".
                  "avg_ticket_size", // total ticket amount of success orders / total number of success orders
                  "conflict_txn_rate", // total number of conflict orders / total number of orders (Dont use it unless explicitly asked about conflicted orders/transactions, NEVER use it to identify failed transactions/error message filter)
                  "average_latency", // total latency of success orders / total number of success orders
                  "order_with_transactions", // total number of orders against which at least one transaction has been attempted by the customer (includes 'success + pending + failure').
                  "order_with_transactions_gmv" // equivalent to total amount of ALL orders (success+failed+pending+created+others). Use this explicitly ONLY if the user clearly asks for "all orders", "total value of orders", or similar phrases.
                ]

                 - Important distinctions regarding "total amount"
                   -> \`total_amount\` represents the 'total amount of successful orders only' (also known as 'GMV' or 'Processed Amount').
                   -> \`order_with_transactions_gmv\` represents 'the total amount across ALL orders' (including 'failed, pending, and created' orders). Do not use this for GMV calculations.
                 - Consider Conversational Context: Carefully examine if the current user query is a continuation or refinement of a previous query within the ongoing conversation. If the current query lacks specific details about metrics, dimensions, or sorting, but appears to build upon earlier messages, actively infer these details from the established conversational context. For example, if the user first asks "What is the SR and volume for Razorpay?" and then follows up with "Show me the daily trend", the second query implicitly requires the \`metric\` for "SR and volume" from the first query, while adding the time dimension for the daily trend.
        dimensions: A list of dimension strings or dimension objects, default empty list
                  IMPORTANT NOTES:
                    - Critical Instruction: When the query asks for 'absolute values' (e.g., "How many?", "Number of?", "Total Number?", "What is?", "Top X?"), do not include \`granularity\`, \`intervalCol\`, or \`timeZone\` in the \`dimensions\`. Only include these when the query asks for a 'trend over time' (e.g., "Show me the daily trend of...", "How has X changed over time", "Trend of...", "Over time", "Per day", "Per hour"). **If the user explicitly asks for a "graph" or "chart", always treat it as a trend query and include granularity.** **This is the most important instruction.**

                    - Default Time Range: If no time range is specified in the query, use the default time range, which is '12:00 AM of the current day to the current timestamp'. In such cases, use a 'granularity' of \`"hour"\` instead of \`"day"\`. This ensures trends are broken down into hourly intervals when no specific time range is given.
                    - Time-Based Columns: Only use \`order_created_at\` as a value for \`intervalCol\`.
                    - Using Limit for Top Values: Add \`"limit": 1\` only when the query explicitly asks for the **single top value** of a dimension or metric, and the expected output from the analytics API is just 'one row'.
                      -> For example, in the query: _"What is the top error message yesterday based on transaction volume?"_, the response should include \`"limit": 1\` because only the single top error message is requested, also you have to use sortedOn to make sure you're getting the top error message.

                    - Specific Dimensions:
                      -> To view specific card types (Credit Card/Debit Card) for transactions, use \`"payment_instrument_group"\` as a dimension.
                      -> To get payment volume, use \`"order_with_transactions"\` instead of \`total_volume\`.
                      -> UPI apps are stored in the \`"bank"\` dimension.
        interval: A dict with 'start' and 'end' keys (ISO format: YYYY-MM-DDTHH:MM:SSZ).
                 IMPORTANT NOTES:
                   - If user doesn't explicitly mention the interval, assume interval to be 12am of the same day to current time. INTERVAL IS MANDATORY.
        sortedOn: (Optional) A dict specifying how to sort the results, if needed.
                 IMPORTANT NOTES:
                   - **For any query that will return more than one row** (i.e. whenever \`dimensions\` is non-empty and you're not explicitly limiting to a single result), you **must** include a top-level \`sortedOn\` object _outside_ of \`filters\`:
                       \`\`\`json
                       "sortedOn": {
                         "sortDimension": "<primary_metric>",
                         "ordering": "Desc"
                       }
                       \`\`\`
                       -> Use the first metric in your \`metric\` list as the \`sortDimension\` (or choose the metric most relevant to the user's request, e.g. \`success_rate\` if present).  
                       -> Always set \`"ordering": "Desc"\`.  
                   - The value of the "sortDimension" key MUST ONLY BE from the available \`metric\` values.                
                   - Value for "sortDimension" CANNOT BE "order_created_at".
                   - If you also use a \`"limit"\` inside \`filters\` (for a top-N within a breakdown), **still** keep the top-level \`sortedOn\` outside \`filters\`.

    Returns:
        A dictionary containing the API response from /q endpoint.

    Example:
        Basic success rate query:
        {
            "filters": {
            "field": "payment_gateway",
            "condition": "In",
            "val": ["RAZORPAY"]
            },
            "metric": "success_rate",
            "dimensions": ["payment_method_type"],
            "interval": {
            "start": "2024-03-01T00:00:00Z",
            "end": "2024-03-21T23:59:59Z"
            }
        }
    """
    try:
        # Extract arguments
        interval_data = params.arguments.get("interval")
        metric = params.arguments.get("metric") 
        dimensions = params.arguments.get("dimensions", [])
        filters = params.arguments.get("filters")
        sortedOn = params.arguments.get("sortedOn")
        metric_filters = params.arguments.get("metric_filters")
        
        logger.info(
            f"QAPI Tool Input: Interval={interval_data}, Metric={metric}, Dimensions={dimensions}, Filters={filters}, SortedOn={sortedOn}"
        )
        
        # Create Interval object from input
        if not interval_data:
            return QApiErrorResponse(
                error="interval is required",
                payload_attempted=params.arguments
            )
        
        interval = Interval(
            start=interval_data.get("start"),
            end=interval_data.get("end")
        )
        
        # Parse filters if provided
        parsed_filters = None
        if filters:
            try:
                parsed_filters = FlatFilter(**filters)
            except Exception as e:
                return QApiErrorResponse(
                    error=f"Invalid filter format: {str(e)}",
                    payload_attempted=params.arguments
                )
        
        # Parse metric filters if provided
        parsed_metric_filters = None
        if metric_filters:
            try:
                parsed_metric_filters = [MetricFilter(**mf) for mf in metric_filters]
            except Exception as e:
                return QApiErrorResponse(
                    error=f"Invalid metric filter format: {str(e)}",
                    payload_attempted=params.arguments
                )
        
        # Resolve aliases before creating the payload
        resolved_metric, resolved_dimensions, resolved_filters, resolved_sortedOn, transformation_map = resolve_aliases_in_payload(
            metric, dimensions or [], parsed_filters, sortedOn
        )
        

        if resolved_filters:
            # Extract fields and their values from the filter
            filter_field_values = extract_filter_fields_and_values(resolved_filters)
            logger.info(f"Filter fields and values to validate: {filter_field_values}")
            
           
        
        # Auto-include currency dimension for amount metrics
        resolved_dimensions = ensure_currency_dimension_for_amounts(resolved_metric, resolved_dimensions)

        # Construct the payload using the QApiPayload model with resolved aliases
        payload = QApiPayload(
            domain="kvorders",
            metric=resolved_metric,
            interval=interval,
            filters=resolved_filters,
            dimensions=resolved_dimensions,
            sortedOn=resolved_sortedOn,
            metric_filters=parsed_metric_filters,
        )

        # Log the payload for debugging
        logger.debug(f"QAPI Tool: Creating payload: {json.dumps(payload.model_dump())}")

        # Use euler_token as web_login_token and get real token_response
        if not euler_token:
            return QApiErrorResponse(
                error="Authentication token not available",
                payload_attempted=payload.model_dump()
            )
        
        # Validate token and get real token response
        try:
            token_response = validate_token(euler_token)
        except Exception as e:
            return QApiErrorResponse(
                error=f"Token validation failed: {str(e)}",
                payload_attempted=payload.model_dump()
            )

        # Call the extracted API function and get the response
        response = await call_query_api(payload, euler_token, token_response)

        logger.info(f"QAPI Tool Return: Response object type={type(response)}")
        
        # Reverse aliases in the response to convert back to LLM-known names
        response_with_reversed_aliases = reverse_aliases_in_response(response, transformation_map)

        # Return response directly to callback
        if isinstance(response_with_reversed_aliases, QApiSuccessResponse):
            await params.result_callback({"data": response_with_reversed_aliases.model_dump_json()})
        else:
            await params.result_callback({"error": response_with_reversed_aliases.error})
            
    except Exception as e:
        logger.error(f"Error in q_api tool: {str(e)}", exc_info=True)
        error_payload = params.arguments if hasattr(params, 'arguments') else {}
        await params.result_callback({"error": f"Failed to execute query: {str(e)}"})


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

create_euler_offer_function = FunctionSchema(
    name="create_euler_offer",
    description="Creates discount offers, cashbacks, and other promotional offers in the platform. IMPORTANT: Before calling this function, you MUST first present all the offer details to the user in a clear, formatted way and explicitly ask for their confirmation. Only proceed with calling this function after the user has explicitly confirmed they want to create the offer. Do not call this function without explicit user confirmation. To set the offer's active period, always use the get_current_time() tool for accurate start and end times in IST",
    properties={
        "offerCode": {
            "type": "string",
            "description": "Unique identifier for the offer. Examples: SAVE20, WELCOME10, NEWYEAR2025"
        },
        "offerType": {
            "type": "string",
            "description": "Type of promotional offer. ONLY these types are supported: CASHBACK (gives money back to customer), DISCOUNT (reduces order amount). No other offer types can be created.",
            "enum": ["CASHBACK", "DISCOUNT"]
        },
        "offerTitle": {
            "type": "string",
            "description": "Customer-facing title for the offer. Examples: Get 20% Off on All Items, Welcome Cashback for New Users"
        },
        "discountValue": {
            "type": "number",
            "description": "Discount amount in rupees for absolute discounts, or percentage value for percentage-based discounts"
        },
        "startDate": {
            "type": "string",
            "description": "REQUIRED: Ask the user for the offer start date and time. Must be provided in IST format YYYY-MM-DD HH:MM:SS. Do not use example dates - always get the actual desired start date from the user."
        },
        "endDate": {
            "type": "string",
            "description": "REQUIRED: Ask the user for the offer end date and time. Must be provided in IST format YYYY-MM-DD HH:MM:SS. Do not use example dates - always get the actual desired end date from the user."
        },
        "offerDescription": {
            "type": "string",
            "description": "Detailed description of the offer terms and conditions"
        },
        "minOrderAmount": {
            "type": "number",
            "description": "Minimum order value required to apply this offer in rupees"
        },
        "maxDiscountAmount": {
            "type": "number",
            "description": "Maximum discount amount that can be applied in rupees"
        },
        "calculationType": {
            "type": "string",
            "description": "How the discount is calculated",
            "enum": ["PERCENTAGE", "ABSOLUTE"]
        },
        "isCouponBased": {
            "type": "boolean",
            "description": "Whether customers need to enter a coupon code to apply this offer"
        },
        "sponsoredBy": {
            "type": "string",
            "description": "Entity sponsoring this offer",
            "enum": ["BREEZE"]
        },
        "paymentInstruments": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["CARD", "NB", "WALLET", "CONSUMER_FINANCE", "REWARD", "CASH", "UPI"]
            },
            "description": "Payment methods eligible for this offer. If not specified, applies to all payment methods"
        }
    },
    required=OFFER_REQUIRED_KEYS
)

field_value_discovery_function = FunctionSchema(
    name="field_value_discovery",
    description="""Tool to discover candidate values for *only cardinality dimensions* via fuzzy matching.
    Note: Use this tool **only** when you need to look up possible field values to build filters without hard-coding lists.

    Supported Dimensions:
        Only call this tool if the query references one of these fields:
        - actual_order_status
        - actual_payment_status
        - allowed_requeue
        - auth_type
        - bank
        - card_brand
        - card_type
        - emi
        - emi_tenure
        - emi_type
        - entire_payment_flow
        - gateway
        - industry
        - is_business_retry
        - is_cvv_less_txn
        - is_offer_txn
        - is_requeued_order
        - is_retargeted_order
        - is_retried_order
        - is_technical_retry
        - is_token_bin
        - is_tokenized
        - issuer_token_reference
        - mandate_feature
        - order_source_object
        - order_source_object_id
        - order_status
        - order_type
        - os
        - payment_gateway
        - payment_instrument_group
        - payment_method_subtype
        - payment_method_type
        - payment_status
        - platform
        - prev_order_status
        - prev_txn_status
        - previous_order_status
        - previous_txn_status
        - status_sync_source
        - stored_card_vault_provider
        - ticket_size
        - token_repeat
        - tokenization_consent
        - tokenization_consent_ui_presented
        - tokenization_eligibility
        - tokenized_flow
        - txn_conflict
        - txn_flow_type
        - txn_latency_enum
        - txn_object_type
        - txn_source_object
        - txn_type
        - unified_response_category
        - user_opt_in
        - using_stored_card
        - using_token
        - is_upicc

    Notes:
        - Limit Enforcement:
          -> If default_limit > 50, return FieldLookupBatchResponse(error="default_limit cannot be greater than 50.")
        - Guards Loading:
          -> Load JSON from Langfuse prompt "high_cardinality_field_guards". On parse failure, raise RuntimeError("high_cardinality_field_guards prompt is not valid JSON").
        - Candidate Preparation:
          -> For each request, look up guards[dimension], filter out non-string or blank entries.
          -> Determine cap = request.max_results if given, else default_limit.
        - No Candidates:
          -> If the guard list is empty, return an empty list for each query (or a single empty list if no queries).
        - Fuzzy Matching:
          -> If queries provided: for each q, compute similarity = SequenceMatcher(None, q.lower(), c.lower()).ratio();
             -> Sort candidates by descending similarity, take top cap.
        - Default Ordering:
          -> If no queries: return the first cap candidates in their original order.
        - **Important Note on Absence of Results:**
          -> If a searched value is not found in the list of candidate values, it does *not* always mean that the value is unsupported for the given field. It could also indicate that there is currently no data recorded with that value for this particular field. Be sure to convey this to the user when communicating results—absence of a value in this lookup is not definitive proof that the value is invalid.

        - Response Structure:
          -> Returns FieldLookupBatchResponse with:
             • results: List[DimensionLookupResult], each containing:
               – dimension: string  
               – results: List[List[str]]  (outer list = per query, inner list = matched values)
             • error: optional string if limit validation fails""",
    properties={
        "requests": {
            "type": "array",
            "description": "List of dimension lookup requests",
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": "The dimension to look up values for (e.g., 'payment_gateway', 'payment_method_type', 'card_brand')"
                    },
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of fuzzy search queries. If empty, returns first N values."
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Optional override for number of results per query"
                    }
                },
                "required": ["dimension", "queries"]
            }
        },
        "default_limit": {
            "type": "integer",
            "description": "Default number of results to return per query (max 50)",
            "default": 10
        }
    },
    required=["requests"]
)

q_api_function = FunctionSchema(
    name="q_api",
    description="""Calls an internal /q analytics API with the provided analytics payload. 
    REMEMBER! try to apply all required the filters, dimensions, etc. in least amount of function tool calls.
    CAN do more calls if all the filters, dimensions, etc. in the query's context are not possible in a single call.

    IMPORTANT: Before using q_api with filters, ALWAYS call field_value_discovery first!
    - If your query uses ANY filter on supported dimensions (like bank, payment_gateway, card_brand, platform, order_status, payment_status, actual_order_status, actual_payment_status, etc.), you MUST call field_value_discovery tool first to validate the filter values.
    - Only skip field_value_discovery for static identifiers like merchant_id, error_message, card_bin.
    - This ensures your filter values are correct and prevents validation errors.

    USEFUL SYNONYMS:
      Revenue = Processed amount = GMV = total_amount (successful orders only)
      Netbanking, NB
      BNPL, Pay Later
      EMI, Instalments
      THREE_DS , 3DS
      THREE_DS_2, 3DS
      UPI QR, QR , Scan n Pay
      UPI COLLECT , COLLECT
      Wallets, Prepaid Instrument , PPI
      Network, Card Brand, Brand, Card network
      UPI INTENT , INTENT , PAY, Pay using App, UPI_PAY (If user is asking about UPI intent / intent transactions, always set payment_method_subtype to UPI_PAY)
      Payment Gateway, Gateway , Aggregator, PG
      Auto Pay, Mandate, subscriptions, recurring payment
      Payment Instrument, Payment Instrument Group, Payment Containers
      Success rate, Conversion rate , SR, S.R , Payment SR , Order SR , Order Success Rate
    
    Args:
        filters: A dict representing the 'filters' section with valid field values from the schema.
                REQUIRED FORMAT: Filters must be in FlatFilter format with 'clauses' array and 'logic' string:
                {
                  "clauses": [
                    {"field": "payment_gateway", "condition": "In", "val": ["RAZORPAY"]},
                    {"field": "card_brand", "condition": "In", "val": ["VISA"]}
                  ],
                  "logic": "0 AND 1"
                }
                
                For single filter: {"clauses": [{"field": "payment_gateway", "condition": "In", "val": ["RAZORPAY"]}], "logic": "0"}
                
                IMPORTANT NOTES:
                  - Using "limit" in Filters:
                    -> If query asks to *list* or *give* or *show* the possible values/enums of a "dimension", then **always** apply a limit = 10 for limiting the number of rows.
                    -> Add `limit` in the filter when the query requests to limit the number of rows by otuput, i.e., "top 'n'..." in the user query means apply limit as 'n'. However if user is just asking for top, then assume limit as 1 (VERY IMPORTANT) 
                    -> For example, the query: _"Give me a breakdown of the transaction volume by payment method for the top error message"_ or _"Give me the top error message yesterday"_ requires filtering for the top error message while providing the breakdown by payment method. In such cases:
                    -> if anywhere in the query "top 3", "top 2", "top", etc are present, ALWAYS APPLY `limit` in "filter". 
                    -> Use `limit` to restrict the filter to the top error message, e.g.,:
                  ```json
                     # Example filter for top error message 
                        {
                            "filters": {
                            "and": {
                                "left": {
                                "condition": "In",
                                "field": "error_message",
                                "val": {
                                    "sortedOn": {
                                    "sortDimension": "order_with_transactions",
                                    "ordering": "Desc"
                                    },
                                    "limit": Int
                                }
                                },
                                "right": {
                                "condition": "NotIn",
                                "field": "error_message",          
                                "val": [null]
                                }
                            }
                            }
                        }                     
                  ```
                 - ALWAYS add a filter to exclude null values when querying for top values of any dimension/field. This ensures that null values don't appear in the top results. For example, when asked for "top payment gateways", always include a filter like `"condition": "NotIn", "field": "payment_gateway", "val": [null]` in combination with the limit filter. _MAKE SURE TO ALWAYS FILTER OUT NULL VALUES NOT EMPTY STRING ""_
                 - Consider Conversational Context: Carefully examine if the current user query is a continuation or refinement of a previous query within the ongoing conversation. If the current query lacks specific filter details but appears to build upon earlier messages, actively infer the necessary filters from the established conversational context. For example, if the user first asks "What is the SR for Razorpay?" and then follows up with "Break it down by card type", the second query implicitly requires the `payment_gateway` filter for "RAZORPAY" from the first query.
                 - Use payment_instrument_group, payment_method_subtype, payment_method_type to find out type of payment instrument used For eg. credit card, debit card, upi, etc..
                 - You are not allowed to use any field apart from the provided possible enum values in the JSON schema.
                 - Do not return an empty filter object.
                 - After generating the filter, check each key and match it with the allowed JSON schema. Do not return filters outside of the JSON schema.
                 - Return only the JSON filter in the output; do not return any other text apart from the generated filter.
                 - If the query asks details about a specific merchant, add the filter for merchant_id. (Note: merchant_id should be lowercase and without spaces)
                 - If the query specifies EMI transactions, always set filter for emi_bank to be not null!
                 - To filter transactions for a specific card type (Credit Card/Debit Card) filter on "payment_instrument_group"!
                 - NOTE: For handling queries regarding payments through UPI apps, set payment_method_subtype filter on UPI_PAY. UPI App name is stored in the "bank" field/dimension. 
                 - When asked about payments through UPI handle/VPA/UPI ID/UPI Address (eg. @icici, @okicici, @okhdfcbank, @ptyes), set payment_method_subtype filter on UPI_COLLECT. UPI handle is stored in "bank" field. (example - 'paytm handle' in the query refers to "Paytm" in the "bank" fieldDimensionEnum and set payment_method_subtype filter on UPI_COLLECT)
                 - When asked about transactions going through a specific wallet, set payment_method_type filter on WALLET and the wallet name is stored in "bank" field.
                 - NOTE: When asked to filter on order success/failure, always use "payment_status" in dimensions or filter fields. If the user wants more fine grained filtering then use actual_payment_status otherwise always default to "payment_status". Supported values for payment_status: ["SUCCESS", "FAILURE", "PENDING"]
                 - NOTE: When asked for upi credit card transactions, NEVER `payment_instrument_group` = `CREDIT CARD`, always use `is_upicc` = `true`!!!
                 - You should not generate filters for time intervals! Time intervals are handled by interval section of the payload, not filters!
                 - When calculating the success rate, do not apply a filter for payment_status: SUCCESS. The success rate is a metric that should be handled by another component of the system. Avoid adding any filters specifically for success rate calculations. However, you may apply filters for other parts of the query as needed. For example, for a query like "Can you provide the success rate for Visa cards segmented by payment gateway?", the filter should include only "card_brand" in ["VISA"] and not "payment_status" in ["SUCCESS"]. Remember this important instruction!
                 - When asked about payment failure reason, refer to the error_message field.
        metric: A string or list of strings representing metric(s) to be queried. 
               IMPORTANT NOTES:
                 [
                  "total_amount", // total amount (in amount value) of success orders ONLY, ALSO KNOWN AS GMV or Processed Amount (Successful orders only)
                  "success_volume", // total number of success orders (Total number of orders against which a transaction has been attempted by the customer and any one of the transactions was successful)
                  "success_rate", // total number of success orders / total number of orders
                  // Use "total_amount" only for successful transactions. If the query doesn't specify success explicitly, still assume success and choose "total_amount".
                  "avg_ticket_size", // total ticket amount of success orders / total number of success orders
                  "conflict_txn_rate", // total number of conflict orders / total number of orders (Dont use it unless explicitly asked about conflicted orders/transactions, NEVER use it to identify failed transactions/error message filter)
                  "average_latency", // total latency of success orders / total number of success orders
                  "order_with_transactions", // total number of orders against which at least one transaction has been attempted by the customer (includes 'success + pending + failure').
                  "order_with_transactions_gmv" // equivalent to total amount of ALL orders (success+failed+pending+created+others). Use this explicitly ONLY if the user clearly asks for "all orders", "total value of orders", or similar phrases.
                ]

                 - Important distinctions regarding "total amount"
                   -> `total_amount` represents the 'total amount of successful orders only' (also known as 'GMV' or 'Processed Amount').
                   -> `order_with_transactions_gmv` represents 'the total amount across ALL orders' (including 'failed, pending, and created' orders). Do not use this for GMV calculations.
                 - Consider Conversational Context: Carefully examine if the current user query is a continuation or refinement of a previous query within the ongoing conversation. If the current query lacks specific details about metrics, dimensions, or sorting, but appears to build upon earlier messages, actively infer these details from the established conversational context. For example, if the user first asks "What is the SR and volume for Razorpay?" and then follows up with "Show me the daily trend", the second query implicitly requires the `metric` for "SR and volume" from the first query, while adding the time dimension for the daily trend.
        dimensions: A list of dimension strings or dimension objects, default empty list
                    IMPORTANT NOTES:
                      - Critical Instruction: When the query asks for 'absolute values' (e.g., "How many?", "Number of?", "Total Number?", "What is?", "Top X?"), do not include `granularity`, `intervalCol`, or `timeZone` in the `dimensions`. Only include these when the query asks for a 'trend over time' (e.g., "Show me the daily trend of...", "How has X changed over time", "Trend of...", "Over time", "Per day", "Per hour"). **If the user explicitly asks for a "graph" or "chart", always treat it as a trend query and include granularity.** **This is the most important instruction.**

                      - Default Time Range: If no time range is specified in the query, use the default time range, which is '12:00 AM of the current day to the current timestamp'. In such cases, use a 'granularity' of `"hour"` instead of `"day"`. This ensures trends are broken down into hourly intervals when no specific time range is given.
                      - Time-Based Columns: Only use `order_created_at` as a value for `intervalCol`.
                      - Using Limit for Top Values: Add `"limit": 1` only when the query explicitly asks for the **single top value** of a dimension or metric, and the expected output from the analytics API is just 'one row'.
                        -> For example, in the query: _"What is the top error message yesterday based on transaction volume?"_, the response should include `"limit": 1` because only the single top error message is requested, also you have to use sortedOn to make sure you're getting the top error message.

                      - Specific Dimensions:
                        -> To view specific card types (Credit Card/Debit Card) for transactions, use `"payment_instrument_group"` as a dimension.
                        -> To get payment volume, use `"order_with_transactions"` instead of `total_volume`.
                        -> UPI apps are stored in the `"bank"` dimension.
        interval: A dict with 'start' and 'end' keys (ISO format: YYYY-MM-DDTHH:MM:SSZ).
                 IMPORTANT NOTES:
                   - If user doesn't explicitly mention the interval, assume interval to be 12am of the same day to current time. INTERVAL IS MANDATORY.
        sortedOn: (Optional) A dict specifying how to sort the results, if needed.
                 IMPORTANT NOTES:
                   - **For any query that will return more than one row** (i.e. whenever `dimensions` is non-empty and you're not explicitly limiting to a single result), you **must** include a top-level `sortedOn` object _outside_ of `filters`:
                       ```json
                       "sortedOn": {
                         "sortDimension": "<primary_metric>",
                         "ordering": "Desc"
                       }
                       ```
                       -> Use the first metric in your `metric` list as the `sortDimension` (or choose the metric most relevant to the user's request, e.g. `success_rate` if present).  
                       -> Always set `"ordering": "Desc"`.  
                   - The value of the "sortDimension" key MUST ONLY BE from the available `metric` values.                
                   - Value for "sortDimension" CANNOT BE "order_created_at".
                   - If you also use a `"limit"` inside `filters` (for a top-N within a breakdown), **still** keep the top-level `sortedOn` outside `filters`.
                  

    Returns:
        A dictionary containing the API response from /q endpoint.
    
    Example:
        Basic success rate query:
        {
            "filters": {
            "field": "payment_gateway",
            "condition": "In",
            "val": ["RAZORPAY"]
            },
            "metric": "success_rate",
            "dimensions": ["payment_method_type"],
            "interval": {
            "start": "2024-03-01T00:00:00Z",
            "end": "2024-03-21T23:59:59Z"
            }
        }""",
    properties={
        "interval": {
            "type": "object",
            "description": "Time range for the query",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "Start time in ISO format (YYYY-MM-DDTHH:MM:SSZ)"
                },
                "end": {
                    "type": "string", 
                    "description": "End time in ISO format (YYYY-MM-DDTHH:MM:SSZ)"
                }
            },
            "required": ["start", "end"]
        },
        "metric": {
            "type": ["string", "array"],
            "description": "Metric(s) to query - can be single metric or array of metrics",
            "items": {"type": "string"}
        },
        "dimensions": {
            "type": "array",
            "description": "Dimensions to group by (optional)",
            "items": {"type": "string"}
        },
        "filters": {
            "type": "object",
            "description": "Filters to apply (optional)",
            "properties": {
                "clauses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "condition": {"type": "string"},
                            "val": {}
                        }
                    }
                },
                "logic": {"type": "string"}
            }
        },
        "sortedOn": {
            "type": "object", 
            "description": "Sorting configuration (optional)"
        },
        "metric_filters": {
            "type": "array",
            "description": "Filters on metric values (optional)",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "condition": {"type": "string"},
                    "value": {"type": "number"}
                }
            }
        }
    },
    required=["interval", "metric"]
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
        create_euler_offer_function,
        field_value_discovery_function,
        q_api_function,
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
    "create_euler_offer": create_euler_offer,
    "field_value_discovery": field_value_discovery,
    "q_api": q_api,
}