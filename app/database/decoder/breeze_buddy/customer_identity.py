"""Decoder for customer_identity rows."""

from typing import List, Optional

import asyncpg

from app.schemas.breeze_buddy.memory import CustomerIdentity


def decode_customer_identity(row: asyncpg.Record) -> Optional[CustomerIdentity]:
    if not row:
        return None
    return CustomerIdentity(
        id=row["id"],
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        phone=row["phone"],
        customer_id=row["customer_id"],
        status=row.get("status") or "ACTIVE",
        conflicting_customer_id=row.get("conflicting_customer_id"),
        conflicted_at=row.get("conflicted_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_customer_identity_list(
    rows: List[asyncpg.Record],
) -> List[CustomerIdentity]:
    if not rows:
        return []
    decoded = [decode_customer_identity(r) for r in rows]
    return [c for c in decoded if c is not None]
