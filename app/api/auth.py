import asyncio
import json
from enum import Enum
from typing import Optional, Union, Any

import httpx # Changed from requests for new async function
import requests # Keep for existing fetch_breeze_token
from pydantic import BaseModel, Field

from app.core.logger import logger

class BreezeAuthRequest(BaseModel):
    token: str
    issuer: str = "JUSPAY"
    loginType: str = "email"

class BreezeAuthData(BaseModel):
    token: str

class BreezeAuthResponse(BaseModel):
    status: str
    message: Optional[str] = None
    data: Optional[BreezeAuthData] = None

class FetchTokenStatus(Enum):
    SUCCESS = "success"
    INVALID_TOKEN = "invalid_token"
    NETWORK_ERROR = "network_error"
    OTHER_ERROR = "other_error"

class SuccessResult(BaseModel):
    status: FetchTokenStatus = Field(default=FetchTokenStatus.SUCCESS)
    token: str

class ErrorResult(BaseModel):
    status: FetchTokenStatus

FetchTokenResult = Union[SuccessResult, ErrorResult]


# --- Models for Euler Auth Validation ---
class EulerAuthValidateRequest(BaseModel):
    token: str

class EulerAuthValidateResponse(BaseModel):
    merchantId: Optional[str] = None
    # Add other fields if needed, Pydantic will ignore unknown keys by default
    # For now, we only care about merchantId and the overall success.
    # The example response is very large, so we'll only model what's strictly needed.
    # We'll assume a successful HTTP status implies the token was valid if merchantId is present.

class ValidateEulerAuthStatus(Enum):
    SUCCESS = "success"
    INVALID_TOKEN_OR_ERROR = "invalid_token_or_error" # General error for simplicity
    NETWORK_ERROR = "network_error"
    OTHER_ERROR = "other_error"

class EulerAuthSuccess(BaseModel):
    status: ValidateEulerAuthStatus = Field(default=ValidateEulerAuthStatus.SUCCESS)
    merchant_id: str

class EulerAuthError(BaseModel):
    status: ValidateEulerAuthStatus
    message: Optional[str] = None

ValidateEulerAuthResult = Union[EulerAuthSuccess, EulerAuthError]


# It's good practice to have a shared session for HTTP requests
# if you plan to make multiple calls to the same host or need connection pooling.
# For a single function, a direct call is also fine.
# For fetch_breeze_token
http_client_sync = requests.Session()
# For validate_euler_auth, httpx.AsyncClient will be used within the function

async def fetch_breeze_token(platform_token: str) -> FetchTokenResult:
    """
    Fetches the Breeze authentication token.
    @param platform_token: The token obtained from the initial login.
    @return: A FetchTokenResult indicating success, invalid token, or other errors.
    """
    if not platform_token:
        logger.error("fetch_breeze_token called with empty platform_token.")
        return ErrorResult(status=FetchTokenStatus.OTHER_ERROR)

    try:
        request_body_content = BreezeAuthRequest(token=platform_token).model_dump_json()
        headers = {
            "Content-Type": "application/json; charset=utf-8"
        }
        url = "https://portal.breeze.in/auth"

        logger.info(f"Breeze Auth Request Body: {request_body_content}")
        logger.info(f"Making request to Breeze auth endpoint: {url}")

        response = await asyncio.to_thread( # Keep asyncio.to_thread for the sync requests library
            http_client_sync.post,
            url,
            data=request_body_content,
            headers=headers,
            timeout=30 # Adding a timeout
        )

        response_body_string = response.text # Read body once

        if not response.ok: # response.ok checks for status codes 200-299
            logger.error(f"Breeze auth request failed: {response.status_code} {response.reason}")
            logger.error(f"Error Response body: {response_body_string}")
            if response.status_code == 400 and response_body_string:
                try:
                    # Pydantic will ignore unknown keys by default if not defined in the model
                    error_response = BreezeAuthResponse.model_validate_json(response_body_string)
                    if error_response.message and "invalid token" in error_response.message.lower():
                        logger.warning("Breeze API returned Invalid Token error.")
                        return ErrorResult(status=FetchTokenStatus.INVALID_TOKEN)
                except Exception as e:
                    logger.error(f"Error parsing error response body: {e}", exc_info=True)
            return ErrorResult(status=FetchTokenStatus.OTHER_ERROR)

        if not response_body_string:
            logger.error("Breeze auth success response body is null.")
            return ErrorResult(status=FetchTokenStatus.OTHER_ERROR)

        logger.info(f"Breeze auth success response received: {response_body_string[:200]}...")
        try:
            auth_response = BreezeAuthResponse.model_validate_json(response_body_string)
            if auth_response.status and auth_response.status.lower() == "success" and auth_response.data and auth_response.data.token:
                logger.info("Breeze token successfully parsed.")
                return SuccessResult(token=auth_response.data.token)
            else:
                logger.error(
                    f"Breeze auth success response status not success or token missing: Status={auth_response.status}, Message={auth_response.message}"
                )
                return ErrorResult(status=FetchTokenStatus.OTHER_ERROR)
        except Exception as e:
            logger.error(f"Error parsing Breeze auth success response JSON: {e}", exc_info=True)
            return ErrorResult(status=FetchTokenStatus.OTHER_ERROR)

    except requests.exceptions.RequestException as e: # Catches network-related errors
        logger.error(f"RequestException (e.g., Network error) during Breeze auth request: {e}", exc_info=True)
        return ErrorResult(status=FetchTokenStatus.NETWORK_ERROR)
    except Exception as e:
        logger.error(f"Unexpected error during Breeze auth request: {e}", exc_info=True)
        return ErrorResult(status=FetchTokenStatus.OTHER_ERROR)


async def validate_euler_auth(token: str) -> ValidateEulerAuthResult:
    """
    Validates a Juspay Euler token and returns the merchantId if successful.

    Args:
        token: The token to validate.

    Returns:
        ValidateEulerAuthResult indicating success (with merchant_id) or an error.
    """
    if not token:
        logger.error("validate_euler_auth called with empty token.")
        return EulerAuthError(status=ValidateEulerAuthStatus.OTHER_ERROR, message="Token cannot be empty.")

    api_url = "https://portal.juspay.in/api/ec/v1/validate/token"
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "user-agent": "ClairvoyanceApp/1.0" # Good practice
    }
    payload = EulerAuthValidateRequest(token=token).model_dump()

    logger.info(f"Validating Euler auth token. URL: {api_url}")
    logger.debug(f"Payload: {payload}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                api_url,
                json=payload,
                headers=headers
            )

            logger.info(f"Euler auth validation API response status: {response.status_code}")
            
            if response.status_code >= 200 and response.status_code < 300:
                try:
                    response_data = response.json()
                    logger.debug(f"Euler auth validation API response data: {str(response_data)[:500]}...")
                    
                    # Validate with Pydantic model
                    parsed_response = EulerAuthValidateResponse.model_validate(response_data)
                    
                    if parsed_response.merchantId:
                        logger.info(f"Euler token validated successfully. Merchant ID: {parsed_response.merchantId}")
                        return EulerAuthSuccess(merchant_id=parsed_response.merchantId)
                    else:
                        logger.error("Euler token validation successful response, but merchantId is missing.")
                        return EulerAuthError(status=ValidateEulerAuthStatus.INVALID_TOKEN_OR_ERROR, message="Validation successful but merchantId missing in response.")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode JSON response from Euler auth validation API: {e}. Response text: {response.text}", exc_info=True)
                    return EulerAuthError(status=ValidateEulerAuthStatus.OTHER_ERROR, message=f"Invalid JSON response: {e}")
                except Exception as e: # Catches Pydantic validation errors too
                    logger.error(f"Error processing successful Euler auth validation response: {e}", exc_info=True)
                    return EulerAuthError(status=ValidateEulerAuthStatus.OTHER_ERROR, message=f"Error processing response: {e}")
            else:
                error_message = f"Euler auth validation API request failed with status {response.status_code}"
                try:
                    # Try to get more info from response if possible
                    error_details = response.json()
                    logger.error(f"{error_message}. Response: {error_details}")
                    error_message = f"{error_message} - {error_details.get('message', response.text)}"
                except json.JSONDecodeError:
                    logger.error(f"{error_message}. Response: {response.text}")
                return EulerAuthError(status=ValidateEulerAuthStatus.INVALID_TOKEN_OR_ERROR, message=error_message)

        except httpx.RequestError as e:
            logger.error(f"HTTP RequestError during Euler auth validation API call: {e}", exc_info=True)
            return EulerAuthError(status=ValidateEulerAuthStatus.NETWORK_ERROR, message=f"Network error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during Euler auth validation API call: {e}", exc_info=True)
            return EulerAuthError(status=ValidateEulerAuthStatus.OTHER_ERROR, message=f"An unexpected error occurred: {e}")

# Example of how you might call it (for testing purposes, not part of the library code)
# if __name__ == "__main__":
#     import asyncio
#
#     async def main():
#         # Replace with a real or test token
#         test_token = "your_platform_token_here"
#         if test_token == "your_platform_token_here":
#             print("Please replace 'your_platform_token_here' with a valid token to test.")
#             return
#
#         result = await fetch_breeze_token(test_token)
#         print(f"Fetch Result: {result}")
#
#         # Example with an empty token
#         # result_empty = await fetch_breeze_token("")
#         # print(f"Fetch Result (empty token): {result_empty}")
#
#     asyncio.run(main())