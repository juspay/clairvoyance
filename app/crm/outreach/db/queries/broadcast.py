"""SQL builders for crm_broadcast (T17, canon/06-outreach.md) — one table,
one file. $1 placeholders only; merchant_id is $1 on every read and write.
"""

from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import BROADCAST_TABLE

_BROADCAST_COLUMNS = """
    id, merchant_id, name, campaign_key, segment_id, workflow_id, status,
    scheduled_for, resolved_at, dispatched_at, closed_at, created_by, created_at
"""


def insert_broadcast_query(
    merchant_id: str,
    name: str,
    segment_id: str,
    workflow_id: str,
    campaign_key: Optional[str],
    created_by: Optional[str],
) -> Tuple[str, List[Any]]:
    """A broadcast is born a draft: nothing scheduled, nothing frozen — the
    clocks after created_at stay NULL until the launch (phase 2) sets them."""
    query = f"""
        INSERT INTO {BROADCAST_TABLE}
            (merchant_id, name, segment_id, workflow_id, campaign_key, created_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, name, segment_id, workflow_id, campaign_key, created_by]


def get_broadcast_query(merchant_id: str, broadcast_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_BROADCAST_COLUMNS}
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 AND id = $2
    """
    return query, [merchant_id, broadcast_id]


def list_broadcasts_query(
    merchant_id: str, campaign_key: Optional[str], limit: int, offset: int
) -> Tuple[str, List[Any]]:
    """Newest first — crm_broadcast_merchant_created_ix, or with a badge the
    campaign-level read on crm_broadcast_campaign_ix."""
    values: List[Any] = [merchant_id]
    badge = ""
    if campaign_key is not None:
        values.append(campaign_key)
        badge = "AND campaign_key = $2"
    values += [limit, offset]
    query = f"""
        SELECT {_BROADCAST_COLUMNS}
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 {badge}
        ORDER BY created_at DESC, id DESC
        LIMIT ${len(values) - 1} OFFSET ${len(values)}
    """
    return query, values


def count_broadcasts_query(
    merchant_id: str, campaign_key: Optional[str]
) -> Tuple[str, List[Any]]:
    """The list's total, under the same badge filter (the RunPage ruling)."""
    if campaign_key is None:
        return (
            f"SELECT count(*)::int AS total FROM {BROADCAST_TABLE} WHERE merchant_id = $1",
            [merchant_id],
        )
    query = f"""
        SELECT count(*)::int AS total
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 AND campaign_key = $2
    """
    return query, [merchant_id, campaign_key]
