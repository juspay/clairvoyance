import httpx
import json
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, validator

from app.core.logger import logger

class BreezeAnalyticsError(Exception):
    """Custom exception for Breeze Analytics API errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text

    def __str__(self):
        return f"BreezeAnalyticsError: {super().__str__()} (Status: {self.status_code}, Response: {self.response_text[:200] if self.response_text else 'N/A'})"


async def get_breeze_analytics(
    breeze_token: str,
    start_time_iso: str, # e.g., "2023-01-01T00:00:00.000Z"
    end_time_iso: str,   # e.g., "2023-01-01T01:00:00.000Z"
    shop_id: str,
    shop_url: str,
    shop_type: str = "SHOPIFY" 
) -> Optional[Dict[str, Any]]: # Return raw dictionary for now
    """
    Fetches analytics data from the Breeze API for a given shop and time range.
    Returns the raw 'data' field from the JSON response.
    """
    if not all([breeze_token, start_time_iso, end_time_iso, shop_id, shop_url, shop_type]):
        logger.error("get_breeze_analytics called with one or more missing required parameters.")
        raise ValueError("Missing required parameters for Breeze analytics.")

    api_url = "https://portal.breeze.in/analytics"
    
    request_payload = {
        "shopIds": [shop_id], # API expects an array
        "startTime": start_time_iso,
        "shops": [shop_url], # API expects an array
        "endTime": end_time_iso,
        "operationalTab": "OVERVIEW",
        "granularityFilter": None, # JSONObject.NULL in Kotlin maps to None in Python for json.dumps
        "shopType": shop_type
    }

    headers = {
        "accept": "*/*",
        "x-auth-token": breeze_token,
        "Content-Type": "application/json",
        "user-agent": "ClairvoyanceApp/1.0" # Good practice
    }

    logger.info(f"Fetching Breeze analytics. ShopID: {shop_id}, Period: {start_time_iso} to {end_time_iso}")
    logger.debug(f"Request URL: {api_url}")
    logger.debug(f"Request Headers: x-auth-token: {breeze_token[:10]}...")
    logger.debug(f"Request Payload: {json.dumps(request_payload)}")

    async with httpx.AsyncClient(timeout=60.0) as client: # Increased timeout
        try:
            response = await client.post(api_url, json=request_payload, headers=headers)
            
            logger.info(f"Breeze Analytics API response status: {response.status_code}")

            if response.status_code == 200:
                response_body_text = response.text
                if not response_body_text:
                    logger.error("Empty response body from Breeze Analytics API.")
                    return None
                
                logger.info(f"Breeze Analytics full response: {response_body_text}") # Changed to INFO level
                
                try:
                    json_response = json.loads(response_body_text)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode JSON response from Breeze Analytics: {e}", exc_info=True)
                    logger.error(f"Problematic JSON: {response_body_text[:500]}")
                    return None

                api_status = json_response.get("status")
                if api_status != "success":
                    logger.error(f"Breeze Analytics API returned non-success status: {api_status}. Message: {json_response.get('message')}")
                    return None

                data_field = json_response.get("data")
                if data_field is None or not isinstance(data_field, dict): # Expecting a dict for the data
                    logger.error(f"No 'data' field or 'data' is not a dictionary in Breeze Analytics response. Data: {data_field}")
                    return None
                
                return data_field 

            else:
                error_body = response.text
                logger.error(f"Breeze Analytics API request failed: {response.status_code} {response.reason_phrase}")
                logger.error(f"Error Response Body: {error_body[:500]}")
                return None

        except httpx.RequestError as e:
            logger.error(f"Network error during Breeze Analytics request: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error during Breeze Analytics request: {e}", exc_info=True)
            return None