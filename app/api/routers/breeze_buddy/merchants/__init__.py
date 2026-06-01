"""
Merchants API endpoints.

Admin-only endpoints for managing and viewing merchants.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.merchants import (
    MerchantCreate,
    MerchantListResponse,
    MerchantResponse,
    MerchantUpdate,
)
from app.schemas.breeze_buddy.users import DeleteUserResponse

from .handlers import (
    create_merchant_handler,
    delete_merchant_handler,
    get_all_merchants_handler,
    get_merchant_by_merchant_identifier_handler,
    update_merchant_handler,
)

router = APIRouter()


@router.post("/merchant", response_model=MerchantResponse)
async def create_merchant(
    merchant_data: MerchantCreate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Create a new merchant entity.

    **RBAC rules:**
    - Admin: Can create merchant entities and optionally assign to a reseller
    - Reseller: Can create merchant entities, auto-assigned to themselves

    Args:
        merchant_data: Merchant entity creation data

    Returns:
        Created merchant entity

    Raises:
        403: If user doesn't have permission to create
        409: If merchant_id already exists
        500: If there's an error creating the merchant
    """
    return await create_merchant_handler(merchant_data, current_user)


@router.get("/merchants", response_model=MerchantListResponse)
async def list_merchants(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    merchant_id: Optional[str] = Query(
        None, description="Filter by merchant_id (partial match)"
    ),
    name: Optional[str] = Query(None, description="Filter by name (partial match)"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    sort_by: str = Query(
        "created_at",
        description="Sort by field",
        pattern="^(merchant_id|name|created_at|updated_at)$",
    ),
    sort_order: str = Query("desc", description="Sort order", pattern="^(asc|desc)$"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all merchant entities with pagination and filtering.

    **RBAC rules:**
    - Admin: See all merchant entities
    - Reseller/Merchant/User: See only merchant entities in their merchant_ids scope

    Args:
        page: Page number (1-indexed)
        limit: Items per page (1-100)
        merchant_id: Filter by merchant_id (partial match, case-insensitive)
        name: Filter by name (partial match, case-insensitive)
        is_active: Filter by active status
        sort_by: Sort field (merchant_id, name, created_at, updated_at)
        sort_order: Sort direction (asc, desc)

    Returns:
        Paginated list of merchant entities

    Raises:
        403: If user doesn't have access
        500: If there's an error retrieving merchants
    """
    return await get_all_merchants_handler(
        page=page,
        limit=limit,
        merchant_identifier_filter=merchant_id,
        is_active_filter=is_active,
        name_filter=name,
        sort_by=sort_by,
        sort_order=sort_order,
        current_user=current_user,
    )


@router.get("/merchant/{merchant_id}", response_model=MerchantResponse)
async def get_merchant_by_id(
    merchant_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get merchant entity by merchant_id.

    **RBAC rules:**
    - Admin: Can view any merchant entity
    - Reseller/Merchant/User: Can view only merchant entities in their scope

    Args:
        merchant_id: Business identifier (e.g., "redbus", "nvidia")

    Returns:
        Merchant entity details

    Raises:
        403: If user doesn't have access
        404: If merchant entity not found
        500: If there's an error retrieving the merchant
    """
    return await get_merchant_by_merchant_identifier_handler(merchant_id, current_user)


@router.put("/merchant/{merchant_id}", response_model=MerchantResponse)
async def update_merchant(
    merchant_id: str,
    merchant_data: MerchantUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update merchant entity by merchant_id.

    **RBAC rules:**
    - Admin: Can update any merchant entity
    - Reseller: Can only update merchant entities they own (reseller_id match)

    Updates merchant entity fields (name, description, is_active, reseller_id).
    Note: merchant_id cannot be changed after creation.

    Args:
        merchant_id: Business identifier (e.g., "redbus", "nvidia")
        merchant_data: Fields to update

    Returns:
        Updated merchant entity

    Raises:
        403: If user doesn't have permission to update
        404: If merchant entity not found
        500: If there's an error updating the merchant
    """
    return await update_merchant_handler(merchant_id, merchant_data, current_user)


@router.delete("/merchant/{merchant_id}", response_model=DeleteUserResponse)
async def delete_merchant(
    merchant_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete merchant entity by merchant_id.

    **RBAC rules:**
    - Admin: Can delete any merchant entity
    - Reseller: Can only delete merchant entities they own (reseller_id match)

    Permanently removes the merchant entity from the database.
    This will NOT delete associated user accounts.

    Args:
        merchant_id: Business identifier (e.g., "redbus", "nvidia")

    Returns:
        Deletion confirmation

    Raises:
        403: If user doesn't have permission to delete
        404: If merchant entity not found
        500: If there's an error deleting the merchant
    """
    return await delete_merchant_handler(merchant_id, current_user)
