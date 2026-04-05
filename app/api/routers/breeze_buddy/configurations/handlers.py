"""
Business logic handlers for configuration operations.
All handlers perform database operations and enforce business rules.
"""

from typing import List, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor import (
    calling_activation_for_merchant,
    create_call_execution_config,
    delete_call_execution_config,
    get_all_call_execution_configs,
    get_call_execution_config_by_id,
    get_call_execution_config_by_merchant_id,
    update_call_execution_config,
)
from app.schemas import (
    CallExecutionConfig,
    CreateCallExecutionConfigRequest,
    UpdateCallExecutionConfigRequest,
    UserInfo,
)

from .rbac import validate_config_access


async def create_configuration_handler(
    config: CreateCallExecutionConfigRequest, current_user: UserInfo
) -> CallExecutionConfig:
    """
    Create a new call execution configuration.

    Args:
        config: Configuration creation request
        current_user: Current authenticated user

    Returns:
        Created configuration object

    Raises:
        HTTPException: 400 if creation fails
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) creating configuration "
        f"for reseller: {config.reseller_id}, template: {config.template}"
    )

    try:
        call_execution_config = await create_call_execution_config(
            id=str(uuid4()),
            initial_offset=config.initial_offset,
            retry_offset=config.retry_offset,
            call_start_time=config.call_start_time,
            call_end_time=config.call_end_time,
            max_retry=config.max_retry,
            calling_provider=config.calling_provider,
            reseller_id=config.reseller_id,
            template=config.template,
            merchant_id=config.merchant_id,
            enable_international_call=config.enable_international_call,
            enable_inbound=(
                config.enable_inbound if config.enable_inbound is not None else True
            ),
            inbound_call_start_time=config.inbound_call_start_time,
            inbound_call_end_time=config.inbound_call_end_time,
            inbound_call_timezone=config.inbound_call_timezone,
            inbound_block_action=(
                config.inbound_block_action.value
                if config.inbound_block_action
                else "REJECT"
            ),
            inbound_redirect_number=config.inbound_redirect_number,
            inbound_block_message=config.inbound_block_message,
            enforce_blacklist=(
                config.enforce_blacklist
                if config.enforce_blacklist is not None
                else True
            ),
            rate_limit_enabled=(
                config.rate_limit_enabled
                if config.rate_limit_enabled is not None
                else False
            ),
            rate_limit_max_calls=config.rate_limit_max_calls,
            rate_limit_window_seconds=(
                config.rate_limit_window_seconds
                if config.rate_limit_window_seconds is not None
                else 1800
            ),
            rate_limit_whitelist=config.rate_limit_whitelist,
            pre_checks=config.pre_checks,
            telephony_config=config.telephony_config,
        )

        if call_execution_config:
            logger.info(
                f"Configuration created successfully: ID={call_execution_config.id}, "
                f"reseller={config.reseller_id}, template={config.template}"
            )
            return call_execution_config
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create configuration",
            )

    except Exception as e:
        logger.error(f"Error creating configuration: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create configuration. Please check your input and try again.",
        )


async def list_configurations_handler(
    reseller_id: Optional[str],
    template: Optional[str],
    merchant_id: Optional[str],
    current_user: UserInfo,
) -> List[CallExecutionConfig]:
    """
    List configurations with optional filters.

    Args:
        reseller_id: Optional reseller ID filter
        template: Optional template filter
        merchant_id: Optional merchant ID filter
        current_user: Current authenticated user

    Returns:
        List of configurations (RBAC filtering applied separately)
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) listing configurations "
        f"(reseller={reseller_id}, template={template}, merchant={merchant_id})"
    )

    try:
        logger.info(
            f"Fetching configurations from database for reseller_id={reseller_id}"
        )
        # Get configurations based on filters
        if reseller_id:
            configs = await get_call_execution_config_by_merchant_id(reseller_id)
        else:
            configs = await get_all_call_execution_configs()

        # Apply additional filters
        if template:
            configs = [c for c in configs if c.template == template]

        if merchant_id:
            configs = [c for c in configs if c.merchant_id == merchant_id]

        logger.info(f"Found {len(configs)} configurations before RBAC filtering")
        return configs

    except Exception as e:
        logger.error(f"Error listing configurations: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve configurations. Please try again later.",
        )


async def get_configuration_handler(
    config_id: str, current_user: UserInfo
) -> CallExecutionConfig:
    """
    Get a single configuration by ID.

    Args:
        config_id: Configuration UUID
        current_user: Current authenticated user

    Returns:
        Configuration object

    Raises:
        HTTPException: 404 if not found
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) "
        f"requesting configuration: {config_id}"
    )

    try:
        config = await get_call_execution_config_by_id(config_id)

        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Configuration {config_id} not found",
            )

        return config

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting configuration {config_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve configuration. Please try again later.",
        )


async def update_configuration_handler(
    config_id: str, config: UpdateCallExecutionConfigRequest, current_user: UserInfo
) -> CallExecutionConfig:
    """
    Update an existing configuration.

    Args:
        config_id: Configuration UUID
        config: Update request
        current_user: Current authenticated user

    Returns:
        Updated configuration object

    Raises:
        HTTPException: 404 if not found, 403 if access denied
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) updating configuration: {config_id}"
    )

    try:
        # Verify configuration exists
        existing_config = await get_call_execution_config_by_id(config_id)
        if not existing_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Configuration {config_id} not found",
            )

        # RBAC: Validate access against existing configuration (not request body)
        validate_config_access(
            current_user,
            existing_config.reseller_id,
            existing_config.merchant_id,
            operation="update configuration for",
        )

        # Validate that identity fields match the existing configuration
        # This prevents accidentally updating a different record
        if existing_config.reseller_id != config.reseller_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot change reseller_id from {existing_config.reseller_id} to {config.reseller_id}",
            )

        if existing_config.template != config.template:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot change template from {existing_config.template} to {config.template}",
            )

        if existing_config.merchant_id != config.merchant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot change merchant_id from {existing_config.merchant_id} to {config.merchant_id}",
            )

        # Update configuration
        updated_config = await update_call_execution_config(
            reseller_id=config.reseller_id,
            template=config.template,
            merchant_id=config.merchant_id,
            initial_offset=config.initial_offset,
            retry_offset=config.retry_offset,
            call_start_time=config.call_start_time,
            call_end_time=config.call_end_time,
            max_retry=config.max_retry,
            calling_provider=config.calling_provider,
            enable_international_call=config.enable_international_call,
            enable_inbound=config.enable_inbound,
            inbound_call_start_time=config.inbound_call_start_time,
            inbound_call_end_time=config.inbound_call_end_time,
            inbound_call_timezone=config.inbound_call_timezone,
            inbound_block_action=(
                config.inbound_block_action.value
                if config.inbound_block_action
                else None
            ),
            inbound_redirect_number=config.inbound_redirect_number,
            inbound_block_message=config.inbound_block_message,
            enforce_blacklist=config.enforce_blacklist,
            rate_limit_enabled=config.rate_limit_enabled,
            rate_limit_max_calls=config.rate_limit_max_calls,
            rate_limit_window_seconds=config.rate_limit_window_seconds,
            rate_limit_whitelist=config.rate_limit_whitelist,
            pre_checks=config.pre_checks,
            telephony_config=config.telephony_config,
        )

        if updated_config:
            logger.info(f"Configuration {config_id} updated successfully")
            return updated_config
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Configuration {config_id} not found",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating configuration {config_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update configuration. Please check your input and try again.",
        )


async def delete_configuration_handler(config_id: str, current_user: UserInfo) -> None:
    """
    Delete a configuration.

    Args:
        config_id: Configuration UUID
        current_user: Current authenticated user

    Raises:
        HTTPException: 404 if not found
    """
    logger.info(f"Admin {current_user.username} deleting configuration: {config_id}")

    try:
        # Verify configuration exists
        existing_config = await get_call_execution_config_by_id(config_id)
        if not existing_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Configuration {config_id} not found",
            )

        # Delete configuration
        success = await delete_call_execution_config(config_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Configuration {config_id} not found",
            )

        logger.info(f"Configuration {config_id} deleted successfully")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting configuration {config_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete configuration. Please try again later.",
        )


async def calling_activation_handler(
    enable_calling: bool,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    current_user: UserInfo,
) -> dict:
    """
    Enable or disable calling globally or for specific resellers/shops.

    Args:
        enable_calling: Boolean to enable or disable calling
        reseller_id: Optional reseller ID filter
        merchant_id: Optional merchant identifier filter
        current_user: Current authenticated user

    Returns:
        Dictionary with status, message, and updated configs

    Raises:
        HTTPException: 404 if no configs found, 500 on error
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) toggling calling to {enable_calling} "
        f"for reseller: {reseller_id}, merchant_id: {merchant_id}"
    )

    try:
        updated_configs = await calling_activation_for_merchant(
            enable_calling=enable_calling,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
        )

        if updated_configs:
            logger.info(f"Successfully updated {len(updated_configs)} config(s)")
            return {
                "status": "success",
                "message": f"Updated {len(updated_configs)} config(s)",
                "configs": updated_configs,
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No call execution config found matching the criteria",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling calling status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle calling status. Please try again later.",
        )
