"""
Database accessor functions for outbound number pool operations.
"""

import logging
from typing import List, Optional

from app.database.decoder.breeze_buddy.outbound_number import (
    decode_outbound_number,
    decode_outbound_number_list,
)
from app.database.decoder.breeze_buddy.outbound_number_pool import (
    decode_outbound_number_pool,
    decode_outbound_number_pool_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.outbound_number_pool import (
    clear_number_pool_id_query,
    decrement_pool_channels_query,
    disable_outbound_number_pool_query,
    get_all_outbound_number_pools_query,
    get_numbers_by_pool_id_query,
    get_outbound_number_pool_by_id_query,
    get_outbound_number_pools_by_reseller_query,
    increment_pool_channels_query,
    increment_pool_rotation_index_query,
    insert_outbound_number_pool_query,
    set_number_pool_id_query,
    update_outbound_number_pool_query,
)
from app.schemas import OutboundNumber, OutboundNumberPool

logger = logging.getLogger(__name__)


def get_row_count(result) -> int:
    """Helper to get the number of rows returned by a query."""
    if result is None:
        return 0
    return len(result)


async def create_outbound_number_pool(
    id: str,
    name: str,
    provider: str,
    reseller_id: str,
    merchant_id: Optional[str] = None,
    max_channels: int = 0,
) -> Optional[OutboundNumberPool]:
    """Create a new outbound number pool."""
    try:
        query, values = insert_outbound_number_pool_query(
            id=id,
            name=name,
            provider=provider,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            max_channels=max_channels,
        )
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            logger.error("Failed to create outbound number pool: no rows returned")
            return None
        return decode_outbound_number_pool(result)
    except Exception as e:
        logger.error(f"Error creating outbound number pool: {e}")
        return None


async def get_outbound_number_pool_by_id(
    pool_id: str,
) -> Optional[OutboundNumberPool]:
    """Get a pool by its ID."""
    try:
        query, values = get_outbound_number_pool_by_id_query(pool_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return None
        return decode_outbound_number_pool(result)
    except Exception as e:
        logger.error(f"Error getting outbound number pool by id: {e}")
        return None


async def get_all_outbound_number_pools() -> List[OutboundNumberPool]:
    """Get all outbound number pools."""
    try:
        query, values = get_all_outbound_number_pools_query()
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return []
        return decode_outbound_number_pool_list(result)
    except Exception as e:
        logger.error(f"Error getting all outbound number pools: {e}")
        return []


async def get_outbound_number_pools_by_reseller(
    reseller_id: str,
    merchant_id: Optional[str] = None,
) -> List[OutboundNumberPool]:
    """Get pools for a reseller, optionally filtered by merchant."""
    try:
        query, values = get_outbound_number_pools_by_reseller_query(
            reseller_id, merchant_id
        )
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return []
        return decode_outbound_number_pool_list(result)
    except Exception as e:
        logger.error(f"Error getting outbound number pools by reseller: {e}")
        return []


async def update_outbound_number_pool(
    pool_id: str,
    name: Optional[str] = None,
    max_channels: Optional[int] = None,
) -> Optional[OutboundNumberPool]:
    """Update a pool's name or max_channels."""
    try:
        query, values = update_outbound_number_pool_query(
            pool_id=pool_id, name=name, max_channels=max_channels
        )
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return None
        return decode_outbound_number_pool(result)
    except Exception as e:
        logger.error(f"Error updating outbound number pool: {e}")
        return None


async def disable_outbound_number_pool(
    pool_id: str,
) -> Optional[OutboundNumberPool]:
    """Soft-delete a pool by setting status to DISABLED."""
    try:
        query, values = disable_outbound_number_pool_query(pool_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return None
        return decode_outbound_number_pool(result)
    except Exception as e:
        logger.error(f"Error disabling outbound number pool: {e}")
        return None


async def increment_pool_channels(
    pool_id: str,
) -> Optional[OutboundNumberPool]:
    """Atomically increment current_channels. Returns None if at capacity."""
    try:
        query, values = increment_pool_channels_query(pool_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            logger.warning(f"Pool {pool_id} at capacity — increment returned zero rows")
            return None
        return decode_outbound_number_pool(result)
    except Exception as e:
        logger.error(f"Error incrementing pool channels: {e}")
        return None


async def decrement_pool_channels(
    pool_id: str,
) -> Optional[OutboundNumberPool]:
    """Atomically decrement current_channels (floor at 0)."""
    try:
        query, values = decrement_pool_channels_query(pool_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return None
        return decode_outbound_number_pool(result)
    except Exception as e:
        logger.error(f"Error decrementing pool channels: {e}")
        return None


async def increment_pool_rotation_index(
    pool_id: str,
) -> Optional[OutboundNumberPool]:
    """Atomically increment the rotation index."""
    try:
        query, values = increment_pool_rotation_index_query(pool_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return None
        return decode_outbound_number_pool(result)
    except Exception as e:
        logger.error(f"Error incrementing pool rotation index: {e}")
        return None


async def get_numbers_by_pool_id(
    pool_id: str,
) -> List[OutboundNumber]:
    """Get all outbound numbers belonging to a pool."""
    try:
        query, values = get_numbers_by_pool_id_query(pool_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return []
        return decode_outbound_number_list(result)
    except Exception as e:
        logger.error(f"Error getting numbers by pool id: {e}")
        return []


async def set_number_pool_id(
    outbound_number_id: str,
    pool_id: str,
) -> Optional[OutboundNumber]:
    """Assign an outbound number to a pool."""
    try:
        query, values = set_number_pool_id_query(outbound_number_id, pool_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return None
        return decode_outbound_number(result)
    except Exception as e:
        logger.error(f"Error setting number pool id: {e}")
        return None


async def clear_number_pool_id(
    outbound_number_id: str,
) -> Optional[OutboundNumber]:
    """Remove an outbound number from its pool."""
    try:
        query, values = clear_number_pool_id_query(outbound_number_id)
        result = await run_parameterized_query(query, values)
        if get_row_count(result) == 0:
            return None
        return decode_outbound_number(result)
    except Exception as e:
        logger.error(f"Error clearing number pool id: {e}")
        return None
