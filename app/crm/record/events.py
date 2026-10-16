"""Attributed-event reads — owned by the record module, consumed by the
outreach walker (the goal re-check at fire time).

Trivial today — one query, one decode, no decisions — but contracts.py
re-exports from here rather than db/accessor directly (the timeline.py
seam): cross-module callers never depend on a mechanical accessor
signature.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.crm.record.db import accessor
from app.crm.record.schemas import RawEvent
from app.crm.record.workers import variables_for

# Our own call report (crm_mirror): source telephony, keyed by the topic-
# qualified natural id (the lead's call_id, else its id) — dedupe is
# (merchant_id, source, external_id), topic deliberately not in it.
CALL_REPORT_SOURCE = "telephony"
CALL_COMPLETED_TOPIC = "call.completed"


def call_report_key(natural_id: str) -> str:
    """PURE: the external_id a call's call.completed is stored under."""
    return f"{CALL_COMPLETED_TOPIC}:{natural_id}"


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


async def call_report(
    merchant_id: str, natural_id: str
) -> Optional[Tuple[RawEvent, Dict[str, Any]]]:
    """A finished call's call.completed as its consumers hear it — the stored
    letter and the variables they are handed — or None while it is not yet
    recorded (the mirror writes it after the post-call outcome check)."""
    event = await accessor.event_by_key(
        merchant_id, CALL_REPORT_SOURCE, call_report_key(natural_id)
    )
    if event is None:
        return None
    variables = await variables_for(event)
    if variables is None:
        return None
    return event, variables
