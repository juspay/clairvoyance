"""Attributed-event reads — owned by the record module, consumed by the
outreach walker (the goal re-check at fire time).

Trivial today — one query, one decode, no decisions — but contracts.py
re-exports from here rather than db/accessor directly (the timeline.py
seam): cross-module callers never depend on a mechanical accessor
signature.
"""

from datetime import datetime
from typing import List

from app.crm.record.db import accessor


async def customer_has_event(
    merchant_id: str, customer_id: str, topics: List[str], since: datetime
) -> bool:
    return await accessor.customer_has_event(merchant_id, customer_id, topics, since)
