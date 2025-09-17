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
    get_call_execution_config_by_merchant_id_query,
    insert_call_execution_config_query,
)
from app.schemas import CallExecutionConfig, CallProvider, Workflow


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
    workflow: Workflow,
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
            workflow=workflow,
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
) -> List[CallExecutionConfig]:
    """
    Get call execution config by merchant ID.
    """
    logger.info(f"Getting call execution config by merchant ID: {merchant_id}")

    try:
        query_text, values = get_call_execution_config_by_merchant_id_query(merchant_id)
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_call_execution_config_list(result)
            logger.info(
                f"Found {len(decoded_result)} call execution configs for merchant ID: {merchant_id}"
            )
            return decoded_result

        logger.info(f"No call execution config found with merchant ID: {merchant_id}")
        return []

    except Exception as e:
        logger.error(f"Error getting call execution config by merchant ID: {e}")
        return []
