"""Record accessor — mechanical DB access ONLY (module rules §1).

Executes exactly one query builder per function. Envelope semantics,
fail postures and serialization decisions live in the logic file
(ingest.py).
"""

from datetime import datetime
from typing import Optional

from app.crm.record.db.queries import insert_event_query
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
