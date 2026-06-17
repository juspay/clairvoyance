"""SQL queries for outbound_number_pool table operations."""

from typing import Any, List, Optional, Tuple

OUTBOUND_NUMBER_POOL_TABLE = "outbound_number_pool"
OUTBOUND_NUMBER_TABLE = "outbound_number"


def insert_outbound_number_pool_query(
    id: str,
    name: str,
    provider: str,
    reseller_id: str,
    merchant_id: Optional[str],
    max_channels: int,
) -> Tuple[str, List[Any]]:
    """Insert a new outbound number pool."""
    query = f"""
        INSERT INTO "{OUTBOUND_NUMBER_POOL_TABLE}"
        ("id", "name", "provider", "reseller_id", "merchant_id", "max_channels",
         "current_channels", "rotation_index", "status", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5, $6, 0, 0, 'ACTIVE', NOW(), NOW())
        RETURNING *;
    """
    values = [id, name, provider, reseller_id, merchant_id, max_channels]
    return query, values


def get_outbound_number_pool_by_id_query(
    pool_id: str,
) -> Tuple[str, List[Any]]:
    """Get a pool by its ID."""
    query = f"""
        SELECT * FROM "{OUTBOUND_NUMBER_POOL_TABLE}"
        WHERE "id" = $1;
    """
    return query, [pool_id]


def get_all_outbound_number_pools_query() -> Tuple[str, List[Any]]:
    """Get all pools ordered by creation date."""
    query = f"""
        SELECT * FROM "{OUTBOUND_NUMBER_POOL_TABLE}"
        ORDER BY "created_at" DESC;
    """
    return query, []


def get_outbound_number_pools_by_reseller_query(
    reseller_id: str,
    merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Get pools for a given reseller, optionally filtered by merchant."""
    if merchant_id is not None:
        query = f"""
            SELECT * FROM "{OUTBOUND_NUMBER_POOL_TABLE}"
            WHERE "reseller_id" = $1 AND "merchant_id" = $2
            ORDER BY "created_at" DESC;
        """
        return query, [reseller_id, merchant_id]
    else:
        query = f"""
            SELECT * FROM "{OUTBOUND_NUMBER_POOL_TABLE}"
            WHERE "reseller_id" = $1
            ORDER BY "created_at" DESC;
        """
        return query, [reseller_id]


def update_outbound_number_pool_query(
    pool_id: str,
    name: Optional[str] = None,
    max_channels: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """Update pool fields (name, max_channels). Only non-None fields are updated."""
    set_clauses = ['"updated_at" = NOW()']
    values: List[Any] = []
    param_index = 1

    if name is not None:
        set_clauses.append(f'"name" = ${param_index}')
        values.append(name)
        param_index += 1

    if max_channels is not None:
        set_clauses.append(f'"max_channels" = ${param_index}')
        values.append(max_channels)
        param_index += 1

    values.append(pool_id)
    query = f"""
        UPDATE "{OUTBOUND_NUMBER_POOL_TABLE}"
        SET {", ".join(set_clauses)}
        WHERE "id" = ${param_index}
        RETURNING *;
    """
    return query, values


def disable_outbound_number_pool_query(
    pool_id: str,
) -> Tuple[str, List[Any]]:
    """Soft-delete a pool by setting status to DISABLED."""
    query = f"""
        UPDATE "{OUTBOUND_NUMBER_POOL_TABLE}"
        SET "status" = 'DISABLED', "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    return query, [pool_id]


def increment_pool_channels_query(
    pool_id: str,
) -> Tuple[str, List[Any]]:
    """Atomically increment current_channels if below max_channels.
    Returns zero rows if the pool is at capacity.
    """
    query = f"""
        UPDATE "{OUTBOUND_NUMBER_POOL_TABLE}"
        SET "current_channels" = COALESCE("current_channels", 0) + 1,
            "updated_at" = NOW()
        WHERE "id" = $1
          AND COALESCE("current_channels", 0) < COALESCE("max_channels", 0)
        RETURNING *;
    """
    return query, [pool_id]


def decrement_pool_channels_query(
    pool_id: str,
) -> Tuple[str, List[Any]]:
    """Atomically decrement current_channels (floor at 0)."""
    query = f"""
        UPDATE "{OUTBOUND_NUMBER_POOL_TABLE}"
        SET "current_channels" = GREATEST(0, COALESCE("current_channels", 0) - 1),
            "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    return query, [pool_id]


def increment_pool_rotation_index_query(
    pool_id: str,
) -> Tuple[str, List[Any]]:
    """Atomically increment the rotation index and return the updated pool."""
    query = f"""
        UPDATE "{OUTBOUND_NUMBER_POOL_TABLE}"
        SET "rotation_index" = (COALESCE("rotation_index", 0) + 1) % 1000000,
            "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    return query, [pool_id]


def get_numbers_by_pool_id_query(
    pool_id: str,
) -> Tuple[str, List[Any]]:
    """Get all outbound numbers that belong to a pool, ordered by creation date."""
    query = f"""
        SELECT * FROM "{OUTBOUND_NUMBER_TABLE}"
        WHERE "pool_id" = $1
        ORDER BY "created_at" ASC;
    """
    return query, [pool_id]


def set_number_pool_id_query(
    outbound_number_id: str,
    pool_id: str,
) -> Tuple[str, List[Any]]:
    """Assign an outbound number to a pool."""
    query = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}"
        SET "pool_id" = $1, "updated_at" = NOW()
        WHERE "id" = $2
        RETURNING *;
    """
    return query, [pool_id, outbound_number_id]


def clear_number_pool_id_query(
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """Remove an outbound number from its pool."""
    query = f"""
        UPDATE "{OUTBOUND_NUMBER_TABLE}"
        SET "pool_id" = NULL, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    return query, [outbound_number_id]
