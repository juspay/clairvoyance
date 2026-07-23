"""
Plivo implementation of the NumberProvider interface.

Uses PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN from static config. The INR
conversion rate is Redis-backed (dynamic.py) since it drifts with the
market and shouldn't need a pod restart to update.
The plivo SDK is synchronous, so we wrap calls with asyncio.to_thread().
"""

import asyncio
from typing import Any, Dict

import plivo
from plivo.exceptions import ResourceNotFoundError

from app.core.config.dynamic import PLIVO_INR_CONVERSION_RATE
from app.core.config.static import PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN
from app.core.logger import logger
from app.schemas.breeze_buddy.telephony_numbers import (
    AvailableTelephonyNumber,
    ProviderBuyResult,
    TelephonyNumberBuyRequest,
    TelephonyNumberSearchMeta,
    TelephonyNumberSearchParams,
    TelephonyNumberSearchResponse,
)
from app.services.telephony.numbers.provider import NumberProvider


def get_response_value(response: Any, key: str, default: Any = None) -> Any:
    """Read a Plivo SDK response value from dict-like or object-like responses."""
    if isinstance(response, dict):
        return response.get(key, default)
    return getattr(response, key, default)


class PlivoNumberProvider(NumberProvider):
    """Plivo integration for number search and buy operations."""

    def __init__(self):
        if not PLIVO_AUTH_ID or not PLIVO_AUTH_TOKEN:
            logger.error("Plivo credentials not configured")
            raise ValueError(
                "PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN must be set in environment"
            )
        self.client = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)

    async def search_numbers(
        self, params: TelephonyNumberSearchParams
    ) -> TelephonyNumberSearchResponse:
        """Search available phone numbers from Plivo inventory."""
        kwargs: Dict[str, Any] = {
            "country_iso": params.country_iso,
            "limit": min(params.limit, 20),
            "offset": params.offset,
        }

        if params.type:
            kwargs["type"] = params.type
        if params.pattern:
            kwargs["pattern"] = params.pattern
        if params.services:
            kwargs["services"] = params.services
        if params.region:
            kwargs["region"] = params.region

        logger.info(f"Searching Plivo numbers with params: {kwargs}")
        inr_conversion_rate = await PLIVO_INR_CONVERSION_RATE()

        # plivo SDK is synchronous - run in thread pool
        try:
            response = await asyncio.to_thread(self.client.numbers.search, **kwargs)
        except ResourceNotFoundError:
            logger.info(f"Plivo search returned no numbers for params: {kwargs}")
            return TelephonyNumberSearchResponse(
                numbers=[],
                meta=TelephonyNumberSearchMeta(
                    limit=params.limit,
                    offset=params.offset,
                    total_count=0,
                    inr_conversion_rate=inr_conversion_rate,
                ),
            )

        numbers = []
        meta = TelephonyNumberSearchMeta(
            limit=params.limit,
            offset=params.offset,
            total_count=0,
            inr_conversion_rate=inr_conversion_rate,
        )

        response_objects = get_response_value(response, "objects")
        if response_objects is not None:
            for num in response_objects:
                numbers.append(
                    AvailableTelephonyNumber(
                        number=get_response_value(num, "number"),
                        prefix=get_response_value(num, "prefix"),
                        city=get_response_value(num, "city"),
                        country=get_response_value(num, "country"),
                        region=get_response_value(num, "region"),
                        type=get_response_value(num, "type"),
                        sub_type=get_response_value(num, "sub_type"),
                        setup_rate=get_response_value(num, "setup_rate"),
                        monthly_rental_rate=get_response_value(
                            num, "monthly_rental_rate"
                        ),
                        sms_enabled=get_response_value(num, "sms_enabled", False),
                        voice_enabled=get_response_value(num, "voice_enabled", False),
                        voice_rate=get_response_value(num, "voice_rate"),
                        sms_rate=get_response_value(num, "sms_rate"),
                        restriction=get_response_value(num, "restriction"),
                        restriction_text=get_response_value(num, "restriction_text"),
                        resource_uri=get_response_value(num, "resource_uri"),
                    )
                )

            response_meta = get_response_value(response, "meta")
            if response_meta is not None:
                meta.limit = get_response_value(response_meta, "limit", params.limit)
                meta.offset = get_response_value(response_meta, "offset", params.offset)
                meta.total_count = get_response_value(
                    response_meta, "total_count", len(numbers)
                )
            else:
                meta.total_count = len(numbers)
        else:
            logger.warning(f"Unexpected Plivo search response type: {type(response)}")

        logger.info(f"Plivo search returned {len(numbers)} numbers")
        return TelephonyNumberSearchResponse(numbers=numbers, meta=meta)

    async def buy_number(self, request: TelephonyNumberBuyRequest) -> ProviderBuyResult:
        """Purchase a phone number from Plivo."""
        logger.info(f"Purchasing Plivo number: {request.number}")

        # plivo SDK is synchronous - run in thread pool
        response = await asyncio.to_thread(
            self.client.numbers.buy, number=request.number
        )

        result = ProviderBuyResult(
            status=get_response_value(response, "status", "unknown"),
            api_id=get_response_value(response, "api_id", "") or None,
            message=get_response_value(response, "message", "") or "",
            numbers=[
                {
                    "number": get_response_value(num, "number", request.number),
                    "status": get_response_value(num, "status", "unknown"),
                }
                for num in (get_response_value(response, "numbers", []) or [])
            ],
        )

        if not result.is_fulfilled:
            raw_response = (
                str(vars(response)) if hasattr(response, "__dict__") else str(response)
            )
            logger.warning(f"Unexpected Plivo buy status. Raw response: {raw_response}")

        logger.info(f"Plivo buy response: {result}")
        return result

    async def unrent_number(self, number: str) -> bool:
        """Unrent (release) a phone number from Plivo account."""
        try:
            logger.info(f"Unrenting Plivo number: {number}")
            await asyncio.to_thread(self.client.numbers.delete, number=number)
            logger.info(f"Successfully unrented Plivo number: {number}")
            return True
        except Exception as e:
            logger.error(f"Failed to unrent Plivo number {number}: {e}", exc_info=True)
            return False
