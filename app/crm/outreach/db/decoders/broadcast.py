"""row -> schema translation for crm_broadcast (T17) — one table, one file.
DB-side translation only — never imported outside db/.
"""

from typing import Any, Mapping

from app.crm.outreach.schemas import Broadcast


def decode_broadcast(row: Mapping[str, Any]) -> Broadcast:
    return Broadcast(
        id=row["id"],
        merchant_id=row["merchant_id"],
        name=row["name"],
        campaign_key=row["campaign_key"],
        segment_id=row["segment_id"],
        workflow_id=row["workflow_id"],
        status=row["status"],
        scheduled_for=row["scheduled_for"],
        resolved_at=row["resolved_at"],
        dispatched_at=row["dispatched_at"],
        closed_at=row["closed_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )
