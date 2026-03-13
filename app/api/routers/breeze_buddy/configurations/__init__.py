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
    - Admin: Can create configurations for any reseller/shop
    - Reseller: Can create configurations for own shops only

    Request Body:
        {
            "reseller_id": "shop_123",
            "template": "order-confirmation",
            "merchant_identifier": "merchant_123",
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
    # Support both new (reseller_id, merchant_identifier) and old (merchant_id, shop_identifier) field names
    reseller_id = config.reseller_id
    merchant_identifier = config.merchant_identifier

    # RBAC: Check if user has permission to create config for this reseller/shop
    validate_config_access(
        current_user,
        reseller_id,
        merchant_identifier,
        operation="create configuration for",
    )

    return await create_configuration_handler(config, current_user)


@router.get("/configurations", response_model=List[CallExecutionConfig])
async def list_configurations(
    reseller_id: Optional[str] = Query(None, description="Filter by reseller ID"),
    template: Optional[str] = Query(
        None, description="Filter by template name (e.g., 'order-confirmation')"
    ),
    merchant_identifier: Optional[str] = Query(
        None, description="Filter by merchant identifier"
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all call execution configurations with optional filters.

    Query Parameters:
    - reseller_id: Filter configurations by reseller (or merchant_id for backward compatibility)
    - template: Filter by template name (order-confirmation, appointment-reminder, etc.)
    - merchant_identifier: Filter by specific shop (or shop_identifier for backward compatibility)

    RBAC Filtering:
    - Admin: Sees all configurations (or filtered by query params)
    - Merchant: Sees only configurations for accessible merchants/shops

    Example Requests:
        GET /configurations                                    # All accessible configs
        GET /configurations?template=order-confirmation        # Filter by template
        GET /configurations?reseller_id=shop_123               # Filter by reseller
        GET /configurations?merchant_identifier=shop_456       # Filter by shop

    Returns:
        List of configuration objects matching filters and user permissions
    """

    # Validate reseller access if filter provided
    if reseller_id and current_user.role != "admin":
        if (
            reseller_id not in current_user.reseller_ids
            and "*" not in current_user.reseller_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to reseller {reseller_id}",
            )
    # Get configurations
    configs = await list_configurations_handler(
        reseller_id, template, merchant_identifier, current_user
    )

    # Apply RBAC filtering
    configs = filter_configs_by_rbac(configs, current_user)

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

    # RBAC: Check access (return 404 to avoid leaking existence)
    try:
        validate_config_access(
            current_user,
            config.reseller_id,
            config.merchant_identifier,
            operation="access configuration for",
        )
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
            "reseller_id": "shop_123",
            "template": "order-confirmation",
            "merchant_identifier": "shop_123",
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
    reseller_id: Optional[str] = Query(None, description="Filter by reseller ID"),
    merchant_identifier: Optional[str] = Query(
        None, description="Filter by merchant identifier"
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Enable or disable calling globally or for specific resellers/shops.

    Query Parameters:
    - enable_calling: Boolean to enable or disable calling
    - reseller_id: Optional reseller ID filter (or merchant_id for backward compatibility)
    - merchant_identifier: Optional shop identifier filter (or shop_identifier for backward compatibility)

    Behavior:
    - If reseller_id is None: All configs across all resellers are updated (admin only)
    - If reseller_id is provided but merchant_identifier is None: All configs for that reseller are updated
    - If both reseller_id and merchant_identifier are provided: Only that specific config is updated

    Permissions:
    - Admin: Can toggle calling for any merchant/shop or globally
    - Merchant: Can only toggle calling for own merchants/shops

    Example Requests:
        PATCH /configurations/toggle-calling?enable_calling=false                           # Global disable (admin only)
        PATCH /configurations/toggle-calling?enable_calling=true&reseller_id=shop_123       # Enable for reseller
        PATCH /configurations/toggle-calling?enable_calling=false&reseller_id=shop_123&merchant_identifier=shop_456  # Disable for specific shop

    Returns:
        {
            "status": "success",
            "message": "Updated N config(s)",
            "configs": [...]
        }
    """

    # RBAC: Check permissions
    if reseller_id is None:
        # Global toggle - only admins allowed
        if current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin users can toggle calling globally",
            )
    else:
        # Reseller-specific toggle - validate access
        validate_config_access(
            current_user,
            reseller_id,
            merchant_identifier,
            operation="toggle calling for",
        )

    result = await calling_activation_handler(
        enable_calling=enable_calling,
        reseller_id=reseller_id,
        merchant_identifier=merchant_identifier,
        current_user=current_user,
    )

    # Apply RBAC filtering to returned configs
    result["configs"] = filter_configs_by_rbac(result["configs"], current_user)
    result["message"] = f"Updated {len(result['configs'])} config(s)"

    return result
