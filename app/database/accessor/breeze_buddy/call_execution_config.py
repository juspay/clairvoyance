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
    get_all_call_execution_configs_query,
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
    shop_identifier: str,
    enable_international_call: bool,
) -> Optional[CallExecutionConfig]:
    """
    Create a new call execution config record.
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
            shop_identifier=shop_identifier,
            enable_international_call=enable_international_call,
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
) -> Optional[CallExecutionConfig]:
    """
    Update an existing call execution config record based on merchant_id, template, and shop_identifier.
    Only updates fields that are provided (not None).
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
