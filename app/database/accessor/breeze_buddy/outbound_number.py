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
    claim_outbound_number_if_available_query,
    decrement_outbound_number_channels_query,
    disable_outbound_number_query,
    get_all_outbound_numbers_query,
    get_all_outbound_numbers_with_call_count_query,
    get_available_outbound_numbers_by_provider_and_reseller_query,
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


async def claim_outbound_number_if_available(
    outbound_number_id: str,
) -> Optional[OutboundNumber]:
    """
    Atomically claim an outbound number (Twilio) by setting status to IN_USE
    only when the current status is AVAILABLE.

    Returns the updated record on success, or None if the number was already
    claimed by a concurrent worker (or doesn't exist).
    """
    logger.info(f"Claiming outbound number (if available) for ID: {outbound_number_id}")
    try:
        query_text, values = claim_outbound_number_if_available_query(
            outbound_number_id
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number claimed atomically: {decoded_result}")
            return decoded_result
        logger.warning(
            f"Could not claim outbound number {outbound_number_id} — "
            "already in use or does not exist"
        )
        return None
    except Exception as e:
        logger.error(f"Error claiming outbound number {outbound_number_id}: {e}")
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


async def disable_outbound_number(outbound_number_id: str) -> Optional[OutboundNumber]:
    """
    Disable outbound number by ID.
    """
    logger.info(f"Disabling outbound number with ID: {outbound_number_id}")

    try:
        query_text, values = disable_outbound_number_query(outbound_number_id)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_outbound_number(result)
            logger.info(f"Outbound number disabled successfully: {decoded_result}")
            return decoded_result

        logger.error(f"Failed to disable outbound number with ID: {outbound_number_id}")
        return None

    except Exception as e:
        logger.error(f"Error disabling outbound number: {e}")
        return None


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


async def get_available_outbound_numbers_by_provider_and_reseller(
    provider: CallProvider, reseller_id: str
) -> List[OutboundNumber]:
    """
    Return AVAILABLE outbound numbers for a provider scoped to a reseller.

    Reseller-specific numbers are returned before generic ones (reseller_id IS NULL),
    with least-loaded numbers first within each tier. Used by the Daily warm-transfer
    handler to pick and claim a number from the pool when the lead has no
    outbound_number_id.
    """
    try:
        query_text, values = (
            get_available_outbound_numbers_by_provider_and_reseller_query(
                provider, reseller_id
            )
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            return decode_outbound_number_list(result)
        return []
    except Exception as e:
        logger.error(
            f"Error fetching available outbound numbers for provider={provider} "
            f"reseller={reseller_id}: {e}"
        )
        return []
