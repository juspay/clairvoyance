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
        f"for merchant: {config.merchant_id}, template: {config.template}"
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
            merchant_id=config.merchant_id,
            template=config.template,
            shop_identifier=config.shop_identifier,
            enable_international_call=config.enable_international_call,
        )

        if call_execution_config:
            logger.info(
                f"Configuration created successfully: ID={call_execution_config.id}, "
                f"merchant={config.merchant_id}, template={config.template}"
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
    merchant_id: Optional[str],
    template: Optional[str],
    shop_identifier: Optional[str],
    current_user: UserInfo,
) -> List[CallExecutionConfig]:
    """
    List configurations with optional filters.

    Args:
        merchant_id: Optional merchant ID filter
        template: Optional template filter
        shop_identifier: Optional shop identifier filter
        current_user: Current authenticated user

    Returns:
        List of configurations (RBAC filtering applied separately)
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) listing configurations "
        f"(merchant={merchant_id}, template={template}, shop={shop_identifier})"
    )

    try:
        # Get configurations based on filters
        if merchant_id:
            configs = await get_call_execution_config_by_merchant_id(merchant_id)
        else:
            configs = await get_all_call_execution_configs()

        # Apply additional filters
        if template:
            configs = [c for c in configs if c.template == template]

        if shop_identifier:
            configs = [c for c in configs if c.shop_identifier == shop_identifier]

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
            existing_config.merchant_id,
            existing_config.shop_identifier,
            operation="update configuration for",
        )

        # Validate that identity fields match the existing configuration
        # This prevents accidentally updating a different record
        if existing_config.merchant_id != config.merchant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot change merchant_id from {existing_config.merchant_id} to {config.merchant_id}",
            )

        if existing_config.template != config.template:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot change template from {existing_config.template} to {config.template}",
            )

        if existing_config.shop_identifier != config.shop_identifier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot change shop_identifier from {existing_config.shop_identifier} to {config.shop_identifier}",
            )

        # Update configuration
        updated_config = await update_call_execution_config(
            merchant_id=config.merchant_id,
            template=config.template,
            shop_identifier=config.shop_identifier,
            initial_offset=config.initial_offset,
            retry_offset=config.retry_offset,
            call_start_time=config.call_start_time,
            call_end_time=config.call_end_time,
            max_retry=config.max_retry,
            calling_provider=config.calling_provider,
            enable_international_call=config.enable_international_call,
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
    merchant_id: Optional[str],
    shop_identifier: Optional[str],
    current_user: UserInfo,
) -> dict:
    """
    Enable or disable calling globally or for specific merchants/shops.

    Args:
        enable_calling: Boolean to enable or disable calling
        merchant_id: Optional merchant ID filter
        shop_identifier: Optional shop identifier filter
        current_user: Current authenticated user

    Returns:
        Dictionary with status, message, and updated configs

    Raises:
        HTTPException: 404 if no configs found, 500 on error
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) toggling calling to {enable_calling} "
        f"for merchant: {merchant_id}, shop_identifier: {shop_identifier}"
    )

    try:
        updated_configs = await calling_activation_for_merchant(
            enable_calling=enable_calling,
            merchant_id=merchant_id,
            shop_identifier=shop_identifier,
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
