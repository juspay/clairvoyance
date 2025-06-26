import asyncio # For asyncio.gather
import httpx
import json
from typing import Optional, Dict, Any, Union, List
from datetime import datetime as dt
from pydantic import BaseModel, Field # For new Pydantic models

from app.core.config import GENIUS_API_URL
from app.core.logger import logger

# It's good practice to have a shared async client if making multiple calls
# For now, we'll create one per call or you can manage it globally in your app

class JuspayAPIError(Exception):
    """Custom exception for Juspay API errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data

    def __str__(self):
        return f"{self.status_code}: {super().__str__()} - {self.response_data}"


def _get_formatted_time_range_iso(
    start_time_iso: str,
    end_time_iso: str
) -> Dict[str, str]:
    """
    Validates and returns ISO formatted start and end times.
    """
    if not start_time_iso:
        raise ValueError("start_time_iso cannot be empty.")
    if not end_time_iso:
        raise ValueError("end_time_iso cannot be empty.")

    try:
        dt.fromisoformat(start_time_iso.replace("Z", "+00:00"))
    except ValueError as e:
        logger.error(f"Invalid start_time_iso format: {start_time_iso} - {e}")
        raise ValueError(f"Invalid start_time_iso format: {start_time_iso}")

    try:
        dt.fromisoformat(end_time_iso.replace("Z", "+00:00"))
    except ValueError as e:
        logger.error(f"Invalid end_time_iso format: {end_time_iso} - {e}")
        raise ValueError(f"Invalid end_time_iso format: {end_time_iso}")

    return {"start": start_time_iso, "end": end_time_iso}


async def _make_genius_api_request_internal(
    login_token: str,
    metric_name: str, # For logging/debugging purposes
    payload_details: Dict[str, Any],
    start_time_iso: str,
    end_time_iso: str
) -> Union[Dict[str, Any], List[Dict[str, Any]]]: # Updated return type for success
    """
    Internal helper to make requests to the GENIUS_API_URL.
    Handles time formatting and common request logic.
    """
    if not login_token:
        logger.error(f"_make_genius_api_request_internal called for metric '{metric_name}' with empty login_token.")
        raise ValueError("Login token cannot be empty.")

    time_range = _get_formatted_time_range_iso(start_time_iso, end_time_iso)

    full_payload = {
        **payload_details, # Includes metric, dimensions, domain, filters etc.
        "interval": {"start": time_range["start"], "end": time_range["end"]},
    }
    # "message" was in tool payloads, can be added if needed, e.g. full_payload["message"] = f"Fetching {metric_name}"

    headers = {
        'Content-Type': 'application/json',
        'x-web-logintoken': login_token,
        "user-agent": "ClairvoyanceApp/1.0"
    }

    logger.info(f"Requesting Juspay Genius API. URL: {GENIUS_API_URL}, Metric: {payload_details.get('metric', metric_name)}, Payload: {json.dumps(full_payload)}")
    logger.debug(f"Headers: {headers}")


    async with httpx.AsyncClient(timeout=60.0) as client: # Increased timeout slightly
        try:
            response = await client.post(
                GENIUS_API_URL,
                json=full_payload,
                headers=headers
            )

            response_text = await response.aread() # Use aread() for bytes then decode, or .text for direct string
            response_text_str = response_text.decode('utf-8') if isinstance(response_text, bytes) else response_text


            logger.info(f"Genius API response status for {metric_name}: {response.status_code}")
            logger.debug(f"Genius API response data for {metric_name} (first 500 chars): {response_text_str[:500]}")

            if response.status_code >= 200 and response.status_code < 300:
                try:
                    # Attempt to parse as a single JSON object first
                    return json.loads(response_text_str)
                except json.JSONDecodeError as je1:
                    # If single parse fails, try parsing as newline-separated JSON objects
                    logger.warning(f"Failed to parse as single JSON for {metric_name}, attempting newline-separated parsing. Error: {je1}")
                    parsed_objects = []
                    lines = response_text_str.strip().split('\n')
                    if not lines: # Handle empty response after strip
                        logger.error(f"Empty response after stripping for {metric_name}, cannot parse line by line.")
                        raise JuspayAPIError(f"Empty JSON response from Genius API for {metric_name}", status_code=response.status_code, response_data={"raw_response": response_text_str})

                    for line in lines:
                        line = line.strip()
                        if line: # Ensure line is not empty
                            try:
                                parsed_objects.append(json.loads(line))
                            except json.JSONDecodeError as je2:
                                logger.error(f"Failed to decode line: '{line}' for {metric_name}. Error: {je2}", exc_info=True)
                                # Decide if one bad line should fail all, or collect good ones.
                                # For now, let's be strict: if any line fails, the whole response is considered invalid.
                                raise JuspayAPIError(f"Invalid line in multi-object JSON response for {metric_name}: '{line}'. Error: {je2}", status_code=response.status_code, response_data={"raw_response": response_text_str})
                    
                    if not parsed_objects: # If all lines were empty or failed silently (though we raise above)
                         logger.error(f"No valid JSON objects found after line-by-line parsing for {metric_name}.")
                         raise JuspayAPIError(f"No valid JSON objects in response for {metric_name}", status_code=response.status_code, response_data={"raw_response": response_text_str})
                    
                    # If only one object was parsed from lines and original didn't have newlines (or only one line), return as dict
                    # This handles cases where a single JSON object might have trailing newlines.
                    if len(parsed_objects) == 1 and len(lines) == 1:
                         return parsed_objects[0]
                    return parsed_objects # Return list of dicts

            else:
                error_message = f"Genius API request for {metric_name} failed with status {response.status_code}"
                logger.error(f"{error_message}. Response: {response_text_str}")
                # Try to parse error response if it's JSON
                try:
                    error_data = json.loads(response_text_str)
                except json.JSONDecodeError:
                    error_data = {"raw_error": response_text_str}
                raise JuspayAPIError(error_message, status_code=response.status_code, response_data=error_data)

        except httpx.RequestError as e:
            logger.error(f"HTTP RequestError during Genius API call for {metric_name}: {e}", exc_info=True)
            raise JuspayAPIError(f"Network error during Genius API call for {metric_name}: {e}", status_code=None)
        except Exception as e: # Catch-all for other unexpected errors
            logger.error(f"Unexpected error during Genius API call for {metric_name}: {e}", exc_info=True)
            raise JuspayAPIError(f"An unexpected error occurred for {metric_name}: {e}", status_code=None)


async def get_success_rate(
    login_token: str,
    start_time_iso: str,
    end_time_iso: str
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Calculates the overall success rate (SR) for transactions over a specified time interval
    using the Genius API.
    """
    payload_details = {
        "dimensions": [],
        "domain": "kvorders",
        "metric": "success_rate"
        # "message": "Fetching SR." # from tool payload, can be added if needed
    }
    return await _make_genius_api_request_internal(
        login_token=login_token,
        metric_name="overall_success_rate",
        payload_details=payload_details,
        start_time_iso=start_time_iso,
        end_time_iso=end_time_iso
    )

async def get_payment_method_wise_sr(
    login_token: str,
    start_time_iso: str,
    end_time_iso: str
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Fetches a breakdown of the success rate (SR) by payment method over a specified time interval
    using the Genius API.
    """
    payload_details = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "metric": "success_rate"
        # "message": "Fetching PM wise SR."
    }
    return await _make_genius_api_request_internal(
        login_token=login_token,
        metric_name="payment_method_wise_sr",
        payload_details=payload_details,
        start_time_iso=start_time_iso,
        end_time_iso=end_time_iso
    )

async def get_failure_transactional_data(
    login_token: str,
    start_time_iso: str,
    end_time_iso: str
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Retrieves transactional data for failed transactions, highlighting top failure reasons
    and associated payment methods using the Genius API.
    """
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
        # "message": "Fetching failure data."
    }
    return await _make_genius_api_request_internal(
        login_token=login_token,
        metric_name="failure_transactional_data",
        payload_details=payload_details,
        start_time_iso=start_time_iso,
        end_time_iso=end_time_iso
    )

async def get_success_transactional_data(
    login_token: str,
    start_time_iso: str,
    end_time_iso: str
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Retrieves the count of successful transactions for each payment method
    over a specified time interval using the Genius API.
    """
    payload_details = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "filters": {"condition": "In", "field": "payment_status", "val": ["SUCCESS"]},
        "metric": "success_volume"
        # "message": "Fetching success data."
    }
    return await _make_genius_api_request_internal(
        login_token=login_token,
        metric_name="success_transactional_data",
        payload_details=payload_details,
        start_time_iso=start_time_iso,
        end_time_iso=end_time_iso
    )

async def get_gmv_order_value_payment_method_wise(
    login_token: str,
    start_time_iso: str,
    end_time_iso: str
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Retrieves the Gross Merchandise Value (GMV) for each payment method
    over a specified time interval using the Genius API.
    """
    payload_details = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "metric": "total_amount"
        # "message": "Fetching GMV."
    }
    return await _make_genius_api_request_internal(
        login_token=login_token,
        metric_name="gmv_order_value_payment_method_wise",
        payload_details=payload_details,
        start_time_iso=start_time_iso,
        end_time_iso=end_time_iso
    )

async def get_average_ticket_payment_wise(
    login_token: str,
    start_time_iso: str,
    end_time_iso: str
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Calculates the average ticket size for each payment method
    over a specified time interval using the Genius API.
    """
    payload_details = {
        "dimensions": ["payment_method_type"],
        "domain": "kvorders",
        "metric": "avg_ticket_size"
        # "message": "Fetching avg ticket size."
    }
    return await _make_genius_api_request_internal(
        login_token=login_token,
        metric_name="average_ticket_payment_wise",
        payload_details=payload_details,
        start_time_iso=start_time_iso,
        end_time_iso=end_time_iso
    )


# --- Cumulative Analytics Models ---

class OverallSuccessRateData(BaseModel):
    success_rate: Optional[float] = None

class PaymentMethodDetail(BaseModel):
    payment_method_type: Optional[str] = None
    success_rate: Optional[float] = None
    transaction_count: Optional[int] = None
    gmv: Optional[float] = None
    average_ticket_size: Optional[float] = None

class FailureDetail(BaseModel):
    error_message: Optional[str] = None
    payment_method_type: Optional[str] = None
    count: Optional[int] = None

class CumulativeJuspayAnalytics(BaseModel):
    overall_success_rate_data: Optional[OverallSuccessRateData] = None
    payment_method_success_rates: List[PaymentMethodDetail] = Field(default_factory=list)
    failure_details: List[FailureDetail] = Field(default_factory=list)
    success_volume_by_payment_method: List[PaymentMethodDetail] = Field(default_factory=list)
    gmv_by_payment_method: List[PaymentMethodDetail] = Field(default_factory=list)
    average_ticket_size_by_payment_method: List[PaymentMethodDetail] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


async def get_cumulative_juspay_analytics(
    login_token: str,
    start_time_iso: str,
    end_time_iso: str
) -> CumulativeJuspayAnalytics:
    """
    Fetches all Juspay metrics concurrently and aggregates them into a single object.
    """
    results = CumulativeJuspayAnalytics()

    tasks = [
        get_success_rate(login_token, start_time_iso, end_time_iso),
        get_payment_method_wise_sr(login_token, start_time_iso, end_time_iso),
        get_failure_transactional_data(login_token, start_time_iso, end_time_iso),
        get_success_transactional_data(login_token, start_time_iso, end_time_iso),
        get_gmv_order_value_payment_method_wise(login_token, start_time_iso, end_time_iso),
        get_average_ticket_payment_wise(login_token, start_time_iso, end_time_iso)
    ]

    # Execute all tasks concurrently and get results, including exceptions
    task_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process overall success rate
    sr_result = task_results[0]
    if isinstance(sr_result, Exception):
        results.errors.append(f"Error fetching overall success rate: {str(sr_result)}")
    elif isinstance(sr_result, dict) and 'success_rate' in sr_result:
        results.overall_success_rate_data = OverallSuccessRateData(success_rate=sr_result.get('success_rate'))
    else:
        results.errors.append(f"Unexpected data format for overall success rate: {type(sr_result)}")

    # Process payment method wise SR
    pm_sr_result = task_results[1]
    if isinstance(pm_sr_result, Exception):
        results.errors.append(f"Error fetching payment method wise SR: {str(pm_sr_result)}")
    elif isinstance(pm_sr_result, list):
        for item in pm_sr_result:
            if isinstance(item, dict):
                results.payment_method_success_rates.append(PaymentMethodDetail(
                    payment_method_type=item.get('payment_method_type'),
                    success_rate=item.get('success_rate')
                ))
    else:
        results.errors.append(f"Unexpected data format for payment method wise SR: {type(pm_sr_result)}")

    # Process failure transactional data
    fail_data_result = task_results[2]
    if isinstance(fail_data_result, Exception):
        results.errors.append(f"Error fetching failure transactional data: {str(fail_data_result)}")
    elif isinstance(fail_data_result, list):
        for item in fail_data_result:
            if isinstance(item, dict):
                results.failure_details.append(FailureDetail(
                    error_message=item.get('error_message'),
                    payment_method_type=item.get('payment_method_type'),
                    count=item.get('order_with_transactions')
                ))
    else:
         results.errors.append(f"Unexpected data format for failure transactional data: {type(fail_data_result)}")

    # Process success transactional data
    succ_data_result = task_results[3]
    if isinstance(succ_data_result, Exception):
        results.errors.append(f"Error fetching success transactional data: {str(succ_data_result)}")
    elif isinstance(succ_data_result, list):
        for item in succ_data_result:
            if isinstance(item, dict):
                results.success_volume_by_payment_method.append(PaymentMethodDetail(
                    payment_method_type=item.get('payment_method_type'),
                    transaction_count=item.get('success_volume')
                ))
    else:
        results.errors.append(f"Unexpected data format for success transactional data: {type(succ_data_result)}")
        
    # Process GMV order value payment method wise
    gmv_data_result = task_results[4]
    if isinstance(gmv_data_result, Exception):
        results.errors.append(f"Error fetching GMV by payment method: {str(gmv_data_result)}")
    elif isinstance(gmv_data_result, list):
        for item in gmv_data_result:
            if isinstance(item, dict):
                results.gmv_by_payment_method.append(PaymentMethodDetail(
                    payment_method_type=item.get('payment_method_type'),
                    gmv=item.get('total_amount')
                ))
    else:
        results.errors.append(f"Unexpected data format for GMV by payment method: {type(gmv_data_result)}")

    # Process average ticket payment wise
    avg_ticket_result = task_results[5]
    if isinstance(avg_ticket_result, Exception):
        results.errors.append(f"Error fetching average ticket size by payment method: {str(avg_ticket_result)}")
    elif isinstance(avg_ticket_result, list):
        for item in avg_ticket_result:
            if isinstance(item, dict):
                results.average_ticket_size_by_payment_method.append(PaymentMethodDetail(
                    payment_method_type=item.get('payment_method_type'),
                    average_ticket_size=item.get('avg_ticket_size')
                ))
    else:
        results.errors.append(f"Unexpected data format for average ticket size: {type(avg_ticket_result)}")

    return results