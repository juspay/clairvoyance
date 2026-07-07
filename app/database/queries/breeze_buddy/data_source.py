"""SQL query builders for Breeze Buddy data sources."""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

DATA_SOURCE_TABLE = "data_source"


def insert_data_source_query(
    id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    source_type: str,
    config: str,
) -> Tuple[str, List[Any]]:
    """Generate query to insert a data source."""
    now = datetime.now(timezone.utc)
    query = f"""
        INSERT INTO "{DATA_SOURCE_TABLE}"
            ("id", "reseller_id", "merchant_id", "name", "source_type",
             "config", "is_active", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, TRUE, $7, $8)
        RETURNING *;
    """
    return query, [
        id,
        reseller_id,
        merchant_id,
        name,
        source_type,
        config,
        now,
        now,
    ]


def get_data_source_by_id_query(
    data_source_id: str, include_inactive: bool = False
) -> Tuple[str, List[Any]]:
    """Generate query to get a data source by ID."""
    active_clause = "" if include_inactive else ' AND "is_active" = TRUE'
    query = f'SELECT * FROM "{DATA_SOURCE_TABLE}" WHERE "id" = $1{active_clause};'
    return query, [data_source_id]


def list_data_sources_query(
    reseller_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_id: Optional[str] = None,
    merchant_ids: Optional[List[str]] = None,
    include_inactive: bool = False,
) -> Tuple[str, List[Any]]:
    """Generate query to list data sources with optional scope filters."""
    conditions: List[str] = []
    values: List[Any] = []

    if reseller_ids is not None:
        if reseller_ids:
            values.append(reseller_ids)
            conditions.append(f'"reseller_id" = ANY(${len(values)}::text[])')
        else:
            conditions.append("FALSE")
    elif reseller_id:
        values.append(reseller_id)
        conditions.append(f'"reseller_id" = ${len(values)}')

    if merchant_ids is not None:
        if merchant_ids:
            values.append(merchant_ids)
            conditions.append(
                f'("merchant_id" = ANY(${len(values)}::text[]) OR "merchant_id" IS NULL)'
            )
        else:
            conditions.append('"merchant_id" IS NULL')
    elif merchant_id:
        values.append(merchant_id)
        conditions.append(f'("merchant_id" = ${len(values)} OR "merchant_id" IS NULL)')

    if not include_inactive:
        conditions.append('"is_active" = TRUE')

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    query = f"""
        SELECT *
        FROM "{DATA_SOURCE_TABLE}"
        {where_clause}
        ORDER BY "created_at" DESC;
    """
    return query, values


def update_data_source_query(
    data_source_id: str,
    name: Optional[str] = None,
    merchant_id: Optional[str] = None,
    config: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to update a data source. Only provided fields change."""
    updates: List[str] = []
    values: List[Any] = []

    if name is not None:
        values.append(name)
        updates.append(f'"name" = ${len(values)}')

    if merchant_id is not None:
        values.append(merchant_id)
        updates.append(f'"merchant_id" = ${len(values)}')

    if config is not None:
        values.append(config)
        updates.append(f'"config" = ${len(values)}::jsonb')

    if is_active is not None:
        values.append(is_active)
        updates.append(f'"is_active" = ${len(values)}')

    values.append(datetime.now(timezone.utc))
    updates.append(f'"updated_at" = ${len(values)}')

    values.append(data_source_id)
    query = f"""
        UPDATE "{DATA_SOURCE_TABLE}"
        SET {", ".join(updates)}
        WHERE "id" = ${len(values)}
        RETURNING *;
    """
    return query, values


def deactivate_data_source_query(data_source_id: str) -> Tuple[str, List[Any]]:
    """Generate query to soft-delete a data source."""
    query = f"""
        UPDATE "{DATA_SOURCE_TABLE}"
        SET "is_active" = FALSE, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    return query, [data_source_id]
