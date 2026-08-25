"""Row -> domain shapes for the record module (module rules §1). DB-side
translation only — never imported outside db/.

Only the call arm exists today (canon: call · chat · message · consent ·
event), so this is one decode function. When a second arm lands, this
becomes a switch on source_kind — not before.
"""

from typing import Any, Mapping

from app.crm.record.schemas import JourneyCard


def decode_journey_card(row: Mapping[str, Any]) -> JourneyCard:
    return JourneyCard(
        id=row["id"],
        merchant_id=row["merchant_id"],
        customer_id=row["customer_id"],
        channel=row["channel"],
        direction=row["direction"],
        handled_by=row["handled_by"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        outcome=row["outcome"],
        recording_ref=row["recording_ref"],
        transcript_ref=row["transcript_ref"],
        source_kind=row["source_kind"],
    )
