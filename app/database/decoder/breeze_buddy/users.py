"""
User accessors - response builders for user queries.
"""

from app.schemas import UserRole
from app.schemas.breeze_buddy.users import UserResponse
from app.utils.common import parse_json_field


def decode_user(row) -> UserResponse:
    """Build UserResponse from database row.

    Args:
        row: Database row containing user data

    Returns:
        UserResponse instance
    """
    return UserResponse(
        id=str(row["id"]),
        username=row["username"],
        email=row.get("email"),
        role=UserRole(row["role"]),
        reseller_ids=parse_json_field(row.get("reseller_ids")),
        merchant_identifiers=parse_json_field(row.get("merchant_identifiers")),
        is_active=row["is_active"],
        owner_id=str(row["owner_id"]) if row.get("owner_id") else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
