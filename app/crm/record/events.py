"""Attributed-event reads — owned by the record module, consumed by the
outreach walker (the goal re-check at fire time).

Trivial today — one query, one decode, no decisions — but contracts.py
re-exports from here rather than db/accessor directly (the timeline.py
seam): cross-module callers never depend on a mechanical accessor
signature.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.crm.record.db import accessor


async def customer_has_event(
    merchant_id: str,
    customer_id: str,
    topics: List[str],
    since: datetime,
    where: Optional[Tuple[str, str]] = None,
) -> bool:
    """Did she do one of ``topics`` after ``since``? ``where`` narrows it
    to the letters whose payload field equals a value (the goal key)."""
    return await accessor.customer_has_event(
        merchant_id, customer_id, topics, since, where
    )


async def event_topics(merchant_id: str, event_ids: List[str]) -> Dict[str, str]:
    """{letter id: topic} for a few ids a run's trail points at — so the
    console can say WHICH event moved a square without outreach reading
    record's table (rule 12's one direction)."""
    return await accessor.event_topics(merchant_id, event_ids)
