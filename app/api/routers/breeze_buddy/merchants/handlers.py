"""
Handlers for merchant endpoints.
"""

from typing import Optional

import asyncpg
from fastapi import HTTPException

from app.core.logger import logger
from app.core.security.scope import resolve_merchant_ids
from app.database.accessor.breeze_buddy import merchants as merchant_accessors
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.merchants import (
    MerchantCreate,
    MerchantListResponse,
    MerchantResponse,
    MerchantUpdate,
)
from app.schemas.breeze_buddy.users import DeleteUserResponse


def _check_create_merchant_access(current_user: UserInfo):
    """Check if user can create merchant entities (admin or reseller)."""
    if current_user.role not in [UserRole.ADMIN, UserRole.RESELLER]:
        raise HTTPException(
            status_code=403,
            detail="Only admins and resellers can create merchant entities",
        )


def _check_update_access(current_user: UserInfo, reseller_id: Optional[str]):
    """Check if user can update merchant entity.

    - Admin: Can update any merchant
    - Reseller: Can only update merchants they own (reseller_id matches)
    """
    if current_user.role == UserRole.ADMIN:
        return  # Admin can update anything

    if current_user.role == UserRole.RESELLER:
        if reseller_id == current_user.id:
            return  # Reseller can update their own merchants
        raise HTTPException(
            status_code=403, detail="You can only modify merchant entities you own"
        )

    raise HTTPException(
        status_code=403, detail="Only admins and resellers can modify merchant entities"
    )


async def _check_merchant_view_access(current_user: UserInfo, merchant_id: str):
    """Check if user can view a specific merchant entity.

    Resolves wildcard through owner chain for proper scoping.
    - Admin: Can view all
    - Reseller with *: Can view only merchants they own (reseller_id = their UUID)
    - Merchant/User with *: Can view owner's scoped merchants
    - Others: Can only view merchants in their merchant_ids scope
    """
    allowed = await resolve_merchant_ids(current_user)
    if allowed is None:
        return  # Unrestricted access (admin, reseller with *)

    if merchant_id not in allowed:
        raise HTTPException(
            status_code=403, detail="You don't have access to this merchant entity"
        )


async def create_merchant_handler(
    merchant_data: MerchantCreate, current_user: UserInfo
) -> MerchantResponse:
    """Create a new merchant entity.

    - Admin: can optionally set reseller_id to assign merchant to a reseller
    - Reseller: reseller_id is auto-set to their own user ID
    """
    _check_create_merchant_access(current_user)

    # Check if merchant_id already exists
    exists = await merchant_accessors.check_merchant_identifier_exists(
        merchant_data.merchant_id
    )
    if exists:
        raise HTTPException(
            status_code=409,
            detail=f"Merchant ID '{merchant_data.merchant_id}' already exists",
        )

    # Determine reseller_id:
    # - Reseller: always auto-set to their own ID (ignore any passed value)
    # - Admin: use provided reseller_id or None
    if current_user.role == UserRole.RESELLER:
        reseller_id = current_user.id
    else:
        # Admin can optionally set reseller_id
        reseller_id = merchant_data.reseller_id

    try:
        merchant = await merchant_accessors.create_merchant(
            merchant_id=merchant_data.merchant_id,
            name=merchant_data.name,
            description=merchant_data.description,
            is_active=(
                merchant_data.is_active if merchant_data.is_active is not None else True
            ),
            reseller_id=reseller_id,
        )

        if not merchant:
            raise HTTPException(
                status_code=500, detail="Failed to create merchant entity"
            )

        return merchant

    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"Merchant ID '{merchant_data.merchant_id}' already exists",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating merchant entity: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_all_merchants_handler(
    page: int,
    limit: int,
    merchant_identifier_filter: Optional[str],
    name_filter: Optional[str],
    is_active_filter: Optional[bool],
    sort_by: str,
    sort_order: str,
    current_user: UserInfo,
) -> MerchantListResponse:
    """Get all merchant entities with pagination and filtering."""
    try:
        # Resolve merchant_ids scope (handles wildcard through owner chain)
        resolved = await resolve_merchant_ids(current_user)

        if resolved is None:
            # Unrestricted access (admin, reseller with wildcard)
            merchants, total = await merchant_accessors.get_all_merchants(
                page=page,
                limit=limit,
                is_active_filter=is_active_filter,
                merchant_identifier_filter=merchant_identifier_filter,
                name_filter=name_filter,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        else:
            # Scoped to resolved merchant_ids
            merchants, total = await merchant_accessors.get_merchants_by_ids(
                resolved,
                page=page,
                limit=limit,
                merchant_identifier_filter=merchant_identifier_filter,
                name_filter=name_filter,
                is_active_filter=is_active_filter,
                sort_by=sort_by,
                sort_order=sort_order,
            )

        total_pages = (total + limit - 1) // limit if total > 0 else 0

        return MerchantListResponse(
            merchants=merchants,
            total=total,
            page=page,
            limit=limit,
            total_pages=total_pages,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching merchant entities: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_merchant_by_merchant_identifier_handler(
    merchant_id: str, current_user: UserInfo
) -> MerchantResponse:
    """Get merchant entity by merchant_id."""
    try:
        # Check view access BEFORE DB fetch to avoid leaking resource existence
        await _check_merchant_view_access(current_user, merchant_id)

        merchant = await merchant_accessors.get_merchant_by_merchant_identifier(
            merchant_id
        )

        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant entity not found")

        return merchant

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching merchant entity {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def update_merchant_handler(
    merchant_id: str,
    merchant_data: MerchantUpdate,
    current_user: UserInfo,
) -> MerchantResponse:
    """Update merchant entity."""
    try:
        # Get existing merchant first
        merchant = await merchant_accessors.get_merchant_by_merchant_identifier(
            merchant_id
        )
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant entity not found")

        # Check update access (admin can update any, reseller only their own)
        _check_update_access(current_user, merchant.reseller_id)

        # Only admin can change reseller_id
        reseller_id = None
        if merchant_data.reseller_id is not None:
            if current_user.role != UserRole.ADMIN:
                raise HTTPException(
                    status_code=403,
                    detail="Only admins can change reseller assignment",
                )
            reseller_id = merchant_data.reseller_id

        updated_merchant = await merchant_accessors.update_merchant(
            merchant_id=merchant_id,
            name=merchant_data.name,
            description=merchant_data.description,
            is_active=merchant_data.is_active,
            reseller_id=reseller_id,
        )

        if not updated_merchant:
            raise HTTPException(status_code=404, detail="Merchant entity not found")

        return updated_merchant

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating merchant entity {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def delete_merchant_handler(
    merchant_id: str, current_user: UserInfo
) -> DeleteUserResponse:
    """Delete merchant entity (admin or reseller who owns it)."""
    # Only admin and reseller can delete
    if current_user.role not in [UserRole.ADMIN, UserRole.RESELLER]:
        raise HTTPException(
            status_code=403,
            detail="Only admin or reseller can delete merchant entities",
        )

    try:
        # Get the merchant first to check ownership
        merchant = await merchant_accessors.get_merchant_by_merchant_identifier(
            merchant_id
        )
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant entity not found")

        # Resellers can only delete merchants they own
        if current_user.role == UserRole.RESELLER:
            if merchant.reseller_id != current_user.id:
                raise HTTPException(
                    status_code=403,
                    detail="You can only delete merchant entities you own",
                )

        deleted = await merchant_accessors.delete_merchant(merchant_id)

        if not deleted:
            raise HTTPException(status_code=404, detail="Merchant entity not found")

        return DeleteUserResponse(
            success=True,
            message=f"Merchant entity '{merchant_id}' deleted",
            deleted_id=merchant_id,
        )

    except ValueError as e:
        # Handle dependent users check failure
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting merchant entity {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
