"""
Database accessor functions for the application.
"""

import json
from datetime import time
from typing import Any, List, Optional

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
from app.schemas import CallExecutionConfig, CallProvider, TelephonyConfig


def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


def _serialize_pre_checks(pre_checks: Optional[List[Any]]) -> Optional[str]:
    """Serialize pre_checks list to JSON string for JSONB storage."""
    if pre_checks is None:
        return None
    return json.dumps(
        [pc.model_dump() if hasattr(pc, "model_dump") else pc for pc in pre_checks]
    )


async def create_call_execution_config(
    id: str,
    initial_offset: int,
    retry_offset: int,
    call_start_time: time,
    call_end_time: time,
    max_retry: int,
    calling_provider: CallProvider,
    reseller_id: str,
    template: str,
    merchant_id: Optional[str],
    enable_international_call: bool,
    enable_calling: bool = True,
    enable_inbound: bool = True,
    inbound_call_start_time: Optional[time] = None,
    inbound_call_end_time: Optional[time] = None,
    inbound_call_timezone: Optional[str] = None,
    inbound_block_action: Optional[str] = None,
    inbound_redirect_number: Optional[str] = None,
    inbound_block_message: Optional[str] = None,
    enforce_blacklist: bool = True,
    rate_limit_enabled: bool = False,
    rate_limit_max_calls: Optional[int] = None,
    rate_limit_window_seconds: Optional[int] = None,
    rate_limit_whitelist: Optional[str] = None,
    template_id: Optional[str] = None,
    pre_checks: Optional[List[Any]] = None,
    telephony_config: Optional[TelephonyConfig] = None,
) -> Optional[CallExecutionConfig]:
    """Create a new call execution config record."""
    logger.info(f"Creating call execution config for reseller ID: {reseller_id}")

    try:
        query_text, values = insert_call_execution_config_query(
            id=id,
            initial_offset=initial_offset,
            retry_offset=retry_offset,
            call_start_time=call_start_time,
            call_end_time=call_end_time,
            max_retry=max_retry,
            calling_provider=calling_provider,
            reseller_id=reseller_id,
            template=template,
            template_id=template_id,
            merchant_id=merchant_id,
            enable_international_call=enable_international_call,
            enable_calling=enable_calling,
            enable_inbound=enable_inbound,
            inbound_call_start_time=inbound_call_start_time,
            inbound_call_end_time=inbound_call_end_time,
            inbound_call_timezone=inbound_call_timezone,
            inbound_block_action=inbound_block_action,
            inbound_redirect_number=inbound_redirect_number,
            inbound_block_message=inbound_block_message,
            enforce_blacklist=enforce_blacklist,
            rate_limit_enabled=rate_limit_enabled,
            rate_limit_max_calls=rate_limit_max_calls,
            rate_limit_window_seconds=rate_limit_window_seconds,
            rate_limit_whitelist=rate_limit_whitelist,
            pre_checks=_serialize_pre_checks(pre_checks),
            telephony_config=(
                json.dumps(telephony_config.model_dump()) if telephony_config else None
            ),
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
    reseller_id: str,
    merchant_id: Optional[str] = None,
) -> List[CallExecutionConfig]:
    """
    Get call execution config by reseller ID.
    """
    logger.info(f"Getting call execution config by reseller ID: {reseller_id}")

    try:
        query_text, values = get_call_execution_config_by_merchant_id_query(
            reseller_id, merchant_id
        )
        result = await run_parameterized_query(query_text, values)

        if result:
            decoded_result = decode_call_execution_config_list(result)
            logger.info(
                f"Found {len(decoded_result)} call execution configs for reseller ID: {reseller_id}"
            )
            return decoded_result

        if merchant_id:
            # If no config is found for the specific merchant_id, try with NULL
            logger.info(
                f"No config found for merchant_id {merchant_id}, trying generic config."
            )
            query_text, values = get_call_execution_config_by_merchant_id_query(
                reseller_id, None
            )
            result = await run_parameterized_query(query_text, values)
            if result:
                decoded_result = decode_call_execution_config_list(result)
                logger.info(
                    f"Found {len(decoded_result)} generic call execution configs for reseller ID: {reseller_id}"
                )
                return decoded_result

        logger.info(f"No call execution config found with reseller ID: {reseller_id}")
        return []

    except Exception as e:
        logger.error(f"Error getting call execution config by reseller ID: {e}")
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
    reseller_id: str,
    template: str,
    merchant_id: Optional[str] = None,
    initial_offset: Optional[int] = None,
    retry_offset: Optional[int] = None,
    call_start_time: Optional[time] = None,
    call_end_time: Optional[time] = None,
    max_retry: Optional[int] = None,
    calling_provider: Optional[CallProvider] = None,
    enable_international_call: Optional[bool] = None,
    enable_calling: Optional[bool] = None,
    enable_inbound: Optional[bool] = None,
    inbound_call_start_time: Optional[time] = None,
    inbound_call_end_time: Optional[time] = None,
    inbound_call_timezone: Optional[str] = None,
    inbound_block_action: Optional[str] = None,
    inbound_redirect_number: Optional[str] = None,
    inbound_block_message: Optional[str] = None,
    enforce_blacklist: Optional[bool] = None,
    rate_limit_enabled: Optional[bool] = None,
    rate_limit_max_calls: Optional[int] = None,
    rate_limit_window_seconds: Optional[int] = None,
    rate_limit_whitelist: Optional[str] = None,
    template_id: Optional[str] = None,
    pre_checks: Optional[List[Any]] = None,
    telephony_config: Optional[TelephonyConfig] = None,
) -> Optional[CallExecutionConfig]:
    """
    Update an existing call execution config record based on reseller_id, template, and merchant_id.
    Only updates fields that are provided (not None).

    Args:
        template_id: UUID of the template (optional update)
        template: Name of the template (kept for backward compatibility)
        pre_checks: List of PreCheckConfig objects for call pre-validation
        telephony_config: Optional telephony provider overrides
    """
    logger.info(
        f"Updating call execution config for reseller: {reseller_id}, template: {template}, merchant_id: {merchant_id}"
    )

    try:
        # Serialize telephony_config for the query:
        #  - None  → don't touch the column (skip in SET clause)
        #  - TelephonyConfig() with all-None fields → clear to SQL NULL
        #  - TelephonyConfig(applet_app_id="x") → store as JSONB
        if telephony_config is not None:
            has_any_value = any(
                v is not None for v in telephony_config.model_dump().values()
            )
            serialized_telephony = (
                json.dumps(telephony_config.model_dump())
                if has_any_value
                else "__CLEAR__"
            )
        else:
            serialized_telephony = None

        query_text, values = update_call_execution_config_query(
            reseller_id=reseller_id,
            template=template,
            merchant_id=merchant_id,
            initial_offset=initial_offset,
            retry_offset=retry_offset,
            call_start_time=call_start_time,
            call_end_time=call_end_time,
            max_retry=max_retry,
            calling_provider=calling_provider,
            enable_international_call=enable_international_call,
            enable_calling=enable_calling,
            enable_inbound=enable_inbound,
            inbound_call_start_time=inbound_call_start_time,
            inbound_call_end_time=inbound_call_end_time,
            inbound_call_timezone=inbound_call_timezone,
            inbound_block_action=inbound_block_action,
            inbound_redirect_number=inbound_redirect_number,
            inbound_block_message=inbound_block_message,
            enforce_blacklist=enforce_blacklist,
            rate_limit_enabled=rate_limit_enabled,
            rate_limit_max_calls=rate_limit_max_calls,
            rate_limit_window_seconds=rate_limit_window_seconds,
            rate_limit_whitelist=rate_limit_whitelist,
            template_id=template_id,
            pre_checks=_serialize_pre_checks(pre_checks),
            telephony_config=serialized_telephony,
        )

        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_call_execution_config(result)
            logger.info(f"Call execution config updated successfully: {decoded_result}")
            return decoded_result

        logger.error(
            f"Failed to update call execution config for reseller: {reseller_id}, template: {template}, merchant_id: {merchant_id}"
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
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> List[CallExecutionConfig]:
    """
    Toggle enable_calling for configs.
    - If reseller_id is None: All configs across all resellers are updated
    - If reseller_id is provided but merchant_id is None: All configs for that reseller are updated
    - If both reseller_id and merchant_id are provided: Only that specific config is updated
    """
    logger.info(
        f"Toggling calling to {enable_calling} for reseller: {reseller_id}, merchant_id: {merchant_id}"
    )

    try:
        query_text, values = calling_activation_for_merchant_query(
            reseller_id=reseller_id,
            enable_calling=enable_calling,
            merchant_id=merchant_id,
        )

        result = await run_parameterized_query(query_text, values)
        if result:
            decoded_result = decode_call_execution_config_list(result)
            logger.info(f"Successfully updated {len(decoded_result)} config(s)")
            return decoded_result

        logger.info(
            f"No configs found for reseller {reseller_id} with merchant_id {merchant_id}"
        )
        return []

    except Exception as e:
        logger.error(f"Error toggling calling status: {e}")
        return []


async def get_all_merchants() -> List[str]:
    """
    Get all unique resellers (merchant_ids).

    Each merchant_id represents a distinct reseller in the system.
    This assumes every merchant has at least one call execution config.

    Returns:
        List of unique merchant_id strings
    """
    logger.info("Getting all resellers (merchant_ids)")

    try:
        query_text, values = get_all_merchants_query()
        result = await run_parameterized_query(query_text, values)

        if result:
            # Extract merchant_id from each row
            resellers = [row["merchant_id"] for row in result]
            logger.info(f"Found {len(resellers)} unique resellers")
            return resellers

        logger.info("No resellers found")
        return []

    except Exception as e:
        logger.error(f"Error getting all resellers: {e}", exc_info=True)
        return []
