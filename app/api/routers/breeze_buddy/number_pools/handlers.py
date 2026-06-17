"""
Handler functions for outbound number pool API endpoints.
"""

import logging
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.api.routers.breeze_buddy.number_pools.rbac import (
    validate_pool_access,
)
from app.database.accessor import (
    clear_number_pool_id,
    create_outbound_number_pool,
    disable_outbound_number_pool,
    get_all_outbound_number_pools,
    get_numbers_by_pool_id,
    get_outbound_number_by_id,
    get_outbound_number_pool_by_id,
    get_outbound_number_pools_by_reseller,
    set_number_pool_id,
    update_outbound_number_pool,
)
from app.schemas import (
    CreateOutboundNumberPoolRequest,
    OutboundNumber,
    OutboundNumberPool,
    UpdateOutboundNumberPoolRequest,
)
from app.schemas.breeze_buddy.auth import UserInfo
from app.schemas.breeze_buddy.core import CallProvider, OutboundNumberStatus

logger = logging.getLogger(__name__)


def _assert_number_not_in_use(number: OutboundNumber):
    """Raise 409 if the number has active calls.

    Twilio numbers are marked IN_USE when on a call.
    Exotel/Plivo numbers track concurrent calls via channels > 0.
    """
    if number.provider == CallProvider.TWILIO:
        if number.status == OutboundNumberStatus.IN_USE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Number {number.id} is currently in use — "
                "cannot change pool membership while a call is active",
            )
    else:
        # Exotel / Plivo — channel-based
        if number.channels is not None and number.channels > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Number {number.id} has {number.channels} active channel(s) — "
                "cannot change pool membership while calls are active",
            )


async def create_pool_handler(
    pool: CreateOutboundNumberPoolRequest,
    current_user: UserInfo,
) -> OutboundNumberPool:
    """Create a new outbound number pool."""
    if not pool.reseller_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reseller_id is required",
        )

    if pool.max_channels <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_channels must be a positive integer",
        )

    result = await create_outbound_number_pool(
        id=str(uuid4()),
        name=pool.name,
        provider=pool.provider.value,
        reseller_id=pool.reseller_id,
        merchant_id=pool.merchant_id,
        max_channels=pool.max_channels,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create outbound number pool",
        )

    return result


async def list_pools_handler(
    current_user: UserInfo,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> List[OutboundNumberPool]:
    """List outbound number pools, optionally filtered by reseller/merchant."""
    if reseller_id:
        pools = await get_outbound_number_pools_by_reseller(reseller_id, merchant_id)
    elif merchant_id:
        all_pools = await get_all_outbound_number_pools()
        pools = [p for p in all_pools if p.merchant_id == merchant_id]
    else:
        pools = await get_all_outbound_number_pools()
    return pools


async def get_pool_handler(
    pool_id: str,
    current_user: UserInfo,
) -> Dict:
    """Get a pool by ID, including its numbers."""
    pool = await get_outbound_number_pool_by_id(pool_id)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pool {pool_id} not found",
        )

    validate_pool_access(current_user, pool.reseller_id, pool.merchant_id)

    numbers = await get_numbers_by_pool_id(pool_id)

    return {
        "pool": pool,
        "numbers": numbers,
    }


async def update_pool_handler(
    pool_id: str,
    update: UpdateOutboundNumberPoolRequest,
    current_user: UserInfo,
) -> OutboundNumberPool:
    """Update a pool's name or max_channels."""
    existing = await get_outbound_number_pool_by_id(pool_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pool {pool_id} not found",
        )

    if update.max_channels is not None and update.max_channels <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_channels must be a positive integer",
        )

    result = await update_outbound_number_pool(
        pool_id=pool_id,
        name=update.name,
        max_channels=update.max_channels,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update outbound number pool",
        )

    return result


async def delete_pool_handler(
    pool_id: str,
    current_user: UserInfo,
) -> OutboundNumberPool:
    """Soft-delete a pool by setting status to DISABLED."""
    existing = await get_outbound_number_pool_by_id(pool_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pool {pool_id} not found",
        )

    result = await disable_outbound_number_pool(pool_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disable outbound number pool",
        )

    return result


async def add_number_to_pool_handler(
    pool_id: str,
    number_id: str,
    current_user: UserInfo,
) -> OutboundNumber:
    """Add an outbound number to a pool."""
    # Verify pool exists
    pool = await get_outbound_number_pool_by_id(pool_id)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pool {pool_id} not found",
        )

    # Verify number exists
    number = await get_outbound_number_by_id(number_id)
    if number is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Number {number_id} not found",
        )

    # Verify provider matches
    if number.provider != pool.provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Number provider ({number.provider.value}) does not match pool provider ({pool.provider.value})",
        )

    # Verify number is not already in another pool
    if number.pool_id is not None and number.pool_id != pool_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Number {number_id} already belongs to pool {number.pool_id}",
        )

    # Reject if the number has active calls — prevents channel accounting mismatches
    _assert_number_not_in_use(number)

    result = await set_number_pool_id(number_id, pool_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add number to pool",
        )

    return result


async def remove_number_from_pool_handler(
    pool_id: str,
    number_id: str,
    current_user: UserInfo,
) -> OutboundNumber:
    """Remove an outbound number from a pool."""
    number = await get_outbound_number_by_id(number_id)
    if number is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Number {number_id} not found",
        )

    if number.pool_id != pool_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Number {number_id} does not belong to pool {pool_id}",
        )

    # Reject if the number has active calls — prevents pool channel leak
    _assert_number_not_in_use(number)

    result = await clear_number_pool_id(number_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove number from pool",
        )

    return result
