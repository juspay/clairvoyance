"""The dialler hears the CRM's waiting-call facts (outreach/waiting_calls.py).

The CRM may not import the dialler, so it states its facts — a workflow call with no
lead row waits, left, or was re-ranked — and this module, like crm_mirror, registers
through the CRM's contract to act on them. Importing it registers the three functions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.ai.voice.agents.breeze_buddy.dispatch.v2 import routes, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import v2_seen
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import (
    Enqueue,
    Rank,
    rank_from_priority,
)
from app.crm.outreach.contracts import WaitingCall, register_waiting_call_hooks


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


async def queue(req: WaitingCall) -> Optional[bool]:
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
        await routes.ensure_route(req.template_id)
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


register_waiting_call_hooks(queue, withdraw, rerank)
