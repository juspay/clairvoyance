"""
Database accessor functions for the application.
"""

from datetime import time
from typing import List, Optional

import asyncpg

from app.core.logger import logger
from app.database.decoder.breeze_buddy.call_execution_config import (
    decode_call_execution_config,
    decode_call_execution_config_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.call_execution_config import (
    calling_activation_for_merchant_query,
    delete_call_execution_config_query,
    get_all_call_execution_configs_query,
    get_all_merchants_query,
    get_call_execution_config_by_id_query,
    get_call_execution_config_by_merchant_id_query,
    insert_call_execution_config_query,
    update_call_execution_config_query,
)
from app.schemas import CallExecutionConfig, CallProvider


def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


async def create_call_execution_config(
    id: str,
    initial_offset: int,
    retry_offset: int,
    call_start_time: time,
    call_end_time: time,
    max_retry: int,
    calling_provider: CallProvider,
    merchant_id: str,
    template: str,
    shop_identifier: Optional[str],
    enable_international_call: bool,
    enable_calling: bool = True,
    template_id: Optional[str] = None,  # NEW: Add template_id parameter
) -> Optional[CallExecutionConfig]:
    """
    Create a new call execution config record.

    Args:
        template_id: UUID of the template (preferred, for referential integrity)
        template: Name of the template (kept for backward compatibility)
    """
    logger.info(f"Creating call execution config for merchant ID: {merchant_id}")

    try:
        query_text, values = insert_call_execution_config_query(
            id=id,
            initial_offset=initial_offset,
            retry_offset=retry_offset,
            call_start_time=call_start_time,
            call_end_time=call_end_time,
            max_retry=max_retry,
            calling_provider=calling_provider,
            merchant_id=merchant_id,
            template=template,
            template_id=template_id,  # NEW
            shop_identifier=shop_identifier,
            enable_international_call=enable_international_call,
            enable_calling=enable_calling,
        )

        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_execution_config(result)
            logger.info(f"Call execution config created successfully: {decoded_result}")
            return decoded_result

        logger.error("Failed to create call execution config")
        return None

    except Exception as e:
        logger.error(f"Error creating call execution config: {e}")
        return None


async def get_call_execution_config_by_merchant_id(
    merchant_id: str,
    shop_identifier: Optional[str] = None,
) -> List[CallExecutionConfig]:
    """
    Get call execution config by merchant ID.
    """
    logger.info(f"Getting call execution config by merchant ID: {merchant_id}")

    try:
        query_text, values = get_call_execution_config_by_merchant_id_query(
            merchant_id, shop_identifier
        )
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_call_execution_config_list(result)
            logger.info(
                f"Found {len(decoded_result)} call execution configs for merchant ID: {merchant_id}"
            )
            return decoded_result

        if shop_identifier:
            # If no config is found for the specific shop_identifier, try with NULL
            logger.info(
                f"No config found for shop_identifier {shop_identifier}, trying generic config."
            )
            query_text, values = get_call_execution_config_by_merchant_id_query(
                merchant_id, None
            )
            result = await run_parameterized_query(query_text, values)
            if result:
                decoded_result = decode_call_execution_config_list(result)
                logger.info(
                    f"Found {len(decoded_result)} generic call execution configs for merchant ID: {merchant_id}"
                )
                return decoded_result

        logger.info(f"No call execution config found with merchant ID: {merchant_id}")
        return []

    except Exception as e:
        logger.error(f"Error getting call execution config by merchant ID: {e}")
        return []


async def get_all_call_execution_configs() -> List[CallExecutionConfig]:
    """
    Get all call execution configs.
    """
    logger.info("Getting all call execution configs")

    try:
        query_text, values = get_all_call_execution_configs_query()
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_call_execution_config_list(result)
            logger.info(f"Found {len(decoded_result)} call execution configs")
            return decoded_result

        logger.info("No call execution configs found")
        return []

    except Exception as e:
        logger.error(f"Error getting all call execution configs: {e}")
        return []


async def update_call_execution_config(
    merchant_id: str,
    template: str,
    shop_identifier: Optional[str] = None,
    initial_offset: Optional[int] = None,
    retry_offset: Optional[int] = None,
    call_start_time: Optional[time] = None,
    call_end_time: Optional[time] = None,
    max_retry: Optional[int] = None,
    calling_provider: Optional[CallProvider] = None,
    enable_international_call: Optional[bool] = None,
    enable_calling: Optional[bool] = None,
    template_id: Optional[str] = None,  # NEW: Add template_id parameter
) -> Optional[CallExecutionConfig]:
    """
    Update an existing call execution config record based on merchant_id, template, and shop_identifier.
    Only updates fields that are provided (not None).

    Args:
        template_id: UUID of the template (optional update)
        template: Name of the template (kept for backward compatibility)
    """
    logger.info(
        f"Updating call execution config for merchant: {merchant_id}, template: {template}, shop_identifier: {shop_identifier}"
    )

    try:
        query_text, values = update_call_execution_config_query(
            merchant_id=merchant_id,
            template=template,
            shop_identifier=shop_identifier,
            initial_offset=initial_offset,
            retry_offset=retry_offset,
            call_start_time=call_start_time,
            call_end_time=call_end_time,
            max_retry=max_retry,
            calling_provider=calling_provider,
            enable_international_call=enable_international_call,
            enable_calling=enable_calling,
            template_id=template_id,  # NEW
        )

        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_execution_config(result)
            logger.info(f"Call execution config updated successfully: {decoded_result}")
            return decoded_result

        logger.error(
            f"Failed to update call execution config for merchant: {merchant_id}, template: {template}, shop_identifier: {shop_identifier}"
        )
        return None

    except Exception as e:
        logger.error(f"Error updating call execution config: {e}")
        return None


async def get_call_execution_config_by_id(
    config_id: str,
) -> Optional[CallExecutionConfig]:
    """
    Get call execution config by ID.
    """
    logger.info(f"Getting call execution config by ID: {config_id}")

    try:
        query_text, values = get_call_execution_config_by_id_query(config_id)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_call_execution_config(result)
            logger.info(f"Found call execution config: {decoded_result}")
            return decoded_result

        logger.info(f"No call execution config found with ID: {config_id}")
        return None

    except Exception as e:
        logger.error(f"Error getting call execution config by ID: {e}")
        return None


async def delete_call_execution_config(config_id: str) -> bool:
    """
    Delete call execution config by ID.
    Returns True if deletion was successful, False otherwise.
    """
    logger.info(f"Deleting call execution config by ID: {config_id}")

    try:
        query_text, values = delete_call_execution_config_query(config_id)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            logger.info(f"Call execution config deleted successfully: {config_id}")
            return True

        logger.warning(f"No call execution config found to delete with ID: {config_id}")
        return False

    except Exception as e:
        logger.error(f"Error deleting call execution config: {e}")
        return False


async def calling_activation_for_merchant(
    enable_calling: bool,
    merchant_id: Optional[str] = None,
    shop_identifier: Optional[str] = None,
) -> List[CallExecutionConfig]:
    """
    Toggle enable_calling for configs.
    - If merchant_id is None: All configs across all merchants are updated
    - If merchant_id is provided but shop_identifier is None: All configs for that merchant are updated
    - If both merchant_id and shop_identifier are provided: Only that specific config is updated
    """
    logger.info(
        f"Toggling calling to {enable_calling} for merchant: {merchant_id}, shop_identifier: {shop_identifier}"
    )

    try:
        query_text, values = calling_activation_for_merchant_query(
            merchant_id=merchant_id,
            enable_calling=enable_calling,
            shop_identifier=shop_identifier,
        )

        result = await run_parameterized_query(query_text, values)
        if result:
            decoded_result = decode_call_execution_config_list(result)
            logger.info(f"Successfully updated {len(decoded_result)} config(s)")
            return decoded_result

        logger.info(
            f"No configs found for merchant {merchant_id} with shop_identifier {shop_identifier}"
        )
        return []

    except Exception as e:
        logger.error(f"Error toggling calling status: {e}")
        return []


async def get_all_merchants() -> List[str]:
    """
    Get all unique merchants (shop_identifiers).

    Each shop_identifier represents a distinct merchant in the system.
    This assumes every shop has at least one call execution config.

    Returns:
        List of unique shop_identifier strings
    """
    logger.info("Getting all merchants (shop_identifiers)")

    try:
        query_text, values = get_all_merchants_query()
        result = await run_parameterized_query(query_text, values)

        if result:
            # Extract shop_identifier from each row
            merchants = [row["shop_identifier"] for row in result]
            logger.info(f"Found {len(merchants)} unique merchants")
            return merchants

        logger.info("No merchants found")
        return []

    except Exception as e:
        logger.error(f"Error getting all merchants: {e}", exc_info=True)
        return []
