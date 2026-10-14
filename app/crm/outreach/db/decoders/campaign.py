"""row -> schema translation for crm_campaign (T22) — one table, one file.
DB-side translation only — never imported outside db/.
"""

from typing import Any, Mapping

from app.crm.outreach.schemas import Campaign


def decode_campaign(row: Mapping[str, Any]) -> Campaign:
    return Campaign(
        merchant_id=row["merchant_id"],
        key=row["key"],
        name=row["name"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )
