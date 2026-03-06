"""
Decoder functions for blacklisted numbers.
"""

from typing import List, Optional

import asyncpg

from app.schemas import BlacklistedNumber


def decode_blacklisted_number(row: asyncpg.Record) -> Optional[BlacklistedNumber]:
    """
    Decode blacklisted number from database result using Pydantic model.
    """
    if not row:
        return None

    return BlacklistedNumber(
        id=row["id"],
        phone_number=row["phone_number"],
        reseller_id=row.get("merchant_id") or row.get("reseller_id"),
        reason=row.get("reason"),
        created_by=row.get("created_by"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_blacklisted_number_list(
    result: List[asyncpg.Record],
) -> List[BlacklistedNumber]:
    """
    Decode a list of blacklisted number records.
    """
    if not result:
        return []

    decoded = [decode_blacklisted_number(row) for row in result]
    return [item for item in decoded if item is not None]
