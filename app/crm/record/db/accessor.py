"""Record accessor — mechanical DB access ONLY (module rules §1).

Executes exactly one query builder per function. Envelope semantics,
fail postures and serialization decisions live in the logic files
(ingest.py, workers.py). A ``conn`` param runs inside the caller's atom;
``txn`` is reserved for the _in_txn bodies that own a boundary.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.record.db import DbTxn
from app.crm.record.db.decoder import (
    decode_event_schema,
    decode_journey_card,
    decode_raw_event,
)
from app.crm.record.db.queries import (
    claim_pending_events_query,
    customer_has_event_query,
    get_customer_journey_query,
    get_schema_query,
    insert_detected_schema_query,
    insert_event_query,
    list_schemas_query,
    quarantine_event_query,
    register_schema_query,
    sample_fields_query,
    stamp_event_query,
    topic_counts_query,
)
from app.crm.record.schemas import EventSchema, JourneyCard, RawEvent, TopicCount
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


async def claim_pending_events(conn: DbTxn, limit: int) -> List[RawEvent]:
    """FOR UPDATE SKIP LOCKED inside the caller's transaction — the lock is
    the claim, held for the life of that transaction."""
    query, values = claim_pending_events_query(limit)
    rows = await conn.fetch(query, *values)
    return [decode_raw_event(row) for row in rows]


async def stamp_event(conn: DbTxn, event_id: str, customer_id: Optional[str]) -> None:
    query, values = stamp_event_query(event_id, customer_id)
    await conn.execute(query, *values)


async def quarantine_event(conn: DbTxn, event_id: str, reason: str) -> None:
    query, values = quarantine_event_query(event_id, reason)
    await conn.execute(query, *values)


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


async def customer_has_event(
    merchant_id: str,
    customer_id: str,
    topics: List[str],
    since: datetime,
    where: Optional[Tuple[str, str]] = None,
) -> bool:
    query, values = customer_has_event_query(
        merchant_id, customer_id, topics, since, where
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return bool(row["found"]) if row else False


# --- crm_event_schema (T24) ---------------------------------------------------


async def insert_detected_schema(
    conn: DbTxn, merchant_id: str, source: str, topic: str
) -> None:
    """Inside the pass's row savepoint — the nudge row shares fate with the
    row that revealed the topic."""
    query, values = insert_detected_schema_query(merchant_id, source, topic)
    await conn.execute(query, *values)


async def register_schema(
    merchant_id: str,
    source: str,
    topic: str,
    label: str,
    fields_json: str,
    registered_by: str,
) -> EventSchema:
    query, values = register_schema_query(
        merchant_id, source, topic, label, fields_json, registered_by
    )
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    if row is None:
        raise RuntimeError("schema registration returned no row")
    return decode_event_schema(row)


async def list_schemas(merchant_id: str) -> List[EventSchema]:
    query, values = list_schemas_query(merchant_id)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [decode_event_schema(row) for row in rows]


async def get_schema(
    merchant_id: str, source: str, topic: str
) -> Optional[EventSchema]:
    query, values = get_schema_query(merchant_id, source, topic)
    async with crm_connection() as conn:
        row = await conn.fetchrow(query, *values)
    return decode_event_schema(row) if row is not None else None


async def topic_counts(merchant_id: str, days: int) -> List[TopicCount]:
    query, values = topic_counts_query(merchant_id, days)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    return [
        TopicCount(source=r["source"], topic=r["topic"], seen=r["seen"]) for r in rows
    ]


async def sample_fields(
    merchant_id: str, source: str, topic: str, limit: int
) -> List[Dict[str, Any]]:
    """Raw {path, jtype, seen, samples} rows; catalog.py guesses the type."""
    query, values = sample_fields_query(merchant_id, source, topic, limit)
    async with crm_connection() as conn:
        rows = await conn.fetch(query, *values)
    out: List[Dict[str, Any]] = []
    for r in rows:
        samples = []
        for text in r["samples"] or []:
            try:
                samples.append(json.loads(text))
            except (TypeError, ValueError):
                samples.append(text)
        out.append(
            {
                "path": r["path"],
                "jtype": r["jtype"],
                "seen": r["seen"],
                "samples": samples,
            }
        )
    return out
