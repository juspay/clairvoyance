"""Event-spine ingest logic — owned by the record module (A8).

BUSINESS LOGIC ONLY — DB mechanics live in accessor.py. Store first,
understand later: one INSERT with the envelope stamped and the payload
verbatim. Dedupe is the UNIQUE (merchant_id, source, external_id) — a
duplicate is a silent no-op, exactly as the front-door law wants.
``customer_id`` is a pass-through for producers that already resolved the
handle (the voice mirror stamps at write); left None, the row sits on the
pending queue (processed_at IS NULL) for the consumer to stamp (ADR 0020).

Two callers, two fail postures (module rules §6):

- ``ingest_event`` — the push door (A9). Raises on store failure so the
  API can 503 and the producer retries; returns None only on dedupe.
- ``record_event`` — buddy-side mirrors. Fire-and-forget: recording a
  fact must never break the operation that produced it, so it logs and
  returns None on failure instead of raising.
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.crm.record.db import accessor


async def ingest_event(
    *,
    merchant_id: str,
    source: str,
    topic: str,
    external_id: str,
    payload: Dict[str, Any],
    occurred_at: Optional[datetime] = None,
    schema_version: str = "1",
    customer_id: Optional[str] = None,
) -> Optional[str]:
    """Store one letter, honestly: the new event id, None on duplicate,
    a raised exception when the store failed."""
    return await accessor.insert_event(
        merchant_id,
        source,
        topic,
        external_id,
        json.dumps(payload, default=str),
        schema_version,
        occurred_at,
        customer_id,
    )


async def record_event(
    *,
    merchant_id: str,
    source: str,
    topic: str,
    external_id: str,
    payload: Dict[str, Any],
    occurred_at: Optional[datetime] = None,
    schema_version: str = "1",
    customer_id: Optional[str] = None,
) -> Optional[str]:
    """Record one fact into the event spine. Returns the new event id,
    or None on duplicate / failure."""
    try:
        return await ingest_event(
            merchant_id=merchant_id,
            source=source,
            topic=topic,
            external_id=external_id,
            payload=payload,
            occurred_at=occurred_at,
            schema_version=schema_version,
            customer_id=customer_id,
        )
    except Exception as e:
        logger.error(
            f"event spine ingest failed for {source}/{topic} "
            f"external_id={external_id}: {e}"
        )
        return None
