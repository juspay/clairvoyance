"""
Database accessor functions for the application.
"""

from datetime import datetime
from typing import List, Optional

import asyncpg

from app.core.logger import logger
from app.database.decoder.breeze_buddy.outbound_number import (
    decode_outbound_number,
    decode_outbound_number_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.outbound_number import (
    decrement_outbound_number_channels_query,
    get_all_outbound_numbers_query,
    get_all_outbound_numbers_with_call_count_query,
    get_available_outbound_numbers_query,
    get_outbound_number_based_on_status_and_provider_query,
    get_outbound_number_by_id_query,
    get_outbound_number_by_number_query,
    increment_outbound_number_channels_query,
    insert_outbound_number_query,
    update_outbound_number_status_query,
)
from app.schemas import CallProvider, OutboundNumber, OutboundNumberStatus


def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


async def create_outbound_number(
    id: str,
    number: str,
    provider: CallProvider,
    status: OutboundNumberStatus,
    reseller_id: str,
    merchant_id: Optional[str] = None,
    channels: Optional[int] = None,
    maximum_channels: Optional[int] = None,
) -> Optional[OutboundNumber]:
    """
    Create a new outbound number record.
    """
    logger.info(f"Creating outbound number with ID: {id}")

    try:
        query_text, values = insert_outbound_number_query(
            id=id,
            number=number,
            provider=provider,
            status=status,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            channels=channels,
            maximum_channels=maximum_channels,
        )

        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number created successfully: {decoded_result}")
            return decoded_result

        logger.error("Failed to create outbound number")
        return None

    except Exception as e:
        logger.error(f"Error creating outbound number: {e}")
        return None


async def get_outbound_number_by_id(
    outbound_number_id: str,
) -> Optional[OutboundNumber]:
    """
    Get outbound number by ID.
    """
    logger.info(f"Getting outbound number by ID: {outbound_number_id}")

    try:
        query_text, values = get_outbound_number_by_id_query(outbound_number_id)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number found: {decoded_result}")
            return decoded_result

        logger.info(f"No outbound number found with ID: {outbound_number_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting outbound number by ID: {e}")
        return None


async def update_outbound_number_status(
    outbound_number_id: str, status: OutboundNumberStatus
) -> Optional[OutboundNumber]:
    """
    Update outbound number status.
    """
    logger.info(
        f"Updating outbound number status for ID: {outbound_number_id}, new status: {status}"
    )

    try:
        query_text, values = update_outbound_number_status_query(
            outbound_number_id, status
        )
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number status updated: {decoded_result}")
            return decoded_result

        logger.error(
            f"Failed to update outbound number status for ID: {outbound_number_id}"
        )
        return None

    except Exception as e:
        logger.error(f"Error updating outbound number status: {e}")
        return None


async def increment_outbound_number_channels(
    outbound_number_id: str,
) -> Optional[OutboundNumber]:
    """
    Atomically increment outbound number channels by 1.
    Only succeeds if channels < maximum_channels (enforces capacity limit).
    Returns None if the number is at capacity or doesn't exist.
    This avoids race conditions by using database-level atomic increment with constraint.
    """
    logger.info(f"Incrementing outbound number channels for ID: {outbound_number_id}")

    try:
        query_text, values = increment_outbound_number_channels_query(
            outbound_number_id
        )
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number channels incremented: {decoded_result}")
            return decoded_result

        # No rows updated means either the number doesn't exist or it's at capacity
        logger.warning(
            f"Could not increment channels for ID: {outbound_number_id} - "
            "number may be at maximum capacity or does not exist"
        )
        return None

    except Exception as e:
        logger.error(f"Error incrementing outbound number channels: {e}")
        return None


async def decrement_outbound_number_channels(
    outbound_number_id: str,
) -> Optional[OutboundNumber]:
    """
    Atomically decrement outbound number channels by 1.
    Uses GREATEST to ensure channels never goes below 0.
    This avoids race conditions by using database-level atomic decrement.
    """
    logger.info(f"Decrementing outbound number channels for ID: {outbound_number_id}")

    try:
        query_text, values = decrement_outbound_number_channels_query(
            outbound_number_id
        )
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number channels decremented: {decoded_result}")
            return decoded_result

        logger.warning(
            f"Failed to decrement outbound number channels for ID: {outbound_number_id}"
        )
        return None

    except Exception as e:
        logger.error(f"Error decrementing outbound number channels: {e}")
        return None


async def acquire_outbound_number(number: OutboundNumber) -> bool:
    """
    Marks an outbound number as in use.
    For channel-tracked providers (EXOTEL/PLIVO): atomically increments channels.
    For status-based providers (TWILIO): sets status to IN_USE.
    Returns True if acquisition succeeded, False if at capacity or failed.
    """
    logger.info(f"Acquiring outbound number {number.id} (provider: {number.provider})")
    if number.uses_channel_tracking:
        result = await increment_outbound_number_channels(number.id)
    else:
        result = await update_outbound_number_status(
            number.id, OutboundNumberStatus.IN_USE
        )
    return result is not None


async def release_outbound_number(number_id: str, provider: CallProvider) -> None:
    """
    Releases an outbound number, making it available for other calls.
    For channel-tracked providers (EXOTEL/PLIVO): atomically decrements channels.
    For status-based providers (TWILIO): sets status back to AVAILABLE.
    """
    logger.info(f"Releasing outbound number {number_id} (provider: {provider})")
    if provider in (CallProvider.EXOTEL, CallProvider.PLIVO):
        await decrement_outbound_number_channels(number_id)
    else:
        await update_outbound_number_status(number_id, OutboundNumberStatus.AVAILABLE)


async def get_all_outbound_numbers() -> List[OutboundNumber]:
    """
    Get all outbound numbers.
    """
    logger.info("Getting all outbound numbers")

    try:
        query_text, values = get_all_outbound_numbers_query()
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_outbound_number_list(result)
            logger.info(f"Found {len(decoded_result)} outbound number records")
            return decoded_result

        logger.info("No outbound numbers found")
        return []

    except Exception as e:
        logger.error(f"Error getting all outbound numbers: {e}")
        return []


async def get_all_outbound_numbers_with_call_count(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[asyncpg.Record]:
    """
    Get all outbound numbers with their call counts.
    """
    logger.info("Getting all outbound numbers with call counts")

    try:
        query_text, values = get_all_outbound_numbers_with_call_count_query(
            start_date=start_date,
            end_date=end_date,
        )
        result = await run_parameterized_query(query_text, values)
        return result if result else []
    except Exception as e:
        logger.error(
            f"Error getting outbound numbers with call counts: {e}", exc_info=True
        )
        return []


async def get_available_outbound_numbers(
    status: OutboundNumberStatus,
) -> List[OutboundNumber]:
    """
    Get all outbound numbers by status, across all providers.
    """
    logger.info(f"Getting all outbound numbers with status: {status}")

    try:
        query_text, values = get_available_outbound_numbers_query(status)
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_outbound_number_list(result)
            logger.info(f"Found {len(decoded_result)} outbound numbers")
            return decoded_result

        logger.info(f"No outbound numbers found with status: {status}")
        return []

    except Exception as e:
        logger.error(f"Error getting outbound numbers: {e}")
        return []


async def get_outbound_number_based_on_status_and_provider(
    status: OutboundNumberStatus, provider: CallProvider
) -> List[OutboundNumber]:
    """
    Get outbound numbers by status and provider.
    """
    logger.info(
        f"Getting outbound numbers with status: {status} and provider: {provider}"
    )

    try:
        query_text, values = get_outbound_number_based_on_status_and_provider_query(
            status, provider
        )
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_outbound_number_list(result)
            logger.info(f"Found {len(decoded_result)} outbound numbers")
            return decoded_result

        logger.info(
            f"No outbound numbers found with status: {status} and provider: {provider}"
        )
        return []

    except Exception as e:
        logger.error(f"Error getting outbound numbers: {e}")
        return []


async def get_outbound_number_by_number(number: str) -> Optional[OutboundNumber]:
    """
    Get outbound number by phone number.
    """
    logger.info(f"Getting outbound number by phone number: {number}")

    try:
        query_text, values = get_outbound_number_by_number_query(number)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number found: {decoded_result}")
            return decoded_result

        logger.info(f"No outbound number found with number: {number}")
        return None

    except Exception as e:
        logger.error(f"Error getting outbound number by number: {e}")
        return None
