"""
Modern RESTful configuration management endpoints with RBAC.

This module provides clean REST API endpoints for managing call execution configurations.
All endpoints support RBAC and work with any template type (template-agnostic design).

Endpoints:
- POST   /configurations           - Create new configuration
- GET    /configurations           - List all configurations (with filters)
- GET    /configurations/{id}      - Get single configuration by ID
- PUT    /configurations/{id}      - Update configuration
- DELETE /configurations/{id}      - Delete configuration

For backward compatibility, old endpoints are available in deprecated/call_execution_config.py
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.database.accessor import get_template_by_id
from app.schemas import (
    CallExecutionConfig,
    CreateCallExecutionConfigRequest,
    UpdateCallExecutionConfigRequest,
    UserInfo,
)

from .handlers import (
    calling_activation_handler,
    create_configuration_handler,
    delete_configuration_handler,
    get_configuration_handler,
    list_configurations_handler,
    update_configuration_handler,
)
from .rbac import filter_configs_by_rbac, validate_config_access

router = APIRouter()


async def _validate_template_access(current_user: UserInfo, template_id: str) -> None:
    """Helper to validate user has access to template via RBAC."""
    template = await get_template_by_id(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template {template_id} not found",
        )

    validate_config_access(
        current_user,
        template.merchant_id,
        template.shop_identifier,
        operation="create configuration for",
    )


@router.post(
    "/configurations",
    response_model=CallExecutionConfig,
    status_code=status.HTTP_201_CREATED,
)
async def create_configuration(
    config: CreateCallExecutionConfigRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Create a new call execution configuration.

    This endpoint allows creating configurations for any template type.
    Works with order-confirmation, appointment-reminder, or any custom template.

    Permissions:
    - Admin: Can create configurations for any merchant/shop
    - Merchant: Can create configurations for own shops only

    Request Body:
        {
            "template_id": "uuid-of-template",
            "initial_offset": 0,
            "retry_offset": 300,
            "call_start_time": "09:00",
            "call_end_time": "21:00",
            "max_retry": 3,
            "calling_provider": "EXOTEL",
            "enable_international_call": false
        }

    Returns:
        Created configuration object with generated ID
    """
    # RBAC: Check if user has permission to create config for this template's merchant/shop
    await _validate_template_access(current_user, config.template_id)

    return await create_configuration_handler(config, current_user)


@router.get("/configurations", response_model=List[CallExecutionConfig])
async def list_configurations(
    template_id: Optional[str] = Query(None, description="Filter by template ID"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all call execution configurations with optional filters.

    Query Parameters:
    - template_id: Filter configurations by template ID

    RBAC Filtering:
    - Admin: Sees all configurations (or filtered by query params)
    - Merchant: Sees only configurations for accessible merchants/shops

    Example Requests:
        GET /configurations                                    # All accessible configs
        GET /configurations?template_id=uuid-of-template       # Filter by template

    Returns:
        List of configuration objects matching filters and user permissions
    """
    # Validate template access if filter provided
    if template_id and current_user.role != "admin":
        await _validate_template_access(current_user, template_id)

    # Get configurations
    configs = await list_configurations_handler(template_id, current_user)

    return configs


@router.get("/configurations/{config_id}", response_model=CallExecutionConfig)
async def get_configuration(
    config_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get a single configuration by ID.

    Path Parameters:
    - config_id: Configuration UUID

    RBAC:
    - Admin: Can access any configuration
    - Merchant: Can only access configurations for own merchants/shops

    Returns:
        Configuration object if found and user has access
        404 if not found or access denied
    """
    # Get configuration
    config = await get_configuration_handler(config_id, current_user)

    # RBAC: Check access via template (return 404 to avoid leaking existence)
    if config.template_id:
        try:
            await _validate_template_access(current_user, config.template_id)
        except HTTPException:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Configuration {config_id} not found",
            )

    return config


@router.put("/configurations/{config_id}", response_model=CallExecutionConfig)
async def update_configuration(
    config_id: str,
    config: UpdateCallExecutionConfigRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update an existing configuration.

    Path Parameters:
    - config_id: Configuration UUID to update

    Permissions:
    - Admin: Can update any configuration
    - Merchant: Can update configurations for own merchants/shops

    Note: RBAC validation is performed against the existing configuration,
    not the request body values, to prevent unauthorized access.

    Request Body:
        {
            "merchant_id": "shop_123",
            "template": "order-confirmation",
            "shop_identifier": "shop_123",
            "initial_offset": 0,
            "retry_offset": 600,
            "call_start_time": "10:00",
            "call_end_time": "20:00",
            "max_retry": 5,
            "calling_provider": "TWILIO",
            "enable_international_call": true
        }

    Returns:
        Updated configuration object
    """
    # RBAC validation is performed in the handler against the existing configuration
    return await update_configuration_handler(config_id, config, current_user)


@router.delete("/configurations/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_configuration(
    config_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete a configuration by ID.

    Path Parameters:
    - config_id: Configuration UUID to delete

    Permissions:
    - Admin: Can delete any configuration
    - Merchant: Cannot delete configurations

    Returns:
        204 No Content on successful deletion
        404 if configuration not found
        403 if user lacks permission
    """
    # RBAC: Only admins can delete configurations
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can delete configurations",
        )

    await delete_configuration_handler(config_id, current_user)
    return None  # 204 No Content


@router.patch("/configurations/calling/activation")
async def calling_activation(
    enable_calling: bool,
    merchant_id: Optional[str] = None,
    shop_identifier: Optional[str] = None,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Enable or disable calling globally or for specific merchants/shops.

    Query Parameters:
    - enable_calling: Boolean to enable or disable calling
    - merchant_id: Optional merchant ID filter
    - shop_identifier: Optional shop identifier filter

    Behavior:
    - If merchant_id is None: All configs across all merchants are updated (admin only)
    - If merchant_id is provided but shop_identifier is None: All configs for that merchant are updated
    - If both merchant_id and shop_identifier are provided: Only that specific config is updated

    Permissions:
    - Admin: Can toggle calling for any merchant/shop or globally
    - Merchant: Can only toggle calling for own merchants/shops

    Example Requests:
        PATCH /configurations/toggle-calling?enable_calling=false                           # Global disable (admin only)
        PATCH /configurations/toggle-calling?enable_calling=true&merchant_id=shop_123       # Enable for merchant
        PATCH /configurations/toggle-calling?enable_calling=false&merchant_id=shop_123&shop_identifier=shop_456  # Disable for specific shop

    Returns:
        {
            "status": "success",
            "message": "Updated N config(s)",
            "configs": [...]
        }
    """
    # RBAC: Check permissions
    if merchant_id is None:
        # Global toggle - only admins allowed
        if current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin users can toggle calling globally",
            )
    else:
        # Merchant-specific toggle - validate access
        validate_config_access(
            current_user, merchant_id, shop_identifier, operation="toggle calling for"
        )

    result = await calling_activation_handler(
        enable_calling=enable_calling,
        merchant_id=merchant_id,
        shop_identifier=shop_identifier,
        current_user=current_user,
    )

    # Apply RBAC filtering to returned configs
    result["configs"] = await filter_configs_by_rbac(result["configs"], current_user)
    result["message"] = f"Updated {len(result['configs'])} config(s)"

    return result
