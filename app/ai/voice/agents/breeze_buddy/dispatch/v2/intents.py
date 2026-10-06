"""The dialler's side of the call-queue hooks (app/core/call_queue.py).

The CRM may not import the dialler, so it asks through that registry: queue a workflow
call that has no lead row yet, take it back, or change its rank. Importing this module
registers the three functions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import v2_seen
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import (
    Enqueue,
    Rank,
    rank_from_priority,
)
from app.core import call_queue
from app.core.call_queue import CallRequest


def _ms(when: Optional[datetime]) -> int:
    return int(when.timestamp() * 1000) if when else 0


def _rank(
    rank: int,
    order: str,
    event_at: Optional[datetime],
    next_rank: Optional[int] = None,
    next_order: Optional[str] = None,
) -> Rank:
    return rank_from_priority(
        {
            "rank": rank,
            "order": order,
            "event_ms": _ms(event_at),
            "next_rank": next_rank,
            "next_order": next_order,
        }
    )


async def queue(req: CallRequest) -> Optional[bool]:
    """True = waiting for a line; False = failed; None = this number takes no call
    without a lead row (not on v2, or not an intents number): insert the lead as today.
    """
    if not await v2_seen():
        return None

    async def enqueue() -> Optional[int]:
        return await scripts.enqueue(
            req.template_id,
            req.lead_id,
            _ms(req.ready_at),
            rank=_rank(
                req.rank, req.order, req.event_at, req.next_rank, req.next_order
            ),
            run_id=req.run_id,
        )

    issued = await enqueue()
    if issued == Enqueue.ROUTE_MISSING:
        # lazy: routes -> managers.calls -> dispatch (import cycle)
        from app.ai.voice.agents.breeze_buddy.dispatch.v2.routes import ensure_route

        await ensure_route(req.template_id)
        issued = await enqueue()
    if issued is None:
        return False
    if issued >= 0 or issued == Enqueue.HOLDS_LINE:
        return True
    return None


async def withdraw(template_id: str, lead_id: str) -> None:
    await scripts.withdraw(template_id, lead_id)


async def rerank(
    template_id: str,
    lead_id: str,
    rank: int,
    order: str,
    event_at: Optional[datetime],
    next_rank: Optional[int] = None,
    next_order: Optional[str] = None,
) -> None:
    await scripts.rerank(
        template_id, lead_id, _rank(rank, order, event_at, next_rank, next_order)
    )


call_queue.register(queue, withdraw, rerank)
