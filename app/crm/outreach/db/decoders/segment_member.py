"""row -> schema translation for crm_segment_member (T21) — one table, one
file (module rules §1 at scale). DB-side translation only — never imported
outside db/.
"""

from typing import Any, Mapping

from app.crm.outreach.schemas import SegmentMember


def decode_segment_member(row: Mapping[str, Any]) -> SegmentMember:
    return SegmentMember(
        customer_id=row["customer_id"],
        added_at=row["added_at"],
        source=row["source"],
    )
