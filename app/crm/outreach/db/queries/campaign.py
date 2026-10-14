"""SQL builders for crm_campaign (T22, canon/06-outreach.md) — one table, one
file. $1 placeholders only; merchant_id is $1 on every read and write.
"""

from datetime import date
from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import CAMPAIGN_TABLE

_CAMPAIGN_COLUMNS = "merchant_id, key, name, starts_at, ends_at, created_by, created_at"


def insert_campaign_query(
    merchant_id: str,
    key: str,
    name: str,
    starts_at: Optional[date],
    ends_at: Optional[date],
    created_by: Optional[str],
) -> Tuple[str, List[Any]]:
    """A new badge. No ON CONFLICT: a key the merchant already has is the
    primary key's refusal to surface, never a merge."""
    query = f"""
        INSERT INTO {CAMPAIGN_TABLE}
            (merchant_id, key, name, starts_at, ends_at, created_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_CAMPAIGN_COLUMNS}
    """
    return query, [merchant_id, key, name, starts_at, ends_at, created_by]


def get_campaign_query(merchant_id: str, key: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_CAMPAIGN_COLUMNS}
        FROM {CAMPAIGN_TABLE}
        WHERE merchant_id = $1 AND key = $2
    """
    return query, [merchant_id, key]


def list_campaigns_query(merchant_id: str, limit: int) -> Tuple[str, List[Any]]:
    """Newest first. A merchant has a handful of seasons, not thousands: one
    page, read off the primary key's merchant prefix."""
    query = f"""
        SELECT {_CAMPAIGN_COLUMNS}
        FROM {CAMPAIGN_TABLE}
        WHERE merchant_id = $1
        ORDER BY created_at DESC, key
        LIMIT $2
    """
    return query, [merchant_id, limit]
