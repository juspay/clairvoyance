"""Business logic handlers for Breeze Buddy data sources."""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.data_source import (
    create_data_source,
    deactivate_data_source,
    get_data_source_by_id,
    list_data_sources,
    update_data_source,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.data_source import (
    CreateDataSourceRequest,
    DataSource,
    UpdateDataSourceRequest,
)
from app.services.data_sources import Discovery, get_adapter


def _has_reseller_access(current_user: UserInfo, reseller_id: str) -> bool:
    return (
        current_user.role == "admin"
        or "*" in current_user.reseller_ids
        or reseller_id in current_user.reseller_ids
    )


def _has_merchant_access(current_user: UserInfo, merchant_id: Optional[str]) -> bool:
    if not merchant_id:
        return True
    return (
        current_user.role == "admin"
        or "*" in current_user.merchant_ids
        or merchant_id in current_user.merchant_ids
    )


def _has_all_merchant_access(current_user: UserInfo) -> bool:
    return current_user.role == "admin" or "*" in current_user.merchant_ids


def _merchant_filter_for_user(current_user: UserInfo) -> Optional[List[str]]:
    """Return merchant IDs to include, or None when unrestricted."""
    if _has_all_merchant_access(current_user):
        return None
    return list(current_user.merchant_ids)


def validate_data_source_access(
    data_source: DataSource,
    current_user: UserInfo,
    operation: str = "access",
) -> None:
    """Validate RBAC access to a data source."""
    if not _has_reseller_access(current_user, data_source.reseller_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {data_source.reseller_id}",
        )

    if not _has_merchant_access(current_user, data_source.merchant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {data_source.merchant_id}",
        )

    logger.debug(
        f"User {current_user.username} authorized to {operation} data source {data_source.id}"
    )


def validate_requested_scope(
    current_user: UserInfo,
    reseller_id: str,
    merchant_id: Optional[str] = None,
) -> None:
    """Validate RBAC access for a requested reseller/merchant scope."""
    if not _has_reseller_access(current_user, reseller_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {reseller_id}",
        )

    if not _has_merchant_access(current_user, merchant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_id}",
        )


async def create_data_source_handler(
    req: CreateDataSourceRequest,
    current_user: UserInfo,
) -> DataSource:
    """Create a data source."""
    validate_requested_scope(current_user, req.reseller_id, req.merchant_id)

    created = await create_data_source(
        reseller_id=req.reseller_id,
        merchant_id=req.merchant_id,
        name=req.name,
        source_type=req.source_type,
        config=req.config,
    )

    if not created:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create data source. Name may already exist in this scope.",
        )

    return created


async def list_data_sources_handler(
    current_user: UserInfo,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    include_inactive: bool = False,
) -> List[DataSource]:
    """List data sources visible to the current user."""
    if reseller_id:
        validate_requested_scope(current_user, reseller_id, merchant_id)
        merchant_ids = None
        if merchant_id is None:
            merchant_ids = _merchant_filter_for_user(current_user)
        return await list_data_sources(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            merchant_ids=merchant_ids,
            include_inactive=include_inactive,
        )

    if merchant_id and not _has_merchant_access(current_user, merchant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_id}",
        )

    if current_user.role == "admin" or (
        "*" in current_user.reseller_ids and _has_all_merchant_access(current_user)
    ):
        return await list_data_sources(
            merchant_id=merchant_id,
            include_inactive=include_inactive,
        )

    if "*" in current_user.reseller_ids:
        return await list_data_sources(
            merchant_id=merchant_id,
            merchant_ids=(
                None
                if merchant_id is not None
                else _merchant_filter_for_user(current_user)
            ),
            include_inactive=include_inactive,
        )

    return await list_data_sources(
        reseller_ids=list(current_user.reseller_ids),
        merchant_id=merchant_id,
        merchant_ids=(
            None if merchant_id is not None else _merchant_filter_for_user(current_user)
        ),
        include_inactive=include_inactive,
    )


async def get_data_source_handler(
    data_source_id: str,
    current_user: UserInfo,
) -> DataSource:
    """Get a data source by ID."""
    data_source = await get_data_source_by_id(data_source_id)
    if not data_source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data source {data_source_id} not found",
        )

    validate_data_source_access(data_source, current_user)
    return data_source


async def discover_data_source_handler(
    data_source_id: str,
    current_user: UserInfo,
) -> Discovery:
    """Discover datasets exposed by a data source."""
    data_source = await get_data_source_handler(data_source_id, current_user)
    try:
        adapter = get_adapter(data_source.source_type.value)
        return await adapter.discover(data_source.config)
    except Exception as exc:
        logger.error(
            f"Error discovering data source {data_source_id}: {exc}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to discover data source. Check its configuration and credentials.",
        ) from exc


async def update_data_source_handler(
    data_source_id: str,
    req: UpdateDataSourceRequest,
    current_user: UserInfo,
) -> DataSource:
    """Update a data source."""
    existing = await get_data_source_handler(data_source_id, current_user)
    validate_requested_scope(current_user, existing.reseller_id, req.merchant_id)

    updated = await update_data_source(
        data_source_id=data_source_id,
        name=req.name,
        merchant_id=req.merchant_id,
        config=req.config,
        is_active=req.is_active,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to update data source.",
        )

    validate_data_source_access(updated, current_user, operation="update")
    return updated


async def delete_data_source_handler(
    data_source_id: str,
    current_user: UserInfo,
) -> None:
    """Soft-delete a data source."""
    await get_data_source_handler(data_source_id, current_user)
    deleted = await deactivate_data_source(data_source_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data source {data_source_id} not found",
        )
