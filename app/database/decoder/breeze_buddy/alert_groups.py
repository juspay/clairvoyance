"""Decoders for alert_groups rows."""

import json
from typing import Optional

from app.schemas.breeze_buddy.alerts import AlertGroup


def decode_alert_group(row) -> Optional[AlertGroup]:
    """Build AlertGroup from database row, or None if row is empty."""
    if not row:
        return None
    members = row["members"]
    if isinstance(members, str):
        members = json.loads(members)
    return AlertGroup(
        id=str(row["id"]),
        name=row["name"],
        reseller_id=row["reseller_id"],
        members=members,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
