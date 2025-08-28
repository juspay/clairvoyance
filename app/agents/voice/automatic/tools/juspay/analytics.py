import httpx
import json
import functools

from datetime import datetime
import pytz
from app.core.logger import logger
from app.core.config import GENIUS_API_URL, EULER_DASHBOARD_API_URL
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from app.agents.voice.automatic.types.models import ApiFailure, ApiSuccess, GeniusApiResponse

# This token will be set when the tools are initialized
euler_token: str | None = None
merchant_id: str | None = None

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


async def list_offers_by_filter(params: FunctionCallParams):
    """
    Lists promotional offers. Fetches offers based on user-provided criteria.
    """
    try:
        logger.info(f"Fetching offers with filters: {params.arguments}")

        if not merchant_id or not euler_token:
            await params.result_callback({"error": "Authentication token or Merchant ID is missing."})
            return

        ist = pytz.timezone("Asia/Kolkata")
        utc = pytz.utc
        
        # Determine the date range for the API call in UTC.
        now_utc = datetime.now(utc)
        start_of_month_utc = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        user_created_at = params.arguments.get("created_at", {})
        start_time_str = user_created_at.get("gte")
        end_time_str = user_created_at.get("lte")
        
        try:
            # If user provides time in IST, convert it to ISO format with timezone.
            if start_time_str:
                start_time_ist = ist.localize(datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S'))
            else:
                start_time_ist = start_of_month_utc.astimezone(ist)

            if end_time_str:
                end_time_ist = ist.localize(datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S'))
            else:
                end_time_ist = now_utc.astimezone(ist)
        except Exception as e:
            logger.error(f"Error converting user-provided time: {e}")
            await params.result_callback({"error": f"Invalid time format. Please use 'YYYY-MM-DD HH:MM:SS' in IST. Error: {e}"})
            return

        # Prepare the payload for the API call.
        filters = {}
        for key in ["offerId", "offerCode", "paymentMethodType", "isCouponBased", "currency"]:
            if key in params.arguments:
                filters[key] = params.arguments[key]

        payload = {
            "status": params.arguments.get("status", ['ACTIVE']),
            "limit": params.arguments.get("limit", 50),
            "created_at": {
                "gte": start_time_ist.isoformat(),
                "lte": end_time_ist.isoformat()
            },
            "merchant_id": merchant_id,
            "filters": filters,
            "benefitType": params.arguments.get("benefitType"),
            "start_time": "2021-12-31 18:30:00Z",
            "end_time": "2050-12-31 18:29:59Z",
            "sort_offers": {
                "order": "DESCENDING",
                "field": "CREATED_AT"
            }
        }

        endpoint = f"{EULER_DASHBOARD_API_URL}/api/offers/dashboard/dashboard-list"
        headers = {'Content-Type': 'application/json', 'x-web-logintoken': euler_token}

        logger.info(f"Requesting Euler offers list from: {endpoint} | Payload: {json.dumps(payload, indent=2)}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(endpoint, json=payload, headers=headers)

            if response.status_code != 200:
                error_text = response.text
                logger.error(f"Failed to list offers: {response.status_code} - {error_text}")
                await params.result_callback({"error": f"Failed to list offers: HTTP {response.status_code}", "details": error_text})
                return

            await params.result_callback({"data": response.text})

    except httpx.TimeoutException:
        logger.error("List offers request timed out.")
        await params.result_callback({"error": "Request timed out. Please try again."})
    except Exception as e:
        logger.error(f"Critical error in list_offers_by_filter: {e}", exc_info=True)
        await params.result_callback({"error": f"An unexpected error occurred: {str(e)}"})


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


async def find_offer_by_code(offer_code: str) -> dict | None:
    """
    Helper function to find an offer by its offer code and return complete offer details.
    Returns the complete offer data if found, None otherwise.
    """
    if not euler_token or not merchant_id:
        logger.error("Missing authentication token or merchant ID for offer search")
        return None

    try:
        # Search for the offer using the offer code
        search_payload = {
            "merchant_id": merchant_id,
            "offer_code": [offer_code],
            "limit": 1,
            "start_time": "2020-01-01T00:00:00Z",
            "end_time": "2050-01-01T00:00:00Z",
            "created_at": {
                "gte": "2020-01-01T00:00:00Z",
                "lte": "2050-01-01T00:00:00Z"
            },
            "sort_offers": {
                "order": "DESCENDING",
                "field": "CREATED_AT"
            }
        }

        endpoint = f"{EULER_DASHBOARD_API_URL}/api/offers/dashboard/dashboard-list?merchant_id={merchant_id}"
        headers = {
            'Content-Type': 'application/json',
            'x-web-logintoken': euler_token
        }

        logger.info(f"Searching for offer with code '{offer_code}' using endpoint: {endpoint}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(endpoint, json=search_payload, headers=headers)
            
            if response.status_code == 200:
                response_data = response.json()
                
                if response_data.get("list") and len(response_data["list"]) > 0:
                    offer_data = response_data["list"][0]
                    offer_id = offer_data.get("offer_id")
                    logger.info(f"Found offer ID '{offer_id}' for offer code '{offer_code}' with complete details")
                    return offer_data
                else:
                    logger.warning(f"No offer found with code '{offer_code}'")
                    return None
            else:
                logger.error(f"Failed to search for offer: {response.status_code} - {response.text}")
                return None

    except Exception as e:
        logger.error(f"Error searching for offer by code '{offer_code}': {e}")
        return None

async def delete_euler_offer(params: FunctionCallParams):
    """
    Deletes a promotional offer from the Euler platform based on its offer code.
    This permanently removes the offer and cannot be undone.
    """
    try:
        offer_code = params.arguments.get("offerCode")
        
        if not offer_code:
            await params.result_callback({"error": "Missing required field: offerCode"})
            return

        # Authentication check
        if not euler_token:
            await params.result_callback({"error": "Authentication token is missing. Cannot delete offer."})
            return

        if not merchant_id:
            await params.result_callback({"error": "Merchant ID not available in session context. Cannot delete offer."})
            return

        logger.info(f"Attempting to delete Euler offer with code '{offer_code}' for merchant '{merchant_id}'")

        # Step 1: Find the offer by code
        offer_data = await find_offer_by_code(offer_code)
        
        if not offer_data:
            await params.result_callback({
                "error": f"Offer with code '{offer_code}' not found. Please verify the offer code and try again."
            })
            return

        # Extract offer_id from the returned offer data
        offer_id = offer_data.get("offer_id")
        if not offer_id:
            await params.result_callback({
                "error": f"Invalid offer data returned for '{offer_code}'. Missing offer_id."
            })
            return

        # Step 2: Delete the offer using the found ID
        delete_endpoint = f"{EULER_DASHBOARD_API_URL}/api/offers/dashboard/{offer_id}/delete"
        delete_payload = {
            "merchant_id": merchant_id,
            "offer_id": offer_id
        }
        
        headers = {
            'Content-Type': 'application/json',
            'x-web-logintoken': euler_token
        }

        logger.info(f"Deleting offer with ID '{offer_id}' using endpoint: {delete_endpoint}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(delete_endpoint, json=delete_payload, headers=headers)
            
            if response.status_code == 200:
                success_result = {
                    "status": "success",
                    "message": f"Successfully deleted offer '{offer_code}'",
                    "details": {
                        "offerCode": offer_code,
                        "offerId": offer_id,
                        "action": "deleted"
                    }
                }
                logger.info(f"Successfully deleted offer '{offer_code}' (ID: {offer_id})")
                await params.result_callback({"data": json.dumps(success_result)})
            else:
                error_text = response.text
                logger.error(f"Failed to delete offer '{offer_code}': {response.status_code} - {error_text}")
                await params.result_callback({
                    "error": f"Failed to delete offer '{offer_code}': HTTP {response.status_code}. {error_text}"
                })

    except httpx.TimeoutException:
        logger.error(f"Delete offer request timed out for offer '{offer_code}'")
        await params.result_callback({"error": "Request timed out. Please try again."})
    except Exception as e:
        logger.error(f"Critical error in delete_euler_offer: {e}", exc_info=True)
        await params.result_callback({"error": f"An unexpected error occurred: {str(e)}"})


async def update_euler_offer(params: FunctionCallParams):
    """
    Updates offer details or status in the Euler platform based on provided parameters.
    This function handles status changes (PAUSED/EXPIRED/ACTIVE) and offer modifications.
    When discount value is updated, the offer title and description are automatically updated to match.
    """
    try:
        offer_code = params.arguments.get("offerCode")
        
        if not offer_code:
            await params.result_callback({"error": "Missing required field: offerCode"})
            return

        # Define immutable fields that cannot be updated
        IMMUTABLE_FIELDS = ["startDate", "endDate", "offerDescription", "sponsoredBy", "paymentInstruments"]
        
        # Check for restricted fields in the request
        restricted_fields_found = []
        for field in IMMUTABLE_FIELDS:
            if params.arguments.get(field) is not None:
                restricted_fields_found.append(field)
        
        if restricted_fields_found:
            await params.result_callback({
                "error": f"Cannot update immutable fields: {', '.join(restricted_fields_found)}. These fields cannot be modified after offer creation. If you need to change these values, please create a new offer instead."
            })
            return

        # Authentication check
        if not euler_token:
            await params.result_callback({"error": "Authentication token is missing. Cannot update offer."})
            return

        if not merchant_id:
            await params.result_callback({"error": "Merchant ID not available in session context. Cannot update offer."})
            return

        logger.info(f"Attempting to update Euler offer details for '{offer_code}' for merchant '{merchant_id}'")

        # Step 1: Find the offer by code and get complete offer data
        offer_data = await find_offer_by_code(offer_code)
        
        if not offer_data:
            await params.result_callback({
                "error": f"Offer with code '{offer_code}' not found. Please verify the offer code and try again."
            })
            return

        # Extract offer_id from the returned offer data
        offer_id = offer_data.get("offer_id")
        if not offer_id:
            await params.result_callback({
                "error": f"Invalid offer data returned for '{offer_code}'. Missing offer_id."
            })
            return

        # Step 2: Use the offer data from find_offer_by_code as the base (avoiding the failing detailed API)
        existing_offer = offer_data
        logger.info(f"Using offer data from search for '{offer_code}' (avoiding detailed API call)")

        # Step 3: Extract user-provided update parameters
        offer_title = params.arguments.get("offerTitle")
        offer_description = params.arguments.get("offerDescription")
        discount_value = params.arguments.get("discountValue")
        calculation_type = params.arguments.get("calculationType")
        min_order_amount = params.arguments.get("minOrderAmount")
        max_discount_amount = params.arguments.get("maxDiscountAmount")
        start_date = params.arguments.get("startDate")
        end_date = params.arguments.get("endDate")
        status = params.arguments.get("status")
        sponsored_by = params.arguments.get("sponsoredBy")
        payment_instruments = params.arguments.get("paymentInstruments")
        offer_type = params.arguments.get("offerType")

        # Auto-update title and description when discount value changes
        if discount_value is not None:
            # Get existing calculation type if not provided
            existing_calc_type = calculation_type if calculation_type is not None else existing_offer.get("rule_dsl", {}).get("benefits", [{}])[0].get("calculation_rule", "ABSOLUTE")
            
            # Auto-generate title and description based on discount value and type
            if existing_calc_type == "PERCENTAGE":
                if offer_title is None:
                    offer_title = f"Get {discount_value}% Off"
                if offer_description is None:
                    offer_description = f"Enjoy {discount_value}% discount on your order"
            else:  # ABSOLUTE
                if offer_title is None:
                    offer_title = f"Get ₹{discount_value} Off"
                if offer_description is None:
                    offer_description = f"Save ₹{discount_value} on your order"
            
            logger.info(f"Auto-updated title to '{offer_title}' and description to '{offer_description}' for discount value {discount_value}")

        # Step 3.5: Intelligent routing - detect if this is status-only update or comprehensive update
        detail_params = [offer_title, offer_description, discount_value, calculation_type, 
                        min_order_amount, max_discount_amount, start_date, end_date, 
                        sponsored_by, payment_instruments, offer_type]
        
        # If only status is provided and no other details, use status-only endpoint
        if status is not None and all(param is None for param in detail_params):
            logger.info(f"Detected status-only update for offer '{offer_code}' to '{status}'")
            
            # Validate status
            valid_statuses = ["PAUSED", "EXPIRED", "ACTIVE"]
            if status not in valid_statuses:
                await params.result_callback({
                    "error": f"Invalid status '{status}'. Valid statuses are: {', '.join(valid_statuses)}"
                })
                return

            # Use status-only endpoint
            status_endpoint = f"{EULER_DASHBOARD_API_URL}/api/offers/dashboard/{offer_id}/status/update"
            status_payload = {"status": status}
            
            headers = {
                'Content-Type': 'application/json',
                'x-web-logintoken': euler_token
            }

            logger.info(f"Using status-only endpoint: {status_endpoint}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(status_endpoint, json=status_payload, headers=headers)
                
                if response.status_code == 200:
                    action_map = {"PAUSED": "paused", "EXPIRED": "expired", "ACTIVE": "activated"}
                    action = action_map.get(status, "updated")
                    
                    success_result = {
                        "status": "success",
                        "message": f"Successfully {action} offer '{offer_code}'",
                        "details": {
                            "offerCode": offer_code,
                            "offerId": offer_id,
                            "action": action,
                            "newStatus": status
                        }
                    }
                    logger.info(f"Successfully {action} offer '{offer_code}' (ID: {offer_id})")
                    await params.result_callback({"data": json.dumps(success_result)})
                    return
                else:
                    error_text = response.text
                    logger.error(f"Failed to update offer '{offer_code}' status: {response.status_code} - {error_text}")
                    await params.result_callback({
                        "error": f"Failed to update offer '{offer_code}' status: HTTP {response.status_code}. {error_text}"
                    })
                    return

        # If we reach here, it's a comprehensive details update
        logger.info(f"Detected comprehensive details update for offer '{offer_code}'")

        # Step 4: Convert dates if provided
        start_date_iso = None
        end_date_iso = None
        if start_date:
            try:
                ist = pytz.timezone("Asia/Kolkata")
                start_date_ist = ist.localize(datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S'))
                start_date_iso = start_date_ist.isoformat()
            except Exception as e:
                await params.result_callback({"error": f"Invalid start date format. Use 'YYYY-MM-DD HH:MM:SS' in IST. Error: {e}"})
                return

        if end_date:
            try:
                ist = pytz.timezone("Asia/Kolkata")
                end_date_ist = ist.localize(datetime.strptime(end_date, '%Y-%m-%d %H:%M:%S'))
                end_date_iso = end_date_ist.isoformat()
            except Exception as e:
                await params.result_callback({"error": f"Invalid end date format. Use 'YYYY-MM-DD HH:MM:SS' in IST. Error: {e}"})
                return

        # Step 5: Handle payment instruments if provided
        payment_instruments_payload = None
        if payment_instruments:
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
            
            payment_instruments_payload = [
                instrument_map[instrument] for instrument in payment_instruments
                if instrument in instrument_map
            ]

        # Step 6: Build update payload using existing data as base, only updating provided fields
        api_payload = {
            "application_mode": existing_offer.get("application_mode", "ORDER"),
            "offer_id": offer_id,
            "merchant_id": merchant_id,
            "offer_code": offer_code,
            "batch_id": existing_offer.get("batch_id", ""),
            "offer_description": {
                "title": offer_title if offer_title is not None else existing_offer.get("offer_description", {}).get("title", ""),
                "description": offer_description if offer_description is not None else existing_offer.get("offer_description", {}).get("description", ""),
                "tnc": existing_offer.get("offer_description", {}).get("tnc", ""),
                "sponsored_by": sponsored_by if sponsored_by is not None else existing_offer.get("offer_description", {}).get("sponsored_by", "BREEZE"),
                "display_title": offer_title if offer_title is not None else existing_offer.get("offer_description", {}).get("display_title", "")
            },
            "ui_configs": existing_offer.get("ui_configs", {
                "is_hidden": "false",
                "should_validate": "true",
                "auto_apply": "true",
                "offer_display_priority": 0,
                "payment_method_label": ""
            }),
            "rule_dsl": {
                "order": {
                    "max_quantity": existing_offer.get("rule_dsl", {}).get("order", {}).get("max_quantity"),
                    "min_quantity": existing_offer.get("rule_dsl", {}).get("order", {}).get("min_quantity"),
                    "max_order_amount": existing_offer.get("rule_dsl", {}).get("order", {}).get("max_order_amount"),
                    "min_order_amount": str(min_order_amount) if min_order_amount is not None else existing_offer.get("rule_dsl", {}).get("order", {}).get("min_order_amount", "1"),
                    "currency": existing_offer.get("rule_dsl", {}).get("order", {}).get("currency", "INR"),
                    "amount_info": existing_offer.get("rule_dsl", {}).get("order", {}).get("amount_info", [])
                },
                "additional_payment_filters": existing_offer.get("rule_dsl", {}).get("additional_payment_filters"),
                "payment_instrument": payment_instruments_payload if payment_instruments_payload is not None else existing_offer.get("rule_dsl", {}).get("payment_instrument", []),
                "counters": existing_offer.get("rule_dsl", {}).get("counters", []),
                "txn_type": existing_offer.get("rule_dsl", {}).get("txn_type", ["ORDER"]),
                "payment_channel": existing_offer.get("rule_dsl", {}).get("payment_channel", []),
                "benefits": [
                    {
                        "type": offer_type if offer_type is not None else existing_offer.get("rule_dsl", {}).get("benefits", [{}])[0].get("type", "DISCOUNT"),
                        "calculation_rule": calculation_type if calculation_type is not None else existing_offer.get("rule_dsl", {}).get("benefits", [{}])[0].get("calculation_rule", "ABSOLUTE"),
                        "value": discount_value if discount_value is not None else existing_offer.get("rule_dsl", {}).get("benefits", [{}])[0].get("value", 0),
                        "amount_info": existing_offer.get("rule_dsl", {}).get("benefits", [{}])[0].get("amount_info", []),
                        "max_amount": max_discount_amount if max_discount_amount is not None else existing_offer.get("rule_dsl", {}).get("benefits", [{}])[0].get("max_amount"),
                        "global_max_amount": existing_offer.get("rule_dsl", {}).get("benefits", [{}])[0].get("global_max_amount")
                    }
                ],
                "filters": existing_offer.get("rule_dsl", {}).get("filters", {
                    "blacklist": [],
                    "whitelist": []
                })
            },
            "status": status if status is not None else existing_offer.get("status", "ACTIVE"),
            "start_time": start_date_iso if start_date_iso is not None else existing_offer.get("start_time"),
            "end_time": end_date_iso if end_date_iso is not None else existing_offer.get("end_time"),
            "metadata": {
                "analytics_offer_code": offer_code,
                "customerResetPeriodType": existing_offer.get("metadata", {}).get("customerResetPeriodType", "offerPeriod"),
                "cardResetPeriodType": existing_offer.get("metadata", {}).get("cardResetPeriodType", "offerPeriod"),
                "productCustomerResetPeriodType": existing_offer.get("metadata", {}).get("productCustomerResetPeriodType", "offerPeriod"),
                "productCardResetPeriodType": existing_offer.get("metadata", {}).get("productCardResetPeriodType", "offerPeriod"),
                "upiResetPeriodType": existing_offer.get("metadata", {}).get("upiResetPeriodType", "offerPeriod"),
                "productUpiResetPeriodType": existing_offer.get("metadata", {}).get("productUpiResetPeriodType", "offerPeriod"),
                "start_date": start_date_iso if start_date_iso is not None else existing_offer.get("metadata", {}).get("start_date"),
                "end_date": end_date_iso if end_date_iso is not None else existing_offer.get("metadata", {}).get("end_date")
            },
            "udf1": existing_offer.get("udf1"),
            "udf2": existing_offer.get("udf2"),
            "udf3": existing_offer.get("udf3"),
            "udf4": existing_offer.get("udf4"),
            "udf5": existing_offer.get("udf5"),
            "udf6": existing_offer.get("udf6"),
            "udf7": existing_offer.get("udf7"),
            "udf8": existing_offer.get("udf8"),
            "udf9": existing_offer.get("udf9"),
            "udf10": existing_offer.get("udf10"),
            "minOfferBreakupCheckbox": existing_offer.get("minOfferBreakupCheckbox", False),
            "offerBreakupBool": existing_offer.get("offerBreakupBool", False),
            "benefitsAmountInfo": existing_offer.get("benefitsAmountInfo", []),
            "has_multi_codes": existing_offer.get("has_multi_codes", False),
            "isFilterBlacklist": existing_offer.get("isFilterBlacklist", False)
        }

        # Step 7: Update offer using v2/update endpoint
        update_endpoint = f"{EULER_DASHBOARD_API_URL}/api/offers/dashboard/{offer_id}/v2/update"
        
        headers = {
            'Content-Type': 'application/json',
            'x-web-logintoken': euler_token
        }

        logger.info(f"Updating offer details with ID '{offer_id}' using endpoint: {update_endpoint}")
        logger.info(f"Payload: {json.dumps(api_payload, indent=2)}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(update_endpoint, json=api_payload, headers=headers)
            
            if response.status_code == 200:
                success_result = {
                    "status": "success",
                    "message": f"Successfully updated offer '{offer_code}' details",
                    "details": {
                        "offerCode": offer_code,
                        "offerId": offer_id,
                        "action": "updated",
                        "updatedFields": {
                            k: v for k, v in {
                                "title": offer_title,
                                "description": offer_description,
                                "discountValue": discount_value,
                                "calculationType": calculation_type,
                                "minOrderAmount": min_order_amount,
                                "maxDiscountAmount": max_discount_amount,
                                "startDate": start_date,
                                "endDate": end_date,
                                "status": status,
                                "sponsoredBy": sponsored_by,
                                "paymentInstruments": payment_instruments,
                                "offerType": offer_type
                            }.items() if v is not None
                        }
                    }
                }
                logger.info(f"Successfully updated offer '{offer_code}' (ID: {offer_id})")
                await params.result_callback({"data": json.dumps(success_result)})
            else:
                error_text = response.text
                logger.error(f"Failed to update offer '{offer_code}' details: {response.status_code} - {error_text}")
                await params.result_callback({
                    "error": f"Failed to update offer '{offer_code}' details: HTTP {response.status_code}. {error_text}"
                })

    except httpx.TimeoutException:
        logger.error(f"Update offer details request timed out for offer '{offer_code}'")
        await params.result_callback({"error": "Request timed out. Please try again."})
    except Exception as e:
        logger.error(f"Critical error in update_euler_offer_details: {e}", exc_info=True)
        await params.result_callback({"error": f"An unexpected error occurred: {str(e)}"})




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
    description="Get the overall payment success rate for all transactions within a specified time range. Use this to understand the general health of the payment system. Default to today if no timeframe specified.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

payment_analytics_by_dimension_function = FunctionSchema(
    name="get_payment_analytics_by_dimension",
    description="Retrieves time-bound KPIs—total transaction volume, success rate, and transaction count—broken down by the selected dimension. Useful to analyze performance by gateway, instrument category, or specific instrument type (e.g., Visa, Mastercard). Always aim to extract as many dimensions as possible for a comprehensive snapshot. Default to today if no timeframe specified.",
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
    description="Get a list of the top transaction failure reasons and the payment methods they occurred on within a specified time range. Use this to diagnose the most common payment issues. Default to today if no timeframe specified.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

success_transactional_data_function = FunctionSchema(
    name="get_success_transactional_data_by_time",
    description="Get the total count of successful transactions for each payment method within a specified time range. Use this to see which payment methods are most popular. Default to today if no timeframe specified.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

gmv_order_value_payment_method_wise_function = FunctionSchema(
    name="get_gmv_order_value_payment_method_wise_by_time",
    description="Get the total Gross Merchandise Value (GMV) for each payment method within a specified time range. The results can be summed to calculate the total payment method GMV/sales. Use this to understand the revenue contribution of each payment method and the overall sales performance. Default to today if no timeframe specified.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

average_ticket_payment_wise_function = FunctionSchema(
    name="get_average_ticket_payment_wise_by_time",
    description="Get the average transaction value (ticket size) for each payment method within a specified time range. Use this to analyze customer spending habits across different payment options. Default to today if no timeframe specified.",
    properties=time_input_schema["properties"],
    required=time_input_schema["required"],
)

merchant_offer_analytics_function = FunctionSchema(
    name="merchant_offer_analytics",
    description="Fetches a list of all active merchant offers and their performance data. Use this to find out what the current offers are, how they are performing, and to diagnose any errors related to offer application. Default to today if no timeframe specified.",
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

list_offers_by_filter_function = FunctionSchema(
    name="list_offers_by_filter",
    description="""Fetches and filters promotional offers. It defaults to fetching active offers from the start of current month to today's date.

**Behavior:**
- **Default:** Returns active offers from the start of current month to today's date.
- **Status Filter:** If the user specifies a status (e.g., "paused", "expired"), it will filter for that status.
- **Date Filter:** If the user provides a start (`gte`) or end (`lte`) date, the tool will process accordingly.""",
    properties={
        "status": {
            "type": "array",
            "items": {"type": "string", "enum": ['ACTIVE', 'EXPIRED', 'PAUSED', 'NEW']},
            "description": "Filter by the calculated real-time status of the offers. Defaults to ['ACTIVE'].",
        },
        "offerId": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Filter by a list of offer IDs.",
        },
        "offerCode": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Filter by a list of offer codes.",
        },
        "paymentMethodType": {
            "type": "array",
            "items": {"type": "string", "enum": ['UPI', 'CARD', 'NB', 'WALLET', 'REWARD', 'CONSUMER_FINANCE', 'CASH']},
            "description": "Filter by a list of payment method types.",
        },
        "benefitType": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Filter by the benefit type of the offers (e.g., 'CASHBACK', 'DISCOUNT').",
        },
        "isCouponBased": {
            "type": "boolean",
            "description": "Filter offers based on whether they are coupon-based.",
        },
        "limit": {
            "type": "number",
            "description": "The maximum number of offers to return. Defaults to 15.",
        },
        "created_at": {
            "type": "object",
            "properties": {
                "gte": {"type": "string", "description": "Optional: The start of the date range in 'YYYY-MM-DD HH:MM:SS' format. Interpreted as IST. If not provided, defaults to start of current month."},
                "lte": {"type": "string", "description": "Optional: The end of the date range in 'YYYY-MM-DD HH:MM:SS' format. Interpreted as IST. If not provided, defaults to current date and time."}
            },
            "description": "Optional: The date range to search for offers. If not provided, defaults to start of current month to current date. When provided, processing will be done accordingly.",
        }
    },
    required=[]
)

delete_euler_offer_function = FunctionSchema(
    name="delete_euler_offer",
    description="Permanently deletes a promotional offer from the Euler platform based on its offer code. This action cannot be undone. Use this when you need to completely remove an offer from the system.",
    properties={
        "offerCode": {
            "type": "string",
            "description": "The unique offer code of the offer to delete. Examples: SAVE20, WELCOME10, NEWYEAR2025"
        }
    },
    required=["offerCode"]
)

update_euler_offer_function = FunctionSchema(
    name="update_euler_offer",
    description="Intelligently updates offer details or status in the Euler platform by automatically detecting intent based on the provided parameters. This function handles status changes (PAUSED/EXPIRED/ACTIVE), discount value updates, and limited modifications. IMPORTANT: The following fields are IMMUTABLE — startDate, endDate (offer validity period), offerDescription (offer description), sponsoredBy (sponsor information), and paymentInstruments (payment methods). If the user wants to change these values, they must create a new offer instead",
    properties={
        "offerCode": {
            "type": "string",
            "description": "The unique offer code of the offer to update. Examples: SAVE20, WELCOME10, NEWYEAR2025"
        },
        "status": {
            "type": "string",
            "description": "New status for the offer. If this is the only parameter provided (besides offerCode), will use fast status-only update.",
            "enum": ["PAUSED", "EXPIRED", "ACTIVE"]
        },
        "offerTitle": {
            "type": "string",
            "description": "Updated customer-facing title for the offer. Examples: Get 20% Off on All Items, Welcome Cashback for New Users"
        },
        "discountValue": {
            "type": "number",
            "description": "Updated discount amount in rupees for absolute discounts, or percentage value for percentage-based discounts"
        },
        "calculationType": {
            "type": "string",
            "description": "Updated calculation method for the discount",
            "enum": ["PERCENTAGE", "ABSOLUTE"]
        },
        "minOrderAmount": {
            "type": "number",
            "description": "Updated minimum order value required to apply this offer in rupees"
        },
        "maxDiscountAmount": {
            "type": "number",
            "description": "Updated maximum discount amount that can be applied in rupees"
        },
        "offerType": {
            "type": "string",
            "description": "Updated type of promotional offer",
            "enum": ["CASHBACK", "DISCOUNT"]
        }
    },
    required=["offerCode"]
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
        list_offers_by_filter_function,
        delete_euler_offer_function,
        update_euler_offer_function,
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
    "list_offers_by_filter": list_offers_by_filter,
    "delete_euler_offer": delete_euler_offer,
    "update_euler_offer": update_euler_offer,
}
