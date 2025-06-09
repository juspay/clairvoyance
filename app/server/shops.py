import httpx
import logging
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

# Configure logging
logger = logging.getLogger(__name__)
# Assuming logging is configured elsewhere or can be set here if needed
# logging.basicConfig(level=logging.INFO)

# --- Pydantic Models ---

class SocialHandle(BaseModel):
    handle: str
    id: str
    shopId: str # shop_id in Python typically, but matching Kotlin for now
    iconUrl: str
    profileUrl: str

class ShopConfig(BaseModel):
    syncIngestionFlow: bool = False
    enableShopifyCartFlow: bool = False
    enableBundles: bool = False
    createBundlesFromCart: bool = False
    loginOtpSafeMode: bool = False
    enableBundlesForce: bool = False
    enableRTOBasedCODFiltering: bool = False
    enableShopifyAbandonmentV2: bool = False
    enableOrderReconViaPT: bool = False
    enableUserIngestionV2: bool = False
    enableRemovingPartialPaymentDiscount: bool = False
    enableBundlesViaNewFlow: bool = False

class ShopMeta(BaseModel):
    useGql: bool = False
    extensionId: str = ""
    id: str = ""
    apiKey: str = ""
    scope: str = ""
    gqlAccessToken: str = ""
    apiSecret: str = ""

class Shop(BaseModel):
    id: str
    url: str
    name: str
    type: str
    merchantId: str # merchant_id in Python typically
    trackingUrl: str = ""
    enableTwoStepCheckout: bool = False
    enableShipping: bool = True
    enablePartialPayments: bool = False
    skipStatusPage: bool = False
    autoRefund: bool = True
    enableUserDataIngestion: bool = True
    enableInventoryCheck: bool = True
    socialHandles: List[SocialHandle] = Field(default_factory=list)
    config: Optional[ShopConfig] = None
    meta: Optional[ShopMeta] = None
    logo: str = ""

class ShopResponse(BaseModel):
    shops: List[Shop] = Field(default_factory=list)

class ShopServiceError(Exception):
    """Custom exception for ShopService errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text

    def __str__(self):
        return f"ShopServiceError: {super().__str__()} (Status: {self.status_code}, Response: {self.response_text[:200] if self.response_text else 'N/A'})"

# Hardcoded token from the Kotlin example
_AUTH_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJSRUFEIiwicm91dGVzIjpbIkFERFJFU1MiLCJDVVNUT01FUiIsIk1FUkNIQU5UIiwiU0hPUCIsIk9SREVSIiwiU0hJUFBJTkdfUFJPVklERVIiLCJTSElQUElOR19aT05FIiwiU0hJUFBJTkdfUlVMRSIsIkNBUlQiLCJQQVJUSUFMX1BBWU1FTlRfUlVMRSIsIk5PVElGSUNBVElPTl9MT0dTIiwiTE9DQVRJT04iLCJDVVNUT01FUiIsIlBBWU1FTlRTX01FVEhPRFNfR1JPVVBTIiwiUEFZTUVOVFNfTUVUSE9EUyIsIkRFTElWRVJZX0VTVElNQVRFIiwiUFJPRFVDVF9HUk9VUCIsIk1BTkRBVEVTIiwiU1VSQ0hBUkdFIiwiQ0FNUEFJR04iLCJUQVNLX0lOU1RBTkNFUyJdLCJleHAiOjMxNzA1NTIxODYxMiwiaXNzIjoid3d3LmJyZWV6ZS5pbiIsImlhdCI6MTcyNjc1NDYxMiwiaWQiOiJGeW5uUjBtZWRreVBZbTZPNmpYeUQifQ.eu1IAaMsBD4WewQtEhtVHxg3VgvAEjPI761S5bHyG6U"

async def fetch_shop_data(merchant_id: str) -> Optional[ShopResponse]:
    """
    Fetches shop data from the Breeze API for a given merchant ID.

    Args:
        merchant_id: The merchant ID to fetch shops for.

    Returns:
        ShopResponse containing the list of shops, or None if the request fails
        or parsing is unsuccessful.
    """
    if not merchant_id:
        logger.error("fetch_shop_data called with empty merchant_id.")
        # Or raise ValueError("Merchant ID cannot be empty.")
        return None

    api_url = f"https://api.breeze.in/shop?merchantId={merchant_id}"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {_AUTH_TOKEN}",
        "user-agent": "ClairvoyanceApp/1.0"
    }

    logger.info(f"Fetching shop data for merchant: {merchant_id}")
    logger.debug(f"Request URL: {api_url}")
    logger.debug(f"Request Headers: Authorization: Bearer {_AUTH_TOKEN[:20]}...") # Log snippet of token

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(api_url, headers=headers)

            logger.info(f"Shop data API response status: {response.status_code}")

            if response.status_code == 200:
                response_body = response.text
                if not response_body:
                    logger.error("Empty response body from shop data API.")
                    return None
                
                logger.debug(f"Shop data response received (first 500 chars): {response_body[:500]}")
                try:
                    # Pydantic will ignore unknown keys by default if not defined in the model
                    # and handle isLenient-like behavior by trying to coerce types.
                    shop_response_data = ShopResponse.model_validate_json(response_body)
                    logger.info(f"Successfully parsed shop data. Found {len(shop_response_data.shops)} shops for merchant {merchant_id}.")
                    return shop_response_data
                except Exception as e: # Catches Pydantic ValidationError and json.JSONDecodeError
                    logger.error(f"Error parsing shop data JSON response for merchant {merchant_id}: {e}", exc_info=True)
                    logger.error(f"Problematic JSON (first 500 chars): {response_body[:500]}")
                    return None # As per Kotlin example, return null on parsing error
            else:
                error_body = response.text
                logger.error(f"Shop data API request failed: {response.status_code} {response.reason_phrase}")
                logger.error(f"Error Response Headers: {response.headers}")
                logger.error(f"Error Response Body: {error_body}")
                return None # As per Kotlin example, return null on API error

        except httpx.RequestError as e:
            logger.error(f"Network error during shop data request for merchant {merchant_id}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error during shop data request for merchant {merchant_id}: {e}", exc_info=True)
            return None

# Example usage (for testing, can be removed or kept under if __name__ == "__main__":)
# async def main():
#     test_merchant_id = "your_merchant_id_here" # Replace with a valid merchant ID
#     if test_merchant_id == "your_merchant_id_here":
#         print("Please replace 'your_merchant_id_here' with a valid merchant ID to test.")
#         return

#     print(f"Fetching shops for merchant: {test_merchant_id}")
#     shop_data = await fetch_shop_data(test_merchant_id)

#     if shop_data:
#         print(f"Found {len(shop_data.shops)} shops.")
#         for shop in shop_data.shops:
#             print(f"  Shop ID: {shop.id}, Name: {shop.name}, URL: {shop.url}")
#             if shop.config:
#                 print(f"    Enable Bundles: {shop.config.enableBundles}")
#             if shop.meta:
#                 print(f"    Meta API Key: {shop.meta.apiKey}")
#     else:
#         print(f"Failed to fetch shop data for merchant {test_merchant_id}.")

# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(main())