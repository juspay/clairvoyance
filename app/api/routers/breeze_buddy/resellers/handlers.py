"""
Handlers for reseller (umbrella) entity endpoints.

A reseller entity is the umbrella itself; a users row (role='reseller') with
the same id is an optional login for it. Entity management is admin-first:

- Admin: full CRUD
- Reseller: sees / renames their own umbrella
- Merchant/User: read-only visibility of umbrellas they hold grants on
  (display labels for the console)
"""

from typing import Optional

import asyncpg
from fastapi import HTTPException

from app.core.logger import logger
from app.core.security.scope import resolve_reseller_ids
from app.database.accessor.breeze_buddy import resellers as reseller_accessors
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.resellers import (
    ResellerCreate,
    ResellerListResponse,
    ResellerResponse,
    ResellerUpdate,
)
from app.schemas.breeze_buddy.users import DeleteUserResponse


async def _allowed_reseller_scope(current_user: UserInfo) -> Optional[list]:
    """Resolve which umbrellas the caller may see (None = unrestricted)."""
    if current_user.role == UserRole.ADMIN:
        return None
    return await resolve_reseller_ids(current_user)


async def get_all_resellers_handler(
    page: int,
    limit: int,
    search: Optional[str],
    is_active_filter: Optional[bool],
    sort_by: str,
    sort_order: str,
    current_user: UserInfo,
) -> ResellerListResponse:
    """List reseller entities, scoped to the caller's umbrella access."""
    allowed = await _allowed_reseller_scope(current_user)

    try:
        resellers, total = await reseller_accessors.get_all_resellers(
            page=page,
            limit=limit,
            id_or_name_filter=search,
            is_active_filter=is_active_filter,
            allowed_reseller_ids=allowed,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        total_pages = (total + limit - 1) // limit if total > 0 else 0
        return ResellerListResponse(
            resellers=resellers,
            total=total,
            page=page,
            limit=limit,
            total_pages=total_pages,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching resellers: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_reseller_by_id_handler(
    reseller_id: str, current_user: UserInfo
) -> ResellerResponse:
    """Get one reseller entity the caller has umbrella access to."""
    allowed = await _allowed_reseller_scope(current_user)
    if allowed is not None and reseller_id not in allowed:
        raise HTTPException(
            status_code=403, detail="You don't have access to this reseller"
        )

    try:
        reseller = await reseller_accessors.get_reseller_by_id(reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        return reseller
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching reseller {reseller_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def create_reseller_handler(
    reseller_data: ResellerCreate, current_user: UserInfo
) -> ResellerResponse:
    """Create a reseller entity (admin only; no login is created)."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403, detail="Only admins can create reseller entities"
        )

    try:
        reseller = await reseller_accessors.create_reseller(
            id=reseller_data.id,
            name=reseller_data.name,
            description=reseller_data.description,
            is_active=reseller_data.is_active,
        )
        if not reseller:
            raise HTTPException(status_code=500, detail="Failed to create reseller")
        return reseller
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"Reseller '{reseller_data.id}' already exists",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating reseller {reseller_data.id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def update_reseller_handler(
    reseller_id: str,
    reseller_data: ResellerUpdate,
    current_user: UserInfo,
) -> ResellerResponse:
    """Update a reseller entity.

    - Admin: any field
    - Reseller: own umbrella only, name/description only
    """
    if current_user.role == UserRole.RESELLER:
        if reseller_id != current_user.id:
            raise HTTPException(
                status_code=403, detail="You can only modify your own umbrella"
            )
        if reseller_data.is_active is not None:
            raise HTTPException(
                status_code=403, detail="Only admins can change active status"
            )
    elif current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Only admins and resellers can modify reseller entities",
        )

    try:
        updated = await reseller_accessors.update_reseller(
            reseller_id=reseller_id,
            name=reseller_data.name,
            description=reseller_data.description,
            is_active=reseller_data.is_active,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Reseller not found")
        return updated
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating reseller {reseller_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def delete_reseller_handler(
    reseller_id: str, current_user: UserInfo
) -> DeleteUserResponse:
    """Delete a reseller entity (admin only).

    Refused with 409 while merchants still reference the umbrella (FK).
    Grant rows cascade; a same-id login row is untouched and reported.
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403, detail="Only admins can delete reseller entities"
        )

    try:
        deleted = await reseller_accessors.delete_reseller(reseller_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Reseller not found")
        return DeleteUserResponse(
            success=True,
            message=f"Reseller '{reseller_id}' deleted",
            deleted_id=reseller_id,
        )
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete reseller '{reseller_id}': workspaces still "
            "belong to it. Reassign or delete them first.",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting reseller {reseller_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
