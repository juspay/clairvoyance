"""
Database queries for user authentication.
Provides essential user lookup functionality for JWT authentication.
"""

import json
from typing import Optional

from app.core.logger import logger
from app.database import get_db_connection
from app.schemas import UserRole, UserInDB


async def get_user_by_username(username: str) -> Optional[UserInDB]:
    """
    Get user by username for authentication.

    Args:
        username: Username to search for

    Returns:
        UserInDB object if found, None otherwise
    """
    query = """
        SELECT id, username, password_hash, role, email, merchant_ids, shop_identifiers, is_active, created_at, updated_at
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
                merchant_ids=row["merchant_ids"]
                if isinstance(row["merchant_ids"], list)
                else json.loads(row["merchant_ids"]),
                shop_identifiers=row["shop_identifiers"]
                if isinstance(row["shop_identifiers"], list)
                else json.loads(row["shop_identifiers"]),
                is_active=row["is_active"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    except Exception as e:
        logger.error(f"Error fetching user by username {username}: {e}")
        return None


