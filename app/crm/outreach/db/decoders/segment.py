"""row -> schema translation for crm_segment (T15) — one table, one file
(module rules §1 at scale). DB-side translation only — never imported
outside db/.
"""

from typing import Any, Mapping

from app.crm.outreach.schemas import Segment
from app.crm.shared.decode import jsonb_value


def decode_segment(row: Mapping[str, Any]) -> Segment:
    definition = jsonb_value(row["definition"])
    return Segment(
        id=row["id"],
        merchant_id=row["merchant_id"],
        name=row["name"],
        # The kind is a predicate over definition, never a stored column
        # (law 10): NULL = a static list whose members are rows.
        kind="static" if definition is None else "dynamic",
        definition=definition,
        member_count=row["member_count"],
        computed_at=row["computed_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
