"""Attributed-event reads — owned by the record module, consumed by the
outreach walker (the goal re-check at fire time).

Trivial today — one query, one decode, no decisions — but contracts.py
re-exports from here rather than db/accessor directly (the timeline.py
seam): cross-module callers never depend on a mechanical accessor
signature.
"""

from datetime import datetime
from typing import List, Optional, Tuple

from app.crm.record.db import accessor
from app.crm.record.schemas import RawEvent


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


async def customer_goal_events(
    merchant_id: str,
    customer_id: str,
    topics: List[str],
    since: datetime,
    where: Optional[Tuple[str, str]] = None,
    limit: int = 50,
) -> List[RawEvent]:
    """The letters that EXISTS would have answered for, newest first and
    capped — so a caller whose question SQL cannot express (a goal tier's
    `where`) asks it of the payloads with its own evaluator."""
    return await accessor.customer_goal_events(
        merchant_id, customer_id, topics, since, where, limit
    )
