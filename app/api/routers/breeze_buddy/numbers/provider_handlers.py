"""
Handlers for provider phone number search and purchase operations.

Buy flow:
1. Duplicate pre-check against telephony_numbers (strict -- a database failure
   must never be mistaken for "number is free", or we would pay for a number twice)
2. Purchase from the provider
3. Register in the telephony_numbers table
4. If step 3 fails for any reason, release the number back to the provider

Steps 1-4 run under a per-number RedisLock (see buy_provider_number_handler)
so two concurrent requests for the same number can't both reach the provider.

Client-facing errors are deliberately generic and carry a correlation id; the full
exception is logged server-side under that same id.
"""

from uuid import uuid4

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.telephony_number import (
    check_number_purchase_conflict,
    create_telephony_number,
)
from app.schemas.breeze_buddy.auth import UserInfo
from app.schemas.breeze_buddy.core import CallProvider, TelephonyNumberStatus
from app.schemas.breeze_buddy.telephony_numbers import (
    ProviderBuyResult,
    TelephonyNumberBuyRequest,
    TelephonyNumberBuyResponse,
    TelephonyNumberSearchParams,
    TelephonyNumberSearchResponse,
)
from app.services.redis.locks import LockAcquireError, RedisLock
from app.services.telephony.numbers.factory import get_number_provider
from app.services.telephony.numbers.provider import NumberProvider

from .rbac import resolve_buy_scope

# Covers the pre-check + provider round-trip + DB register worst case. No
# mid-block renewal (see RedisLock docstring), so this must outlast the
# slowest real buy, not just the typical one.
_BUY_LOCK_TTL_SECONDS = 30


async def search_provider_numbers_handler(
    provider_name: CallProvider,
    params: TelephonyNumberSearchParams,
    current_user: UserInfo,
) -> TelephonyNumberSearchResponse:
    """Search available provider numbers.

    Raises:
        HTTPException: 503 if the provider is unsupported/misconfigured,
            502 if the provider API call fails.
    """
    ref = str(uuid4())
    logger.info(
        f"[{ref}] User {current_user.username} searching {provider_name.value} numbers: "
        f"country={params.country_iso}, type={params.type}, pattern={params.pattern}"
    )

    try:
        provider = get_number_provider(provider_name)
    except ValueError as e:
        logger.error(f"[{ref}] {provider_name.value} not available: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{provider_name.value} number search is not available. (ref: {ref})",
        )

    try:
        return await provider.search_numbers(params)
    except Exception as e:
        logger.error(f"[{ref}] {provider_name.value} search failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{provider_name.value} number search failed. (ref: {ref})",
        )


async def _release_number(
    provider: NumberProvider,
    provider_name: CallProvider,
    number: str,
    provider_result: ProviderBuyResult,
    ref: str,
) -> bool:
    """Release a number we bought but could not register. Never raises.

    A False return means the number is rented on the provider account but absent
    from our database, which needs manual cleanup -- hence the critical log.
    """
    logger.warning(f"[{ref}] Releasing {number} from {provider_name.value} (rollback)")

    # The interface documents "must not raise", but this call happens INSIDE
    # error handling for the caller's own failure -- an implementation that
    # doesn't honor the contract must not be allowed to mask that failure or
    # skip the orphan alert below.
    try:
        released = await provider.unrent_number(number)
    except Exception as e:
        logger.critical(
            f"[{ref}] ORPHANED NUMBER: {number} was purchased from "
            f"{provider_name.value} but the release call itself raised: {e}. "
            f"Manual intervention required. "
            f"provider_api_id={provider_result.api_id}",
            exc_info=True,
        )
        return False

    if not released:
        logger.critical(
            f"[{ref}] ORPHANED NUMBER: {number} was purchased from "
            f"{provider_name.value} but could neither be registered in the database "
            f"nor released. Manual intervention required. "
            f"provider_api_id={provider_result.api_id}"
        )
    return released


async def buy_provider_number_handler(
    provider_name: CallProvider,
    request: TelephonyNumberBuyRequest,
    current_user: UserInfo,
) -> TelephonyNumberBuyResponse:
    """Buy a number from a provider and register it in the telephony_numbers table.

    Steps 1-4 (pre-check through registration) run under a per-number Redis
    lock so a double-submit for the same number cannot trigger two real
    provider purchases; a concurrent request gets a 409 instead of racing.

    Raises:
        HTTPException: 400/403 if ownership can't be resolved within the
            caller's scope, 409 if the number is already registered or a
            purchase for it is already in flight, 502 on provider failure,
            503 if the provider or database is unavailable, 500 if the
            purchase succeeded but registration did not.
    """
    ref = str(uuid4())
    logger.info(
        f"[{ref}] User {current_user.username} ({current_user.role}) buying "
        f"{provider_name.value} number {request.number}: requested "
        f"reseller={request.reseller_id}, merchant={request.merchant_id}"
    )

    # Step 0: resolve ownership within the caller's own scope. This never
    # trusts the request body as-is for non-admins, and must run before
    # anything is spent -- a rejection here means zero provider calls.
    reseller_id, merchant_id = await resolve_buy_scope(
        current_user, request.reseller_id, request.merchant_id
    )
    logger.info(
        f"[{ref}] Resolved ownership for {request.number}: "
        f"reseller={reseller_id}, merchant={merchant_id}"
    )

    # Steps 1-4 run under a per-number lock: a concurrent request for the
    # same number must queue behind this one rather than both racing the
    # provider, which is the only way to guarantee "at most one purchase"
    # without trusting provider-side atomicity we have no documentation for.
    try:
        async with RedisLock(
            f"numbers:buy:{provider_name.value}:{request.number}",
            ttl_seconds=_BUY_LOCK_TTL_SECONDS,
        ):
            # Step 1: duplicate pre-check. This must fail loudly: if the
            # database is unreachable we cannot tell "not owned" from
            # "cannot check", and guessing wrong means paying the provider
            # for a number we may already own.
            try:
                conflict_status = await check_number_purchase_conflict(request.number)
            except Exception as e:
                logger.error(
                    f"[{ref}] Availability check failed for {request.number}: {e}",
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Could not verify number availability. No purchase "
                        f"was made. Please retry. (ref: {ref})"
                    ),
                )

            if conflict_status is not None:
                # .value, not the member: an f-string on a (str, Enum)
                # renders as "TelephonyNumberStatus.AVAILABLE" on 3.11.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Number {request.number} is already registered "
                        f"(status: {conflict_status.value}). (ref: {ref})"
                    ),
                )

            # Step 2: resolve the provider
            try:
                provider = get_number_provider(provider_name)
            except ValueError as e:
                logger.error(f"[{ref}] {provider_name.value} not available: {e}")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        f"Buying numbers from {provider_name.value} is not "
                        f"available. (ref: {ref})"
                    ),
                )

            # Step 3: purchase
            try:
                provider_result = await provider.buy_number(request)
            except Exception as e:
                logger.error(
                    f"[{ref}] {provider_name.value} purchase failed for "
                    f"{request.number}: {e}",
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        f"Purchase from {provider_name.value} failed. " f"(ref: {ref})"
                    ),
                )

            if not provider_result.is_fulfilled:
                logger.error(
                    f"[{ref}] {provider_name.value} purchase not fulfilled for "
                    f"{request.number}: status={provider_result.status}, "
                    f"message={provider_result.message}"
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        f"{provider_name.value} did not fulfil the purchase "
                        f"(status: {provider_result.status}). (ref: {ref})"
                    ),
                )

            # Step 4: register. From here the number is rented, so every
            # failure path below must release it again.
            try:
                telephony_number = await create_telephony_number(
                    id=str(uuid4()),
                    number=request.number,
                    provider=provider_name,
                    status=TelephonyNumberStatus.AVAILABLE,
                    reseller_id=reseller_id,
                    merchant_id=merchant_id,
                    channels=0,
                    maximum_channels=request.maximum_channels,
                )
                if not telephony_number:
                    raise RuntimeError("create_telephony_number returned no record")

            except Exception as e:
                logger.error(
                    f"[{ref}] Failed to register {request.number} after "
                    f"purchase: {e}",
                    exc_info=True,
                )
                released = await _release_number(
                    provider, provider_name, request.number, provider_result, ref
                )
                detail = f"Number could not be registered. (ref: {ref})"
                if not released:
                    detail = (
                        "Number was purchased but could not be registered or "
                        f"released. Support has been alerted. (ref: {ref})"
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail
                )
    except LockAcquireError:
        logger.warning(
            f"[{ref}] {request.number} is already being purchased by another " "request"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Number {request.number} is already being purchased. "
                f"Please retry shortly. (ref: {ref})"
            ),
        )

    logger.info(
        f"[{ref}] Registered {provider_name.value} number {request.number} "
        f"as telephony_number {telephony_number.id}"
    )

    return TelephonyNumberBuyResponse(
        provider_status=provider_result.status,
        provider_api_id=provider_result.api_id,
        telephony_number=telephony_number.model_dump(mode="json"),
        message=(
            f"Number {request.number} purchased and registered via "
            f"{provider_name.value}"
        ),
    )
