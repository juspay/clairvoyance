"""
Database query functions for alert_groups table.
"""

import json
from typing import Any, Dict, List, Tuple

ALERT_GROUPS_TABLE = "alert_groups"


def get_alert_group_by_name_query(name: str, reseller_id: str) -> Tuple[str, List[Any]]:
    """Get alert group by name scoped to a reseller."""
    text = f"""
        SELECT "id", "name", "reseller_id", "members", "created_at", "updated_at"
        FROM "{ALERT_GROUPS_TABLE}"
        WHERE "name" = $1 AND "reseller_id" = $2;
    """
    values: List[Any] = [name, reseller_id]
    return text, values


def upsert_alert_group_query(
    name: str, reseller_id: str, members: List[Dict[str, str]]
) -> Tuple[str, List[Any]]:
    """Create or update an alert group scoped to a reseller."""
    text = f"""
        INSERT INTO "{ALERT_GROUPS_TABLE}" ("name", "reseller_id", "members", "updated_at")
        VALUES ($1, $2, $3::jsonb, NOW())
        ON CONFLICT ("name", "reseller_id") DO UPDATE
            SET "members" = EXCLUDED."members",
                "updated_at" = NOW()
        RETURNING "id", "name", "reseller_id", "members", "created_at", "updated_at";
    """
    values: List[Any] = [name, reseller_id, json.dumps(members)]
    return text, values
