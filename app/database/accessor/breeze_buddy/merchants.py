"""
Database accessor functions for merchant entity management.

This module provides business logic for merchant operations,
using queries from queries.breeze_buddy.merchants and
decoders from decoder.breeze_buddy.merchants.
"""

from typing import List, Optional, Tuple

from app.core.logger import logger
from app.database.decoder.breeze_buddy.merchants import decode_merchant
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.merchants import (
    check_merchant_identifier_exists_query,
    create_merchant_query,
    delete_merchant_query,
    get_all_merchants_query,
    get_merchant_by_merchant_identifier_query,
    get_merchants_by_ids_query,
    get_merchants_by_reseller_query,
    update_merchant_query,
)
from app.schemas.breeze_buddy.merchants import MerchantResponse


async def check_merchant_identifier_exists(merchant_identifier: str) -> bool:
    """Check if merchant_identifier already exists in merchants table.

    Args:
        merchant_identifier: The merchant identifier to check

    Returns:
        True if exists, False otherwise
    """
    query, values = check_merchant_identifier_exists_query(merchant_identifier)

    try:
        result = await run_parameterized_query(query, values)
        return result is not None and len(result) > 0
    except Exception as e:
        logger.error(f"Error checking merchant_identifier existence: {e}")
        raise


async def create_merchant(
    merchant_identifier: str,
    reseller_id: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: bool = True,
) -> Optional[MerchantResponse]:
    """Create a new merchant entity (business entity).

    Args:
        merchant_identifier: Unique business identifier (e.g., "redbus")
        reseller_id: Reseller user ID who owns this merchant (nullable)
        name: Optional display name
        description: Optional description
        is_active: Active status (default: true)

    Returns:
        MerchantResponse if successful, None otherwise
    """
    query, values = create_merchant_query(
        merchant_identifier=merchant_identifier,
        reseller_id=reseller_id,
        name=name,
        description=description,
        is_active=is_active,
    )

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None

        if row:
            logger.info(f"Created merchant entity: {merchant_identifier}")
            return decode_merchant(row)

        return None
    except Exception as e:
        logger.error(f"Error creating merchant entity {merchant_identifier}: {e}")
        raise


async def get_merchant_by_merchant_identifier(
    merchant_identifier: str,
) -> Optional[MerchantResponse]:
    """Get merchant entity by merchant_identifier.

    Args:
        merchant_identifier: The merchant identifier to look up

    Returns:
        MerchantResponse if found, None otherwise
    """
    query, values = get_merchant_by_merchant_identifier_query(merchant_identifier)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None

        if row:
            return decode_merchant(row)

        return None
    except Exception as e:
        logger.error(f"Error fetching merchant entity {merchant_identifier}: {e}")
        raise


async def get_all_merchants(
    page: int = 1,
    limit: int = 50,
    merchant_identifier_filter: Optional[str] = None,
    name_filter: Optional[str] = None,
    reseller_id_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[List[MerchantResponse], int]:
    """Get all merchant entities with pagination and filtering.

    Args:
        page: Page number (1-indexed)
        limit: Items per page
        merchant_identifier_filter: Filter by merchant_identifier (partial match)
        name_filter: Filter by name (partial match, case-insensitive)
        reseller_id_filter: Filter by reseller_id (exact match)
        is_active_filter: Filter by active status
        sort_by: Field to sort by (merchant_identifier, name, created_at, updated_at)
        sort_order: Sort direction (asc, desc)

    Returns:
        Tuple of (list of merchants, total count)
    """
    query, count_query, values = get_all_merchants_query(
        page=page,
        limit=limit,
        merchant_identifier_filter=merchant_identifier_filter,
        name_filter=name_filter,
        reseller_id_filter=reseller_id_filter,
        is_active_filter=is_active_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    try:
        rows = await run_parameterized_query(query, values)
        count_result = await run_parameterized_query(
            count_query, values[:-2] if len(values) > 2 else []
        )
        count_row = count_result[0] if count_result else None

        merchants = [decode_merchant(row) for row in rows] if rows else []
        total = count_row["total"] if count_row else 0

        return merchants, total
    except Exception as e:
        logger.error(f"Error fetching merchant entities: {e}")
        raise


async def get_merchants_by_ids(
    merchant_ids: List[str],
    page: int = 1,
    limit: int = 50,
    merchant_identifier_filter: Optional[str] = None,
    name_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[List[MerchantResponse], int]:
    """Get merchant entities by a list of merchant identifiers with pagination.

    Args:
        merchant_ids: List of merchant identifiers to filter by
        page: Page number (1-indexed)
        limit: Items per page
        merchant_identifier_filter: Additional filter by merchant_identifier (partial match)
        name_filter: Filter by name (partial match, case-insensitive)
        is_active_filter: Filter by active status
        sort_by: Field to sort by (merchant_identifier, name, created_at, updated_at)
        sort_order: Sort direction (asc, desc)

    Returns:
        Tuple of (list of merchants, total count)
    """
    if not merchant_ids:
        return [], 0

    query, count_query, values = get_merchants_by_ids_query(
        merchant_ids=merchant_ids,
        page=page,
        limit=limit,
        merchant_identifier_filter=merchant_identifier_filter,
        name_filter=name_filter,
        is_active_filter=is_active_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    try:
        rows = await run_parameterized_query(query, values)
        count_result = await run_parameterized_query(count_query, values[:-2])
        count_row = count_result[0] if count_result else None

        merchants = [decode_merchant(row) for row in rows] if rows else []
        total = count_row["total"] if count_row else 0

        return merchants, total
    except Exception as e:
        logger.error(f"Error fetching merchant entities by IDs: {e}")
        raise


async def get_merchants_by_reseller(
    reseller_id: str,
    page: int = 1,
    limit: int = 50,
) -> Tuple[List[MerchantResponse], int]:
    """Get all merchant entities owned by a specific reseller.

    Args:
        reseller_id: The reseller's user ID
        page: Page number (1-indexed)
        limit: Items per page

    Returns:
        Tuple of (list of merchants, total count)
    """
    query, count_query, values = get_merchants_by_reseller_query(
        reseller_id=reseller_id,
        page=page,
        limit=limit,
    )

    try:
        rows = await run_parameterized_query(query, values)
        count_result = await run_parameterized_query(count_query, values[:-2])
        count_row = count_result[0] if count_result else None

        merchants = [decode_merchant(row) for row in rows] if rows else []
        total = count_row["total"] if count_row else 0

        return merchants, total
    except Exception as e:
        logger.error(
            f"Error fetching merchant entities for reseller {reseller_id}: {e}"
        )
        raise


async def update_merchant(
    merchant_identifier: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
    reseller_id: Optional[str] = None,
) -> Optional[MerchantResponse]:
    """Update merchant entity (merchant_identifier cannot be changed).

    Args:
        merchant_identifier: The merchant identifier to update
        name: New display name
        description: New description
        is_active: New active status
        reseller_id: New reseller owner ID

    Returns:
        MerchantResponse if successful, None if not found
    """
    query, values = update_merchant_query(
        merchant_identifier=merchant_identifier,
        name=name,
        description=description,
        is_active=is_active,
        reseller_id=reseller_id,
    )

    if not values:
        # No fields to update, return current merchant
        return await get_merchant_by_merchant_identifier(merchant_identifier)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None

        if row:
            logger.info(f"Updated merchant entity: {merchant_identifier}")
            return decode_merchant(row)

        return None
    except Exception as e:
        logger.error(f"Error updating merchant entity {merchant_identifier}: {e}")
        raise


async def delete_merchant(merchant_identifier: str) -> bool:
    """Delete merchant entity by merchant_identifier.

    Returns True if deleted, False if not found.
    Raises ValueError if users still reference this merchant_identifier.
    Raises exception on database errors.

    Note: Uses atomic CTE to check dependencies and delete in a single
    operation to prevent TOCTOU race conditions.

    Args:
        merchant_identifier: The merchant identifier to delete

    Returns:
        True if deleted, False if not found
    """
    query, values = delete_merchant_query(merchant_identifier)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None

        if row:
            user_count = row["user_count"]
            deleted_count = row["deleted_count"]

            if user_count > 0:
                raise ValueError(
                    f"Cannot delete merchant entity '{merchant_identifier}': {user_count} user(s) still reference it"
                )

            if deleted_count > 0:
                logger.info(f"Deleted merchant entity: {merchant_identifier}")
                return True

            return False

        return False
    except Exception as e:
        logger.error(f"Error deleting merchant entity {merchant_identifier}: {e}")
        raise
