"""Record accessor — mechanical DB access ONLY (module rules §1).

Executes exactly one query builder per function. Envelope semantics,
fail postures and serialization decisions live in the logic file
(ingest.py).
"""

from datetime import datetime
from typing import List, Optional

from app.crm.record.db.decoder import decode_journey_card
from app.crm.record.db.queries import get_customer_journey_query, insert_event_query
from app.crm.record.schemas import JourneyCard
from app.crm.shared.db import crm_connection


async def insert_event(
    merchant_id: str,
    source: str,
    topic: str,
    external_id: str,
    payload_json: str,
    schema_version: str,
    occurred_at: Optional[datetime],
    customer_id: Optional[str],
) -> Optional[str]:
    """One INSERT — atomic on its own; a dedupe conflict returns None."""
    query, values = insert_event_query(
        merchant_id,
        source,
        topic,
        external_id,
        payload_json,
        schema_version,
        occurred_at,
        customer_id,
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return str(row["id"]) if row else None


async def get_customer_journey(
    merchant_id: str,
    customer_id: str,
    limit: int = 50,
    before_started_at: Optional[datetime] = None,
    before_id: Optional[str] = None,
) -> List[JourneyCard]:
    query, values = get_customer_journey_query(
        merchant_id, customer_id, limit, before_started_at, before_id
    )
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_journey_card(row) for row in rows]
