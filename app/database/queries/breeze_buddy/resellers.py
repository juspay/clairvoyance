"""
Database query builders for reseller (umbrella) entity operations.

All functions here are pure — they return (query_string, values) tuples and
perform no I/O. All async DB execution lives in the accessor layer.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

RESELLERS_TABLE = "resellers"

# Every read carries the same computed columns the console needs: workspace
# count, grant-holder count, and whether a same-id reseller login exists.
_RESELLER_SELECT = f"""
    SELECT r.id, r.name, r.description, r.is_active, r.created_at, r.updated_at,
           (SELECT COUNT(*) FROM merchants m
             WHERE m.reseller_id = r.id) AS workspace_count,
           (SELECT COUNT(*) FROM user_reseller_access ura
             WHERE ura.reseller_id = r.id) AS member_count,
           EXISTS (SELECT 1 FROM users u
                    WHERE u.id = r.id AND u.role = 'reseller') AS has_login
    FROM {RESELLERS_TABLE} r
"""


def get_reseller_by_id_query(reseller_id: str) -> Tuple[str, List[Any]]:
    """Generate query to get one reseller with computed columns."""
    query = f"{_RESELLER_SELECT} WHERE r.id = $1"
    return query, [reseller_id]


def get_all_resellers_query(
    page: int = 1,
    limit: int = 50,
    id_or_name_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    allowed_reseller_ids: Optional[List[str]] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[str, str, List[Any]]:
    """Generate query to list resellers with pagination and RBAC filtering.

    Args:
        allowed_reseller_ids: RBAC scope — umbrellas the caller may see
            (None = unrestricted, i.e. admin).

    Returns:
        Tuple of (query, count_query, values)
    """
    offset = (page - 1) * limit

    where_conditions = []
    params: list = []
    param_idx = 1

    if id_or_name_filter:
        escaped = id_or_name_filter.replace("%", "\\%").replace("_", "\\_")
        where_conditions.append(
            f"(r.id ILIKE ${param_idx} OR r.name ILIKE ${param_idx})"
        )
        params.append(f"%{escaped}%")
        param_idx += 1

    if is_active_filter is not None:
        where_conditions.append(f"r.is_active = ${param_idx}")
        params.append(is_active_filter)
        param_idx += 1

    if allowed_reseller_ids is not None:
        where_conditions.append(f"r.id = ANY(${param_idx}::text[])")
        params.append(allowed_reseller_ids)
        param_idx += 1

    where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""

    valid_sort_fields = {"id", "name", "created_at", "updated_at"}
    if sort_by not in valid_sort_fields:
        sort_by = "created_at"
    if sort_order.lower() not in {"asc", "desc"}:
        sort_order = "desc"

    query = f"""
        {_RESELLER_SELECT}
        {where_clause}
        ORDER BY r.{sort_by} {sort_order.upper()}
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """

    count_query = f"SELECT COUNT(*) as total FROM {RESELLERS_TABLE} r {where_clause}"

    params.extend([limit, offset])

    return query, count_query, params


def create_reseller_query(
    id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: bool = True,
) -> Tuple[str, List[Any]]:
    """Generate query to create a reseller entity."""
    now = datetime.now(timezone.utc)
    query = f"""
        INSERT INTO {RESELLERS_TABLE} (
            id, name, description, is_active, created_at, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, name, description, is_active, created_at, updated_at
    """
    return query, [id, name, description, is_active, now, now]


def update_reseller_query(
    reseller_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to update a reseller entity (id cannot be changed).

    Returns:
        Tuple of (query, values) — values is empty if no updates requested.
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

    if not set_clauses:
        return "", []

    set_clauses.append(f"updated_at = ${param_idx}")
    params.append(datetime.now(timezone.utc))
    param_idx += 1

    params.append(reseller_id)

    query = f"""
        UPDATE {RESELLERS_TABLE}
        SET {', '.join(set_clauses)}
        WHERE id = ${param_idx}
        RETURNING id, name, description, is_active, created_at, updated_at
    """
    return query, params


def delete_reseller_query(reseller_id: str) -> Tuple[str, List[Any]]:
    """Generate query to delete a reseller entity.

    The merchants.reseller_id FK (NO ACTION) makes the delete fail while any
    merchant still references the umbrella — the accessor maps that to a
    conflict error. Grant rows cascade.
    """
    query = f"DELETE FROM {RESELLERS_TABLE} WHERE id = $1 RETURNING id"
    return query, [reseller_id]


# ─────────────────────────────────────────────────────────────────────────────
# Effective access (normalized read used by GET /user/{id}/access)
# ─────────────────────────────────────────────────────────────────────────────


def get_user_umbrella_grants_query(user_id: str) -> Tuple[str, List[Any]]:
    """Generate query to list a user's umbrella grants with display names."""
    query = """
        SELECT ura.reseller_id, r.name AS reseller_name, ura.all_workspaces
        FROM user_reseller_access ura
        LEFT JOIN resellers r ON r.id = ura.reseller_id
        WHERE ura.user_id = $1
        ORDER BY ura.reseller_id
    """
    return query, [user_id]


def get_user_workspace_access_query(user_id: str) -> Tuple[str, List[Any]]:
    """Generate query to list every workspace a user can reach.

    Explicit membership rows win over inherited rows for the same workspace
    (DISTINCT ON keeps the first by source ordering: 'explicit' < 'inherited').
    """
    query = """
        SELECT DISTINCT ON (merchant_id)
               merchant_id, name, source, via_reseller
        FROM (
            SELECT uma.merchant_id, m.name, 'explicit' AS source,
                   NULL::varchar AS via_reseller
            FROM user_merchant_access uma
            JOIN merchants m ON m.merchant_id = uma.merchant_id
            WHERE uma.user_id = $1
            UNION ALL
            SELECT m.merchant_id, m.name, 'inherited' AS source,
                   ura.reseller_id AS via_reseller
            FROM user_reseller_access ura
            JOIN merchants m ON m.reseller_id = ura.reseller_id
            WHERE ura.user_id = $1 AND ura.all_workspaces
        ) access
        ORDER BY merchant_id, source
    """
    return query, [user_id]
