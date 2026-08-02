"""
Database accessor functions for merchant entity management.

This module provides business logic for merchant operations,
using queries from queries.breeze_buddy.merchants and
decoders from decoder.breeze_buddy.merchants.
"""

from typing import List, Optional, Tuple

from app.core.logger import logger
from app.database import get_db_connection
from app.database.accessor.breeze_buddy.access_grants import ensure_reseller_on_conn
from app.database.accessor.breeze_buddy.wallets import (
    create_wallet_on_conn,
    delete_wallet_on_conn,
    get_wallet_for_update_on_conn,
    update_wallet_reseller_id_on_conn,
)
from app.database.decoder.breeze_buddy.merchants import decode_merchant
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.merchants import (
    check_merchant_identifier_exists_query,
    create_merchant_query,
    delete_merchant_query,
    get_all_merchants_query,
    get_merchant_by_merchant_identifier_query,
    get_merchant_s2s_token_query,
    get_merchants_by_ids_query,
    get_merchants_by_reseller_query,
    set_merchant_s2s_token_query,
    update_merchant_query,
)
from app.schemas.breeze_buddy.merchants import MerchantResponse


async def check_merchant_identifier_exists(merchant_id: str) -> bool:
    """Check if merchant_id already exists in merchants table.

    Args:
        merchant_id: The merchant identifier to check

    Returns:
        True if exists, False otherwise
    """
    query, values = check_merchant_identifier_exists_query(merchant_id)

    try:
        result = await run_parameterized_query(query, values)
        return result is not None and len(result) > 0
    except Exception as e:
        logger.error(f"Error checking merchant_id existence: {e}")
        raise


async def create_merchant(
    merchant_id: str,
    reseller_id: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: bool = True,
) -> Optional[MerchantResponse]:
    """Create a new merchant entity (business entity).

    Args:
        merchant_id: Unique business identifier (e.g., "redbus")
        reseller_id: Reseller user ID who owns this merchant (nullable)
        name: Optional display name
        description: Optional description
        is_active: Active status (default: true)

    Returns:
        MerchantResponse if successful, None otherwise
    """
    query, values = create_merchant_query(
        merchant_id=merchant_id,
        reseller_id=reseller_id,
        name=name,
        description=description,
        is_active=is_active,
    )

    try:
        async for conn in get_db_connection():
            async with conn.transaction():
                if reseller_id:
                    # merchants.reseller_id carries an FK to resellers; keep
                    # legacy callers working by materializing unknown umbrella
                    # slugs as bare resellers rows.
                    await ensure_reseller_on_conn(conn, reseller_id)
                result = await conn.fetch(query, *values)
                if result:
                    # Every merchant must have exactly one wallet row; create
                    # it in the same transaction so it can never be missing.
                    await create_wallet_on_conn(conn, merchant_id, reseller_id)
            row = result[0] if result else None

            if row:
                logger.info(f"Created merchant entity: {merchant_id}")
                return decode_merchant(row)

            return None
        return None
    except Exception as e:
        logger.error(f"Error creating merchant entity {merchant_id}: {e}")
        raise


async def get_merchant_by_merchant_identifier(
    merchant_id: str,
) -> Optional[MerchantResponse]:
    """Get merchant entity by merchant_id.

    Args:
        merchant_id: The merchant identifier to look up

    Returns:
        MerchantResponse if found, None otherwise
    """
    query, values = get_merchant_by_merchant_identifier_query(merchant_id)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None

        if row:
            return decode_merchant(row)

        return None
    except Exception as e:
        logger.error(f"Error fetching merchant entity {merchant_id}: {e}")
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
        merchant_identifier_filter: Filter by merchant_id (partial match)
        name_filter: Filter by name (partial match, case-insensitive)
        reseller_id_filter: Filter by reseller_id (exact match)
        is_active_filter: Filter by active status
        sort_by: Field to sort by (merchant_id, name, created_at, updated_at)
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
    reseller_id_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[List[MerchantResponse], int]:
    """Get merchant entities by a list of merchant identifiers with pagination.

    Args:
        merchant_ids: List of merchant identifiers to filter by
        page: Page number (1-indexed)
        limit: Items per page
        merchant_identifier_filter: Additional filter by merchant_id (partial match)
        name_filter: Filter by name (partial match, case-insensitive)
        reseller_id_filter: Filter by owning reseller (exact match)
        is_active_filter: Filter by active status
        sort_by: Field to sort by (merchant_id, name, created_at, updated_at)
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
        reseller_id_filter=reseller_id_filter,
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
    merchant_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
    reseller_id: Optional[str] = None,
) -> Optional[MerchantResponse]:
    """Update merchant entity (merchant_id cannot be changed).

    Args:
        merchant_id: The merchant identifier to update
        name: New display name
        description: New description
        is_active: New active status
        reseller_id: New reseller owner ID

    Returns:
        MerchantResponse if successful, None if not found
    """
    query, values = update_merchant_query(
        merchant_id=merchant_id,
        name=name,
        description=description,
        is_active=is_active,
        reseller_id=reseller_id,
    )

    if not values:
        # No fields to update, return current merchant
        return await get_merchant_by_merchant_identifier(merchant_id)

    try:
        async for conn in get_db_connection():
            async with conn.transaction():
                if reseller_id:
                    # Reassignment must satisfy the resellers FK; materialize
                    # unknown umbrella slugs like create_merchant does.
                    await ensure_reseller_on_conn(conn, reseller_id)
                result = await conn.fetch(query, *values)
                if result and reseller_id:
                    # Keep the wallet's denormalized reseller_id in sync so
                    # reseller-scoped wallet queries stay accurate after
                    # a merchant is reassigned to a different reseller.
                    await update_wallet_reseller_id_on_conn(
                        conn, merchant_id, reseller_id
                    )
            row = result[0] if result else None

            if row:
                logger.info(f"Updated merchant entity: {merchant_id}")
                return decode_merchant(row)

            return None
        return None
    except Exception as e:
        logger.error(f"Error updating merchant entity {merchant_id}: {e}")
        raise


async def delete_merchant(merchant_id: str) -> bool:
    """Delete merchant entity by merchant_id.

    Returns True if deleted, False if not found.
    Raises ValueError if users still reference this merchant_id, or if the
    merchant's wallet has a non-zero balance.
    Raises exception on database errors.

    Wallet handling: the wallet row is locked and checked in the same
    transaction as the merchant delete. If balance_credits > 0, deletion is
    refused. If balance_credits == 0, the wallet row is deleted alongside
    the merchant. wallet_transactions rows are NEVER touched -- the ledger
    has no FK to wallets/merchants and is preserved permanently as an
    append-only audit record, regardless of merchant/wallet lifecycle.

    Note: the merchant delete itself still uses the existing atomic CTE
    (dependency_check + delete_operation) to check user references and
    delete in one round trip, avoiding TOCTOU races.

    Args:
        merchant_id: The merchant identifier to delete

    Returns:
        True if deleted, False if not found
    """
    query, values = delete_merchant_query(merchant_id)

    try:
        async for conn in get_db_connection():
            async with conn.transaction():
                wallet = await get_wallet_for_update_on_conn(conn, merchant_id)
                if wallet and wallet.balance_credits > 0:
                    raise ValueError(
                        f"Cannot delete merchant entity '{merchant_id}': "
                        f"wallet has a non-zero balance ({wallet.balance_credits} credits)"
                    )

                rows = await conn.fetch(query, *values)
                row = rows[0] if rows else None

                if row and row["user_count"] > 0:
                    raise ValueError(
                        f"Cannot delete merchant entity '{merchant_id}': "
                        f"{row['user_count']} user(s) still reference it"
                    )

                deleted_count = row["deleted_count"] if row else 0

                if deleted_count > 0 and wallet:
                    # Merchant delete succeeded and a wallet existed
                    # (balance already verified == 0 above) -- remove it too.
                    await delete_wallet_on_conn(conn, merchant_id)

            if deleted_count > 0:
                logger.info(f"Deleted merchant entity: {merchant_id}")
                return True

            return False
        return False
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Error deleting merchant entity {merchant_id}: {e}")
        raise


async def set_merchant_s2s_token(merchant_id: str, token: str) -> bool:
    """Store an S2S token (webhook HMAC secret) on a merchant row.

    Stored as-is in the nullable ``s2s_token`` column. Returns True if the
    merchant existed and was updated, False otherwise.
    """
    query, values = set_merchant_s2s_token_query(merchant_id, token)

    try:
        result = await run_parameterized_query(query, values)
        updated = bool(result)
        if updated:
            logger.info(f"Stored S2S token on merchant: {merchant_id}")
        else:
            logger.warning(
                f"Cannot store S2S token: merchant '{merchant_id}' not found"
            )
        return updated
    except Exception as e:
        logger.error(f"Error storing S2S token on merchant {merchant_id}: {e}")
        raise


async def get_merchant_s2s_token(merchant_id: str) -> Optional[str]:
    """Read a merchant's stored S2S token, or None if unset."""
    query, values = get_merchant_s2s_token_query(merchant_id)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if not row or not row["s2s_token"]:
            return None
        return row["s2s_token"]
    except Exception as e:
        logger.error(f"Error reading S2S token for merchant {merchant_id}: {e}")
        raise
