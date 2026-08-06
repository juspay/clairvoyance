"""
Database accessor functions for user account management.

This module provides business logic for user operations,
using queries from queries.breeze_buddy.users and
decoders from decoder.breeze_buddy.users.
"""

import json
from typing import List, Optional, Tuple

from app.core.logger import logger
from app.core.security.password import hash_password_async
from app.database import get_db_connection
from app.database.accessor.breeze_buddy.access_grants import (
    ensure_reseller_on_conn,
    sync_user_access_on_conn,
)
from app.database.decoder.breeze_buddy.users import decode_user
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.merchants import create_merchant_query
from app.database.queries.breeze_buddy.users import (
    check_email_exists_query,
    check_username_exists_query,
    create_user_query,
    delete_user_query,
    get_all_users_query,
    get_user_by_email_query,
    get_user_by_id_query,
    get_user_by_username_query,
    get_user_in_db_by_id_query,
    get_users_by_email_query,
    update_user_query,
)
from app.schemas.breeze_buddy.auth import UserInDB, UserRole
from app.schemas.breeze_buddy.users import UserResponse


async def check_username_exists(username: str) -> bool:
    """Check if a username already exists."""
    query, values = check_username_exists_query(username)
    try:
        result = await run_parameterized_query(query, values)
        return result is not None and len(result) > 0
    except Exception as e:
        logger.error(f"Error checking username {username}: {e}")
        raise


async def check_email_exists(email: str) -> bool:
    """Check if an email address is already registered."""
    query, values = check_email_exists_query(email)
    try:
        result = await run_parameterized_query(query, values)
        return result is not None and len(result) > 0
    except Exception as e:
        logger.error(f"Error checking email {email}: {e}")
        raise


def _decode_user_in_db(row) -> UserInDB:
    """Decode a raw asyncpg Record into a UserInDB model."""
    return UserInDB(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        email=row.get("email"),
        reseller_ids=json.loads(row["reseller_ids"]) if row.get("reseller_ids") else [],
        merchant_ids=json.loads(row["merchant_ids"]) if row.get("merchant_ids") else [],
        is_active=row["is_active"],
        owner_id=row.get("owner_id"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def get_user_by_username(username: str) -> Optional[UserInDB]:
    """Get a full UserInDB record (including password_hash) by username."""
    query, values = get_user_by_username_query(username)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return _decode_user_in_db(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching user by username '{username}': {e}")
        raise


async def get_user_by_email(email: str) -> Optional[UserInDB]:
    """Get a full UserInDB record (including password_hash) by email (first match)."""
    query, values = get_user_by_email_query(email)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return _decode_user_in_db(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching user by email '{email}': {e}")
        raise


async def get_users_by_email(email: str) -> List[UserInDB]:
    """Get all UserInDB records sharing a given email address."""
    query, values = get_users_by_email_query(email)
    try:
        result = await run_parameterized_query(query, values)
        return [_decode_user_in_db(row) for row in result] if result else []
    except Exception as e:
        logger.error(f"Error fetching users by email '{email}': {e}")
        raise


async def get_user_in_db_by_id(user_id: str) -> Optional[UserInDB]:
    """Get a full UserInDB record (including password_hash) by user ID."""
    query, values = get_user_in_db_by_id_query(user_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return _decode_user_in_db(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching user by id '{user_id}': {e}")
        raise


async def create_merchant_and_user_atomically(
    *,
    merchant_id: str,
    merchant_name: str,
    merchant_description: str | None,
    reseller_id: str,
    user_id: str,
    username: str,
    password: str,
    email: str | None,
    role: UserRole,
) -> None:
    """
    Insert both the merchant entity and the user account inside a single
    DB transaction.  Either both succeed or both are rolled back — no
    orphaned merchant rows if the user insert fails.
    """
    password_hash = await hash_password_async(password)

    merchant_q, merchant_v = create_merchant_query(
        merchant_id=merchant_id,
        name=merchant_name,
        description=merchant_description,
        is_active=True,
        reseller_id=reseller_id,
    )
    user_q, user_v = create_user_query(
        id=user_id,
        username=username,
        password_hash=password_hash,
        role=role,
        email=email,
        reseller_ids=[reseller_id],
        merchant_ids=[merchant_id],
        is_active=True,
        owner_id=None,
    )

    async for conn in get_db_connection():
        async with conn.transaction():
            # The umbrella must exist before the merchants-row FK lands
            # (BB_SELF_SIGNUP_RESELLER_ID may point at an unprovisioned slug).
            await ensure_reseller_on_conn(conn, reseller_id)
            await conn.execute(merchant_q, *merchant_v)
            await conn.execute(user_q, *user_v)
            await sync_user_access_on_conn(
                conn,
                user_id=user_id,
                role=getattr(role, "value", role),
                reseller_ids=[reseller_id],
                merchant_ids=[merchant_id],
                username=username,
            )
        return


async def create_user(
    id: str,
    username: str,
    password: str,
    role: str,
    owner_id: Optional[str] = None,
    email: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    is_active: bool = True,
) -> Optional[UserResponse]:
    """Create a new user account (login account).

    Args:
        id: Chosen account ID (no spaces, unique)
        username: Unique username
        password: Plain text password (will be hashed)
        role: User role (admin, reseller, merchant, user)
        owner_id: ID of the user creating this account (None for top-level resellers)
        email: Optional email address
        reseller_ids: List of reseller IDs for access control
        merchant_ids: List of merchant IDs for access control
        is_active: Whether account is active

    Returns:
        UserResponse if successful, None otherwise
    """
    password_hash = await hash_password_async(password)

    if reseller_ids is None:
        reseller_ids = []
    if merchant_ids is None:
        merchant_ids = []

    query, values = create_user_query(
        id=id,
        username=username,
        password_hash=password_hash,
        role=role,
        owner_id=owner_id,
        email=email,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        is_active=is_active,
    )

    try:
        async for conn in get_db_connection():
            async with conn.transaction():
                result = await conn.fetch(query, *values)
                row = result[0] if result else None
                if row:
                    # Keep the grant tables a projection of the JSONB arrays
                    # (dual-write; arrays stay authoritative until cutover).
                    await sync_user_access_on_conn(
                        conn,
                        user_id=id,
                        role=getattr(role, "value", role),
                        reseller_ids=reseller_ids,
                        merchant_ids=merchant_ids,
                        created_by=owner_id,
                        username=username,
                    )

            if row:
                logger.info(f"Created user account: {username} (role: {role})")
                return decode_user(row)

            return None
        return None
    except Exception as e:
        logger.error(f"Error creating user {username}: {e}")
        raise


async def get_all_users(
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
) -> Tuple[List[UserResponse], int]:
    """Get all user accounts with pagination and RBAC filtering.

    Args:
        page: Page number (1-indexed)
        limit: Items per page
        username_filter: Filter by username (partial match)
        role_filter: Filter by role (exact match)
        reseller_id_filter: Filter by reseller ID (checks if in array)
        merchant_identifier_filter: Filter by merchant identifier (checks if in array)
        owner_id_filter: Filter by owner_id (exact match)
        is_active_filter: Filter by active status
        allowed_merchant_ids: RBAC - merchant_ids the caller can access (None = all access)
        excluded_roles: Roles to exclude from results (for RBAC)
        sort_by: Field to sort by (username, role, created_at, updated_at)
        sort_order: Sort direction (asc, desc)

    Returns:
        Tuple of (list of users, total count)
    """
    query, count_query, values = get_all_users_query(
        page=page,
        limit=limit,
        username_filter=username_filter,
        role_filter=role_filter,
        reseller_id_filter=reseller_id_filter,
        merchant_identifier_filter=merchant_identifier_filter,
        owner_id_filter=owner_id_filter,
        is_active_filter=is_active_filter,
        allowed_merchant_ids=allowed_merchant_ids,
        excluded_roles=excluded_roles,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    try:
        rows = await run_parameterized_query(query, values)
        count_result = await run_parameterized_query(
            count_query, values[:-2] if len(values) > 2 else []
        )
        count_row = count_result[0] if count_result else None

        users = [decode_user(row) for row in rows] if rows else []
        total = count_row["total"] if count_row else 0

        return users, total
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        raise


async def get_user_by_id(user_id: str) -> Optional[UserResponse]:
    """Get user account by ID.

    Args:
        user_id: ID of user to fetch

    Returns:
        UserResponse if found, None otherwise
    """
    query, values = get_user_by_id_query(user_id)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None

        if row:
            return decode_user(row)

        return None
    except Exception as e:
        logger.error(f"Error fetching user {user_id}: {e}")
        raise


async def update_user(
    user_id: str,
    password: Optional[str] = None,
    email: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    is_active: Optional[bool] = None,
) -> Optional[UserResponse]:
    """Update user account (username and role cannot be changed).

    Args:
        user_id: ID of user to update
        password: New password (will be hashed)
        email: New email
        reseller_ids: New reseller IDs list
        merchant_ids: New merchant identifiers list
        is_active: New active status

    Returns:
        UserResponse if successful, None if user not found
    """
    password_hash = await hash_password_async(password) if password else None

    query, values = update_user_query(
        user_id=user_id,
        password_hash=password_hash,
        email=email,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        is_active=is_active,
    )

    if not values:
        return await get_user_by_id(user_id)

    access_changed = reseller_ids is not None or merchant_ids is not None

    try:
        async for conn in get_db_connection():
            async with conn.transaction():
                result = await conn.fetch(query, *values)
                row = result[0] if result else None
                if row and access_changed:
                    # Re-project from the post-update arrays (RETURNING row),
                    # so the grant tables track whatever the arrays now say.
                    await sync_user_access_on_conn(
                        conn,
                        user_id=user_id,
                        role=row["role"],
                        reseller_ids=(
                            json.loads(row["reseller_ids"])
                            if row.get("reseller_ids")
                            else []
                        ),
                        merchant_ids=(
                            json.loads(row["merchant_ids"])
                            if row.get("merchant_ids")
                            else []
                        ),
                        username=row["username"],
                    )

            if row:
                logger.info(f"Updated user: {user_id}")
                return decode_user(row)

            return None
        return None
    except Exception as e:
        logger.error(f"Error updating user {user_id}: {e}")
        raise


async def delete_user(user_id: str) -> bool:
    """Delete user account by ID.

    Args:
        user_id: ID of user to delete

    Returns:
        True if deleted, False if not found

    Raises:
        ValueError: If attempting to delete an admin account
    """
    query, values = delete_user_query(user_id)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None

        if row:
            logger.info(f"Deleted user: {user_id}")
            return True

        check_query = "SELECT role FROM users WHERE id = $1"
        check_result = await run_parameterized_query(check_query, [user_id])
        check_row = check_result[0] if check_result else None

        if check_row and check_row["role"] == "admin":
            raise ValueError("Admin accounts cannot be deleted")

        return False
    except Exception:
        logger.exception(f"Error deleting user {user_id}")
        raise
