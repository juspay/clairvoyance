"""
Database queries for user authentication.
Provides essential user lookup functionality for JWT authentication.
"""

import json
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from app.core.logger import logger
from app.database import get_db_connection
from app.schemas import UserInDB, UserRole

USERS_TABLE = "users"


async def get_user_by_username(username: str) -> Optional[UserInDB]:
    """
    Get user by username for authentication.

    Args:
        username: Username to search for

    Returns:
        UserInDB object if found, None otherwise
    """
    query = """
        SELECT
            id,
            username,
            password_hash,
            role,
            email,
            reseller_ids,
            merchant_ids,
            is_active,
            owner_id,
            created_at,
            updated_at
        FROM users
        WHERE username = $1
    """

    try:
        async for conn in get_db_connection():
            row = await conn.fetchrow(query, username)

            if not row:
                return None

            return UserInDB(
                id=str(row["id"]),
                username=row["username"],
                password_hash=row["password_hash"],
                role=UserRole(row["role"]),
                email=row["email"],
                reseller_ids=(
                    row["reseller_ids"]
                    if isinstance(row["reseller_ids"], list)
                    else json.loads(row["reseller_ids"])
                ),
                merchant_ids=(
                    row["merchant_ids"]
                    if isinstance(row["merchant_ids"], list)
                    else json.loads(row["merchant_ids"])
                ),
                is_active=row["is_active"],
                owner_id=str(row["owner_id"]) if row.get("owner_id") else None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    except Exception as e:
        logger.error(f"Error fetching user by username {username}: {e}")
        return None


def check_username_exists_query(username: str) -> Tuple[str, List[Any]]:
    """Generate query to check if username exists."""
    query = "SELECT 1 FROM users WHERE username = $1"
    return query, [username]


def create_user_query(
    id: str,
    username: str,
    password_hash: str,
    role: str,
    owner_id: Optional[str] = None,
    email: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    is_active: bool = True,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new user account."""
    if reseller_ids is None:
        reseller_ids = []
    if merchant_ids is None:
        merchant_ids = []

    query = """
        INSERT INTO users (
            id, username, password_hash, role, email, reseller_ids, merchant_ids,
            is_active, owner_id, created_at, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id, username, role, email, reseller_ids, merchant_ids,
                  is_active, owner_id, created_at, updated_at
    """

    now = datetime.now(timezone.utc)
    values = [
        id,
        username,
        password_hash,
        role,
        email,
        json.dumps(reseller_ids),
        json.dumps(merchant_ids),
        is_active,
        owner_id,
        now,
        now,
    ]

    return query, values


def get_all_users_query(
    page: int = 1,
    limit: int = 50,
    username_filter: Optional[str] = None,
    role_filter: Optional[str] = None,
    reseller_id_filter: Optional[str] = None,
    merchant_identifier_filter: Optional[str] = None,
    owner_id_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    allowed_merchant_ids: Optional[List[str]] = None,
    excluded_roles: Optional[List[str]] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[str, str, List[Any]]:
    """Generate query to get all user accounts with pagination and RBAC filtering.

    Returns:
        Tuple of (query, count_query, values)
    """
    offset = (page - 1) * limit

    # Build WHERE clause
    where_conditions = []
    params: list = []
    param_idx = 1

    if username_filter:
        where_conditions.append(f"username ILIKE ${param_idx}")
        escaped = username_filter.replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
        param_idx += 1

    if role_filter:
        where_conditions.append(f"role = ${param_idx}")
        params.append(role_filter)
        param_idx += 1

    if reseller_id_filter:
        where_conditions.append(f"reseller_ids ? ${param_idx}")
        params.append(reseller_id_filter)
        param_idx += 1

    if merchant_identifier_filter:
        where_conditions.append(f"merchant_ids ? ${param_idx}")
        params.append(merchant_identifier_filter)
        param_idx += 1

    if owner_id_filter:
        where_conditions.append(f"owner_id = ${param_idx}")
        params.append(owner_id_filter)
        param_idx += 1

    if is_active_filter is not None:
        where_conditions.append(f"is_active = ${param_idx}")
        params.append(is_active_filter)
        param_idx += 1

    # RBAC filtering - filter by merchant_ids column
    # allowed_merchant_ids comes from resolve_merchant_ids() which already
    # resolves reseller_ids → merchant_ids hierarchically
    if allowed_merchant_ids is not None and "*" not in allowed_merchant_ids:
        where_conditions.append(f"merchant_ids ?| ${param_idx}")
        params.append(allowed_merchant_ids)
        param_idx += 1

    # Exclude certain roles (for RBAC - resellers/merchants can't see admin/reseller accounts)
    if excluded_roles:
        placeholders = ", ".join(
            [f"${param_idx + i}" for i in range(len(excluded_roles))]
        )
        where_conditions.append(f"role NOT IN ({placeholders})")
        params.extend(excluded_roles)
        param_idx += len(excluded_roles)

    where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""

    # Validate sort fields
    valid_sort_fields = {"username", "role", "created_at", "updated_at"}
    if sort_by not in valid_sort_fields:
        sort_by = "created_at"
    if sort_order.lower() not in {"asc", "desc"}:
        sort_order = "desc"

    # Build queries
    query = f"""
        SELECT id, username, role, email, reseller_ids, merchant_ids,
               is_active, owner_id, created_at, updated_at
        FROM users
        {where_clause}
        ORDER BY {sort_by} {sort_order.upper()}
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """

    count_query = f"SELECT COUNT(*) as total FROM users {where_clause}"

    params.extend([limit, offset])

    return query, count_query, params


def get_user_by_id_query(user_id: str) -> Tuple[str, List[Any]]:
    """Generate query to get user account by ID."""
    query = """
        SELECT id, username, role, email, reseller_ids, merchant_ids,
               is_active, owner_id, created_at, updated_at
        FROM users
        WHERE id = $1
    """
    return query, [user_id]


def update_user_query(
    user_id: str,
    password_hash: Optional[str] = None,
    email: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    is_active: Optional[bool] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to update user account.

    Returns:
        Tuple of (query, values) - values is empty list if no updates needed
    """
    set_clauses = []
    params: list = []
    param_idx = 1

    if password_hash is not None:
        set_clauses.append(f"password_hash = ${param_idx}")
        params.append(password_hash)
        param_idx += 1

    if email is not None:
        set_clauses.append(f"email = ${param_idx}")
        params.append(email)
        param_idx += 1

    if reseller_ids is not None:
        set_clauses.append(f"reseller_ids = ${param_idx}")
        params.append(json.dumps(reseller_ids))
        param_idx += 1

    if merchant_ids is not None:
        set_clauses.append(f"merchant_ids = ${param_idx}")
        params.append(json.dumps(merchant_ids))
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

    params.append(user_id)

    query = f"""
        UPDATE users
        SET {', '.join(set_clauses)}
        WHERE id = ${param_idx}
        RETURNING id, username, role, email, reseller_ids, merchant_ids,
                  is_active, owner_id, created_at, updated_at
    """

    return query, params


def delete_user_query(user_id: str) -> Tuple[str, List[Any]]:
    """Generate query to delete user by ID (excludes admin accounts)."""
    query = """
        DELETE FROM users
        WHERE id = $1 AND role != 'admin'
        RETURNING id
    """
    return query, [user_id]
