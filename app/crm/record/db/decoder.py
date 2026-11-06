"""Row -> domain shapes for the record module (module rules §1). DB-side
translation only — never imported outside db/.

Only the call arm exists today (canon: call · chat · message · consent ·
event), so this is one decode function. When a second arm lands, this
becomes a switch on source_kind — not before.
"""

import json
from typing import Any, Mapping

from app.crm.record.schemas import CatalogField, EventSchema, JourneyCard, RawEvent


def decode_raw_event(row: Mapping[str, Any]) -> RawEvent:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return RawEvent(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        source=row["source"],
        topic=row["topic"],
        schema_version=row["schema_version"],
        external_id=row["external_id"],
        payload=payload,
        received_at=row["received_at"],
        occurred_at=row["occurred_at"],
        customer_id=str(row["customer_id"]) if row["customer_id"] else None,
        attempts=int(row["attempts"]),
    )


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


def decode_event_schema(row: Mapping[str, Any]) -> EventSchema:
    fields = row["fields"]
    if isinstance(fields, str):
        fields = json.loads(fields)
    return EventSchema(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        source=row["source"],
        topic=row["topic"],
        label=row["label"],
        fields=[CatalogField.model_validate(f) for f in (fields or [])],
        status=row["status"],
        version=row["version"],
        registered_by=row["registered_by"],
        first_seen_at=row["first_seen_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
