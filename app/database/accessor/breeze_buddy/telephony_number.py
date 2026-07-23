"""
Database accessor functions for the application.
"""

from datetime import datetime
from typing import List, Optional

import asyncpg

from app.core.logger import logger
from app.database.decoder.breeze_buddy.telephony_number import (
    decode_telephony_number,
    decode_telephony_number_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.telephony_number import (
    decrement_telephony_number_channels_query,
    disable_telephony_number_query,
    get_all_telephony_numbers_query,
    get_all_telephony_numbers_with_call_count_query,
    get_telephony_number_based_on_status_and_provider_query,
    get_telephony_number_by_id_query,
    get_telephony_number_by_number_query,
    get_template_pinned_number_ids_query,
    increment_telephony_number_channels_query,
    insert_telephony_number_query,
    update_telephony_number_query,
    update_telephony_number_status_query,
)
from app.schemas import CallProvider, TelephonyNumber, TelephonyNumberStatus


def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


async def create_telephony_number(
    id: str,
    number: str,
    provider: CallProvider,
    status: TelephonyNumberStatus,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    channels: Optional[int] = None,
    maximum_channels: Optional[int] = None,
) -> Optional[TelephonyNumber]:
    """
    Create a new telephony number record. Ownership shapes:
    merchant_id set = merchant-owned, reseller_id only = umbrella-owned,
    both None = shared platform pool.
    """
    logger.info(f"Creating telephony number with ID: {id}")

    try:
        query_text, values = insert_telephony_number_query(
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
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number created successfully: {decoded_result}")
            return decoded_result

        logger.error("Failed to create telephony number")
        return None

    except asyncpg.exceptions.UniqueViolationError:
        # Re-raised, not swallowed: callers that need to tell "lost a race to
        # a concurrent insert" apart from "creation failed" (e.g. the buy flow's
        # rollback logic) must see this distinctly. Other exception types keep
        # the fail-soft None contract below, unchanged for existing callers.
        raise

    except Exception as e:
        logger.error(f"Error creating telephony number: {e}", exc_info=True)
        return None


async def get_telephony_number_by_id(
    telephony_number_id: str,
) -> Optional[TelephonyNumber]:
    """
    Get telephony number by ID.
    """
    logger.info(f"Getting telephony number by ID: {telephony_number_id}")

    try:
        query_text, values = get_telephony_number_by_id_query(telephony_number_id)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number found: {decoded_result}")
            return decoded_result

        logger.info(f"No telephony number found with ID: {telephony_number_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting telephony number by ID: {e}")
        return None


async def update_telephony_number_status(
    telephony_number_id: str, status: TelephonyNumberStatus
) -> Optional[TelephonyNumber]:
    """
    Update telephony number status.
    """
    logger.info(
        f"Updating telephony number status for ID: {telephony_number_id}, new status: {status}"
    )

    try:
        query_text, values = update_telephony_number_status_query(
            telephony_number_id, status
        )
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number status updated: {decoded_result}")
            return decoded_result

        logger.error(
            f"Failed to update telephony number status for ID: {telephony_number_id}"
        )
        return None

    except Exception as e:
        logger.error(f"Error updating telephony number status: {e}")
        return None


async def increment_telephony_number_channels(
    telephony_number_id: str,
) -> Optional[TelephonyNumber]:
    """
    Atomically increment telephony number channels by 1.
    Only succeeds if channels < maximum_channels (enforces capacity limit).
    Returns None if the number is at capacity or doesn't exist.
    This avoids race conditions by using database-level atomic increment with constraint.
    """
    logger.info(f"Incrementing telephony number channels for ID: {telephony_number_id}")

    try:
        query_text, values = increment_telephony_number_channels_query(
            telephony_number_id
        )
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number channels incremented: {decoded_result}")
            return decoded_result

        # No rows updated means either the number doesn't exist or it's at capacity
        logger.warning(
            f"Could not increment channels for ID: {telephony_number_id} - "
            "number may be at maximum capacity or does not exist"
        )
        return None

    except Exception as e:
        logger.error(f"Error incrementing telephony number channels: {e}")
        return None


async def decrement_telephony_number_channels(
    telephony_number_id: str,
) -> Optional[TelephonyNumber]:
    """
    Atomically decrement telephony number channels by 1.
    Uses GREATEST to ensure channels never goes below 0.
    This avoids race conditions by using database-level atomic decrement.
    """
    logger.info(f"Decrementing telephony number channels for ID: {telephony_number_id}")

    try:
        query_text, values = decrement_telephony_number_channels_query(
            telephony_number_id
        )
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number channels decremented: {decoded_result}")
            return decoded_result

        logger.warning(
            f"Failed to decrement telephony number channels for ID: {telephony_number_id}"
        )
        return None

    except Exception as e:
        logger.error(f"Error decrementing telephony number channels: {e}")
        return None


async def disable_telephony_number(
    telephony_number_id: str,
) -> Optional[TelephonyNumber]:
    """
    Disable telephony number by ID.
    """
    logger.info(f"Disabling telephony number with ID: {telephony_number_id}")

    try:
        query_text, values = disable_telephony_number_query(telephony_number_id)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number disabled successfully: {decoded_result}")
            return decoded_result

        logger.error(
            f"Failed to disable telephony number with ID: {telephony_number_id}"
        )
        return None

    except Exception as e:
        logger.error(f"Error disabling telephony number: {e}")
        return None


async def get_all_telephony_numbers() -> List[TelephonyNumber]:
    """
    Get all telephony numbers.
    """
    logger.info("Getting all telephony numbers")

    try:
        query_text, values = get_all_telephony_numbers_query()
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_telephony_number_list(result)
            logger.info(f"Found {len(decoded_result)} telephony number records")
            return decoded_result

        logger.info("No telephony numbers found")
        return []

    except Exception as e:
        logger.error(f"Error getting all telephony numbers: {e}")
        return []


async def get_all_telephony_numbers_with_call_count(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[asyncpg.Record]:
    """
    Get all telephony numbers with their call counts.
    """
    logger.info("Getting all telephony numbers with call counts")

    try:
        query_text, values = get_all_telephony_numbers_with_call_count_query(
            start_date=start_date,
            end_date=end_date,
        )
        result = await run_parameterized_query(query_text, values)
        return result if result else []
    except Exception as e:
        logger.error(
            f"Error getting telephony numbers with call counts: {e}", exc_info=True
        )
        return []


async def get_telephony_number_based_on_status_and_provider(
    status: TelephonyNumberStatus, provider: CallProvider
) -> List[TelephonyNumber]:
    """
    Get telephony numbers by status and provider.
    """
    logger.info(
        f"Getting telephony numbers with status: {status} and provider: {provider}"
    )

    try:
        query_text, values = get_telephony_number_based_on_status_and_provider_query(
            status, provider
        )
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_telephony_number_list(result)
            logger.info(f"Found {len(decoded_result)} telephony numbers")
            return decoded_result

        logger.info(
            f"No telephony numbers found with status: {status} and provider: {provider}"
        )
        return []

    except Exception as e:
        logger.error(f"Error getting telephony numbers: {e}")
        return []


async def update_telephony_number(
    telephony_number_id: str,
    status: Optional[TelephonyNumberStatus] = None,
    maximum_channels: Optional[int] = None,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    clear_ownership: bool = False,
    set_ownership: bool = False,
) -> Optional[TelephonyNumber]:
    """
    Partially update a telephony number (ownership / status / capacity).
    set_ownership=True rewrites BOTH ownership columns from the given values
    (a reassignment never leaves the previous owner half-set);
    clear_ownership=True returns the number to the shared pool. A
    maximum_channels change is picked up by the channel-token reconciler on
    its next pass (top-up/trim of the Redis semaphore) — no immediate resize.
    """
    logger.info(
        f"Updating telephony number {telephony_number_id} "
        f"(status={status}, max_channels={maximum_channels}, "
        f"reseller={reseller_id}, merchant={merchant_id}, "
        f"set={set_ownership}, clear={clear_ownership})"
    )

    try:
        query_text, values = update_telephony_number_query(
            telephony_number_id,
            status=status,
            maximum_channels=maximum_channels,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            clear_ownership=clear_ownership,
            set_ownership=set_ownership,
        )
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number updated: {decoded_result}")
            return decoded_result

        logger.error(f"Failed to update telephony number {telephony_number_id}")
        return None

    except Exception as e:
        logger.error(f"Error updating telephony number: {e}")
        return None


async def get_template_pinned_number_ids(
    merchant_ids: List[str], reseller_ids: List[str]
) -> List[str]:
    """
    Ids of numbers pinned by templates belonging to the given merchants or
    umbrellas (for RBAC-scoping the numbers list).
    """
    if not merchant_ids and not reseller_ids:
        return []
    try:
        query_text, values = get_template_pinned_number_ids_query(
            merchant_ids, reseller_ids
        )
        result = await run_parameterized_query(query_text, values)
        # template.telephony_number_id is a UUID column, so asyncpg decodes
        # uuid.UUID objects — but telephony_numbers.id is VARCHAR (str), and
        # the RBAC membership checks are plain set lookups where str never
        # equals uuid.UUID. Coerce here, the single choke point.
        return [
            str(r["telephony_number_id"])
            for r in result or []
            if r["telephony_number_id"]
        ]
    except Exception as e:
        logger.error(f"Error getting template-pinned number ids: {e}")
        return []


async def get_telephony_number_by_number(number: str) -> Optional[TelephonyNumber]:
    """
    Get telephony number by phone number.
    """
    logger.info(f"Getting telephony number by phone number: {number}")

    try:
        query_text, values = get_telephony_number_by_number_query(number)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_telephony_number(result)
            logger.info(f"Telephony number found: {decoded_result}")
            return decoded_result

        logger.info(f"No telephony number found with number: {number}")
        return None

    except Exception as e:
        logger.error(f"Error getting telephony number by number: {e}")
        return None


async def check_number_purchase_conflict(
    number: str,
) -> Optional[TelephonyNumberStatus]:
    """
    Check whether a number is free to purchase, propagating database errors.

    Returns None if the number is available (no existing row, or the existing
    row is DISABLED -- a released number stays re-buyable, enforced here at
    the application level, not by a DB constraint). Otherwise returns the
    status of the row blocking the purchase, for the caller's error message.

    Unlike get_telephony_number_by_number, a database failure raises instead of
    being reported as "not found". Callers that spend money on the strength of a
    negative result (e.g. buying a number from a provider) must use this variant,
    so a DB outage cannot be mistaken for "number is free to purchase".
    """
    logger.info(f"Checking purchase conflict for phone number: {number}")

    try:
        query_text, values = get_telephony_number_by_number_query(number)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            existing = decode_telephony_number(result)
            if (
                existing is not None
                and existing.status != TelephonyNumberStatus.DISABLED
            ):
                return existing.status

        return None

    except Exception as e:
        logger.error(f"Error checking purchase conflict for number: {e}", exc_info=True)
        raise
