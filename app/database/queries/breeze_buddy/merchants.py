"""
Database query functions for merchant entity management.

This module contains ONLY query generation functions.
Business logic should be in accessor/breeze_buddy/merchants.py
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

MERCHANTS_TABLE = "merchants"


def check_merchant_identifier_exists_query(
    merchant_identifier: str,
) -> Tuple[str, List[Any]]:
    """Generate query to check if merchant_identifier exists."""
    query = f"SELECT 1 FROM {MERCHANTS_TABLE} WHERE merchant_identifier = $1 LIMIT 1"
    return query, [merchant_identifier]


def create_merchant_query(
    merchant_identifier: str,
    reseller_id: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: bool = True,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new merchant entity."""
    query = f"""
        INSERT INTO {MERCHANTS_TABLE} (
            merchant_identifier, name, description,
            is_active, reseller_id, created_at, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING merchant_identifier, name, description,
                  is_active, reseller_id, created_at, updated_at
    """
    now = datetime.now(timezone.utc)
    values = [merchant_identifier, name, description, is_active, reseller_id, now, now]
    return query, values


def get_merchant_by_merchant_identifier_query(
    merchant_identifier: str,
) -> Tuple[str, List[Any]]:
    """Generate query to get merchant by merchant_identifier."""
    query = f"""
        SELECT merchant_identifier, name, description,
               is_active, reseller_id, created_at, updated_at
        FROM {MERCHANTS_TABLE}
        WHERE merchant_identifier = $1
    """
    return query, [merchant_identifier]


def get_all_merchants_query(
    page: int = 1,
    limit: int = 50,
    merchant_identifier_filter: Optional[str] = None,
    name_filter: Optional[str] = None,
    reseller_id_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[str, str, List[Any]]:
    """Generate query to get all merchants with pagination and filtering.

    Returns:
        Tuple of (query, count_query, values)
    """
    offset = (page - 1) * limit

    # Build WHERE clause
    where_conditions = []
    params: list = []
    param_idx = 1

    if merchant_identifier_filter:
        where_conditions.append(f"merchant_identifier ILIKE ${param_idx}")
        escaped = merchant_identifier_filter.replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
        param_idx += 1

    if name_filter:
        where_conditions.append(f"name ILIKE ${param_idx}")
        escaped = name_filter.replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
        param_idx += 1

    if reseller_id_filter:
        where_conditions.append(f"reseller_id = ${param_idx}")
        params.append(reseller_id_filter)
        param_idx += 1

    if is_active_filter is not None:
        where_conditions.append(f"is_active = ${param_idx}")
        params.append(is_active_filter)
        param_idx += 1

    where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""

    # Validate sort fields
    valid_sort_fields = {"merchant_identifier", "name", "created_at", "updated_at"}
    if sort_by not in valid_sort_fields:
        sort_by = "created_at"
    if sort_order.lower() not in {"asc", "desc"}:
        sort_order = "desc"

    # Build queries
    query = f"""
        SELECT merchant_identifier, name, description,
               is_active, reseller_id, created_at, updated_at
        FROM {MERCHANTS_TABLE}
        {where_clause}
        ORDER BY {sort_by} {sort_order.upper()}
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """

    count_query = f"SELECT COUNT(*) as total FROM {MERCHANTS_TABLE} {where_clause}"

    params.extend([limit, offset])

    return query, count_query, params


def get_merchants_by_reseller_query(
    reseller_id: str,
    page: int = 1,
    limit: int = 50,
) -> Tuple[str, str, List[Any]]:
    """Generate query to get merchants by reseller_id.

    Returns:
        Tuple of (query, count_query, values)
    """
    offset = (page - 1) * limit

    query = f"""
        SELECT merchant_identifier, name, description,
               is_active, reseller_id, created_at, updated_at
        FROM {MERCHANTS_TABLE}
        WHERE reseller_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """

    count_query = f"""
        SELECT COUNT(*) as total
        FROM {MERCHANTS_TABLE}
        WHERE reseller_id = $1
    """

    return query, count_query, [reseller_id, limit, offset]


def update_merchant_query(
    merchant_identifier: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
    reseller_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to update a merchant entity.

    Returns:
        Tuple of (query, values) - values is empty list if no updates needed
    """
    set_clauses = []
    params: list = []
    param_idx = 1

    if name is not None:
        set_clauses.append(f"name = ${param_idx}")
        params.append(name)
        param_idx += 1

    if description is not None:
        set_clauses.append(f"description = ${param_idx}")
        params.append(description)
        param_idx += 1

    if is_active is not None:
        set_clauses.append(f"is_active = ${param_idx}")
        params.append(is_active)
        param_idx += 1

    if reseller_id is not None:
        set_clauses.append(f"reseller_id = ${param_idx}")
        params.append(reseller_id)
        param_idx += 1

    if not set_clauses:
        # No fields to update
        return "", []

    # Add updated_at
    set_clauses.append(f"updated_at = ${param_idx}")
    params.append(datetime.now(timezone.utc))
    param_idx += 1

    # Add merchant_identifier for WHERE clause
    params.append(merchant_identifier)

    query = f"""
        UPDATE {MERCHANTS_TABLE}
        SET {', '.join(set_clauses)}
        WHERE merchant_identifier = ${param_idx}
        RETURNING merchant_identifier, name, description,
                  is_active, reseller_id, created_at, updated_at
    """

    return query, params


def get_merchants_by_ids_query(
    merchant_ids: List[str],
    page: int = 1,
    limit: int = 50,
    merchant_identifier_filter: Optional[str] = None,
    name_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[str, str, List[Any]]:
    """Generate query to get merchant entities by a list of merchant identifiers.

    Returns:
        Tuple of (query, count_query, values)
    """
    offset = (page - 1) * limit

    # Build WHERE clause - always scoped to the given IDs
    where_conditions = ["merchant_identifier = ANY($1)"]
    params: list = [merchant_ids]
    param_idx = 2

    if merchant_identifier_filter:
        where_conditions.append(f"merchant_identifier ILIKE ${param_idx}")
        escaped = merchant_identifier_filter.replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
        param_idx += 1

    if name_filter:
        where_conditions.append(f"name ILIKE ${param_idx}")
        escaped = name_filter.replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
        param_idx += 1

    if is_active_filter is not None:
        where_conditions.append(f"is_active = ${param_idx}")
        params.append(is_active_filter)
        param_idx += 1

    where_clause = f"WHERE {' AND '.join(where_conditions)}"

    # Validate sort fields
    valid_sort_fields = {"merchant_identifier", "name", "created_at", "updated_at"}
    if sort_by not in valid_sort_fields:
        sort_by = "created_at"
    if sort_order.lower() not in {"asc", "desc"}:
        sort_order = "desc"

    query = f"""
        SELECT merchant_identifier, name, description,
               is_active, reseller_id, created_at, updated_at
        FROM {MERCHANTS_TABLE}
        {where_clause}
        ORDER BY {sort_by} {sort_order.upper()}
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """

    count_query = f"SELECT COUNT(*) as total FROM {MERCHANTS_TABLE} {where_clause}"

    params.extend([limit, offset])

    return query, count_query, params


def delete_merchant_query(merchant_identifier: str) -> Tuple[str, List[Any]]:
    """Generate query to delete a merchant with dependency check.

    Uses atomic CTE to check dependencies and delete in a single operation.
    """
    query = """
        WITH dependency_check AS (
            SELECT COUNT(*) as user_count
            FROM users
            WHERE merchant_identifiers ? $1
        ),
        delete_operation AS (
            DELETE FROM merchants
            WHERE merchant_identifier = $1
            AND (SELECT user_count FROM dependency_check) = 0
            RETURNING merchant_identifier
        )
        SELECT
            (SELECT user_count FROM dependency_check) as user_count,
            (SELECT COUNT(*) FROM delete_operation) as deleted_count
    """
    return query, [merchant_identifier]
