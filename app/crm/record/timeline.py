"""Journey read logic — owned by the record module (A12, module rules §1).

Trivial today — one query, one decode, no decisions — but contracts.py
re-exports from here rather than db/accessor directly, same seam every
other module's contract keeps, so cross-module callers never depend on a
mechanical accessor signature.
"""

from datetime import datetime
from typing import List, Optional

from app.crm.record.db import accessor
from app.crm.record.schemas import JourneyCard


async def get_customer_journey(
    merchant_id: str,
    customer_id: str,
    limit: int = 50,
    before_started_at: Optional[datetime] = None,
    before_id: Optional[str] = None,
) -> List[JourneyCard]:
    return await accessor.get_customer_journey(
        merchant_id, customer_id, limit, before_started_at, before_id
    )
