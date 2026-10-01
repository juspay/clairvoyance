"""Journey read logic — owned by the record module (A12, module rules §1).

One query, one decode, then the call cards' outcome layers read from the
lead beside the view (the view keeps canon's 12 columns). contracts.py
re-exports from here rather than db/accessor directly, same seam every
other module's contract keeps, so cross-module callers never depend on a
mechanical accessor signature.
"""

from datetime import datetime
from typing import Dict, List, Optional

from app.core.logger import logger
from app.crm.record.db import accessor
from app.crm.record.schemas import JourneyCard
from app.database.accessor import get_call_outcome_columns

CALL_SOURCE_KIND = "call"


async def get_customer_journey(
    merchant_id: str,
    customer_id: str,
    limit: int = 50,
    before_started_at: Optional[datetime] = None,
    before_id: Optional[str] = None,
) -> List[JourneyCard]:
    cards = await accessor.get_customer_journey(
        merchant_id, customer_id, limit, before_started_at, before_id
    )
    return await with_call_outcomes(merchant_id, cards)


async def with_call_outcomes(
    merchant_id: str, cards: List[JourneyCard]
) -> List[JourneyCard]:
    """Fill each call card's outcome layers from its lead. Fail-open: the
    fields are additive, so a failed read returns the cards as they were."""
    lead_ids = [c.id for c in cards if c.source_kind == CALL_SOURCE_KIND]
    if not lead_ids:
        return cards
    try:
        columns: Dict[str, Dict[str, Optional[str]]] = await get_call_outcome_columns(
            merchant_id, lead_ids
        )
    except Exception:
        logger.opt(exception=True).warning(
            "journey: call outcome columns unreadable — cards returned without them"
        )
        return cards
    return [
        (
            card.model_copy(update=columns[card.id])
            if card.source_kind == CALL_SOURCE_KIND and card.id in columns
            else card
        )
        for card in cards
    ]
