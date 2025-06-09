import httpx
import logging
import json
from typing import Optional, Dict, Any, List, Union
from pydantic import BaseModel, Field, validator

# Configure logging
logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.INFO) # Assuming logging is configured elsewhere

# --- Pydantic Models (Commented out for now) ---
# class FunnelSessions(BaseModel):
#     start: int = 0
#     login: int = 0
#     address: int = 0
#     payment: int = 0
#     ordered: int = 0
#
# class AnalyticsData(BaseModel):
#     totalSales: float = 0.0
#     prepaidSales: float = 0.0
#     prepaidSalesShare: float = 0.0
#     codSales: float = 0.0
#     codSalesShare: float = 0.0
#     conversionRate: float = 0.0
#     sessions: int = 0
#     totalOrders: int = 0
#     prepaidOrders: int = 0
#     prepaidOrdersShare: float = 0.0
#     codOrders: int = 0
#     codOrdersShare: float = 0.0
#     psr: float = 0.0  # Payment Success Rate
#     aov: float = 0.0  # Average Order Value
#     timeTaken: float = 0.0
#     funnelSessions: FunnelSessions = Field(default_factory=FunnelSessions)
#     sourceAttribution: Dict[str, float] = Field(default_factory=dict)
#
#     @validator('*', pre=True, allow_reuse=True)
#     def empty_str_to_default(cls, v, info: Optional[object] = None): # info is ValidationInfo, but Optional[object] for simplicity if not used
#         # For Pydantic V2, 'field.default' is not directly accessible this way in a '*' validator
#         # without more complex introspection via `info.field_name` and then getting model's field default.
#         # A simpler approach for a generic '*' validator is to return a known "empty" value
#         # that the field type can handle (e.g., 0 for int/float, "" for str if not None, or None itself).
#         # However, since our fields have defaults (0, 0.0, default_factory), Pydantic's
#         # own parsing will handle None/missing by applying the default.
#         # This validator is primarily for converting "" to what Pydantic considers "missing" (i.e., None)
#         # so that the field's default can then be applied.
#         if v == "":
#             return None # Pydantic will then use the field's default for None
#         return v # Return v as is if not an empty string (None is handled by Pydantic's default mechanism)
#
#     @validator('funnelSessions', pre=True, allow_reuse=True)
#     def empty_funnel_to_default(cls, v):
#         if v is None or v == {}:
#             return FunnelSessions()
#         return v
#
#     @validator('sourceAttribution', pre=True, allow_reuse=True)
#     def empty_source_to_default(cls, v):
#         if v is None or v == {}:
#             return {}
#         return v


class BreezeAnalyticsError(Exception):
    """Custom exception for Breeze Analytics API errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text

    def __str__(self):
        return f"BreezeAnalyticsError: {super().__str__()} (Status: {self.status_code}, Response: {self.response_text[:200] if self.response_text else 'N/A'})"


# def _parse_analytics_data_from_json(data_json: Dict[str, Any]) -> AnalyticsData:
#     """
#     Parses the 'data' field from the Breeze analytics API response into an AnalyticsData model.
#     Handles potential missing fields by using defaults from the Pydantic model.
#     (Commented out for now - to return raw data)
#     """
#     try:
#         # Helper to safely get nested values
#         def safe_get(data_dict, path, default=None):
#             keys = path.split('.')
#             current = data_dict
#             for key in keys:
#                 if isinstance(current, dict) and key in current:
#                     current = current[key]
#                 else:
#                     # Try with space if key not found (e.g. "Funnel Sessions" vs "FunnelSessions")
#                     key_with_space = key.replace("FunnelSessions", "Funnel Sessions") # Basic attempt
#                     if isinstance(current, dict) and key_with_space in current:
#                         current = current[key_with_space]
#                     else:
#                         logger.debug(f"Path '{path}' not fully found at key '{key}'. Returning default: {default}")
#                         return default
#             return current
#
#         # Helper for type conversion with default
#         def to_float(value, default=0.0):
#             if isinstance(value, (int, float)): return float(value)
#             if isinstance(value, str):
#                 try: return float(value)
#                 except ValueError: return default
#             return default
#
#         def to_int(value, default=0):
#             if isinstance(value, (int, float)): return int(value) # float to int conversion
#             if isinstance(value, str):
#                 try: return int(float(value)) # str to float to int
#                 except ValueError: return default
#             return default
#
#         # Extracting main values
#         total_sales_data = safe_get(data_json, "businessTotalSalesBreakdown.value", {})
#         total_sales = to_float(safe_get(total_sales_data, "value"))
#         prepaid_sales = 0.0
#         cod_sales = 0.0
#         prepaid_sales_share = 0.0
#         if isinstance(total_sales_data.get("bottomContainerItems"), list):
#             for item in total_sales_data["bottomContainerItems"]:
#                 if item.get("metric") == "PREPAID" and item.get("subUnit") == "AMOUNT":
#                     prepaid_sales = to_float(item.get("rate"))
#                 elif item.get("metric") == "COD" and item.get("subUnit") == "AMOUNT":
#                     cod_sales = to_float(item.get("rate"))
#                 elif item.get("metric") == "PREPAID(%)" and item.get("subUnit") == "PERCENTAGE":
#                     prepaid_sales_share = to_float(item.get("rate"))
#
#         total_orders_data = safe_get(data_json, "businessTotalOrdersBreakdown.value", {})
#         total_orders = to_int(safe_get(total_orders_data, "value"))
#         prepaid_orders = 0
#         cod_orders = 0
#         prepaid_orders_share = 0.0
#         if isinstance(total_orders_data.get("bottomContainerItems"), list):
#             for item in total_orders_data["bottomContainerItems"]:
#                 if item.get("metric") == "PREPAID" and item.get("subUnit") == "NUMBER":
#                     prepaid_orders = to_int(item.get("rate"))
#                 elif item.get("metric") == "COD" and item.get("subUnit") == "NUMBER":
#                     cod_orders = to_int(item.get("rate"))
#                 elif item.get("metric") == "PREPAID(%)" and item.get("subUnit") == "PERCENTAGE":
#                     prepaid_orders_share = to_float(item.get("rate"))
#
#         conversion_data = safe_get(data_json, "businessConversionBreakdown.value", {})
#         conversion_rate = to_float(safe_get(conversion_data, "value"))
#         sessions = 0
#         time_taken = 0
#         if isinstance(conversion_data.get("bottomContainerItems"), list):
#             for item in conversion_data["bottomContainerItems"]:
#                 if item.get("metric") == "SESSIONS":
#                     sessions = to_int(item.get("rate"))
#                 elif item.get("metric") == "TIME TAKEN": # Assuming "TIME TAKEN" is the key
#                     time_taken = to_float(item.get("rate")) # Or int if it's seconds
#
#         funnel_raw_data = safe_get(conversion_data, "slotProperties.value", {})
#         funnel_sessions = FunnelSessions(
#             start=to_int(funnel_raw_data.get("clickedCheckoutButton")), # Mapping based on example
#             login=to_int(funnel_raw_data.get("loggedIn")),
#             address=to_int(funnel_raw_data.get("submittedAddress")),
#             payment=to_int(funnel_raw_data.get("clickedProceedToBuyButton")),
#             ordered=to_int(funnel_raw_data.get("placedOrder"))
#         )
#
#         psr = to_float(safe_get(data_json, "paymentSuccessRate.value"))
#         aov = to_float(safe_get(data_json, "averageOrderValue.value"))
#
#         source_attr_raw = safe_get(total_sales_data, "slotProperties.value", {})
#         source_attribution = {k: to_float(v) for k, v in source_attr_raw.items()} if isinstance(source_attr_raw, dict) else {}
#
#
#         return AnalyticsData(
#             totalSales=total_sales,
#             prepaidSales=prepaid_sales,
#             prepaidSalesShare=prepaid_sales_share,
#             codSales=cod_sales,
#             codSalesShare=to_float(cod_sales / total_sales * 100 if total_sales else 0.0), # Calculate if not directly available
#             conversionRate=conversion_rate,
#             sessions=sessions,
#             totalOrders=total_orders,
#             prepaidOrders=prepaid_orders,
#             prepaidOrdersShare=prepaid_orders_share,
#             codOrders=cod_orders,
#             codOrdersShare=to_float(cod_orders / total_orders * 100 if total_orders else 0.0), # Calculate if not directly available
#             psr=psr,
#             aov=aov,
#             timeTaken=time_taken,
#             funnelSessions=funnel_sessions,
#             sourceAttribution=source_attribution
#         )
#     except Exception as e:
#         logger.error(f"Error parsing detailed analytics data structure: {e}", exc_info=True)
#         logger.debug(f"Problematic analytics data JSON: {data_json}")
#         # Return with defaults if parsing specific parts fails, or re-raise
#         # For now, let's try to return a default object to avoid breaking the flow
#         return AnalyticsData()


async def get_breeze_analytics(
    breeze_token: str,
    start_time_iso: str, # e.g., "2023-01-01T00:00:00.000Z"
    end_time_iso: str,   # e.g., "2023-01-01T01:00:00.000Z"
    shop_id: str,
    shop_url: str,
    shop_type: str = "SHOPIFY" # Default as per Kotlin
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
                
                # The actual analytics data might be nested further, e.g., under shop_id or a generic key
                # Based on the Kotlin `parseAnalyticsData(data)` which takes `data.jsonObject`,
                # it seems `data_field` itself is the one to parse.
                # return _parse_analytics_data_from_json(data_field) # Commented out parsing
                return data_field # Return raw data field

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

# Example usage (for testing)
# async def main():
#     # Replace with actual valid values for testing
#     test_breeze_token = "your_breeze_auth_token_here"
#     test_shop_id = "your_shop_id_here"
#     test_shop_url = "your.shop.url" # e.g., "yourstore.myshopify.com"
#     test_shop_type = "SHOPIFY"
#     # Example: Get data for the last hour
#     from datetime import datetime, timedelta, timezone
#     now_utc = datetime.now(timezone.utc)
#     start_time = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
#     end_time = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

#     if "your_" in test_breeze_token or "your_" in test_shop_id:
#         print("Please replace placeholder values in main() for testing get_breeze_analytics.")
#         return

#     print(f"Fetching Breeze analytics for Shop ID {test_shop_id} from {start_time} to {end_time}")
#     analytics_data = await get_breeze_analytics(
#         breeze_token=test_breeze_token,
#         start_time_iso=start_time,
#         end_time_iso=end_time,
#         shop_id=test_shop_id,
#         shop_url=test_shop_url,
#         shop_type=test_shop_type
#     )

#     if analytics_data:
#         print("\nSuccessfully fetched Breeze Analytics Data:")
#         print(f"  Total Sales: {analytics_data.totalSales}")
#         print(f"  Conversion Rate: {analytics_data.conversionRate}")
#         print(f"  Sessions: {analytics_data.sessions}")
#         print(f"  Funnel - Ordered: {analytics_data.funnelSessions.ordered}")
#         # print(analytics_data.model_dump_json(indent=2)) # Full dump
#     else:
#         print("\nFailed to fetch Breeze Analytics Data.")

# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(main())