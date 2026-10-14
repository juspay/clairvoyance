"""Broadcasts (canon T17, canon/06-outreach.md) — the launch button. Phase 1:
the broadcast row — create a draft, read one, list them. The launch (phase
2) freezes the segment late and admits it into the workflow; a broadcast
never sends (the workflow does) and never owns the workflow (ten broadcasts
may launch the same one).

gather (the segment, the workflow, the badge — each this merchant's?) ->
decide (PURE: every problem at once) -> apply (insert a draft). The
tenant-pinned FKs (089) refuse the same things at the table, so even a
delete racing between the gather and the insert cannot cross merchants.
"""

from typing import List, Optional

from app.crm.outreach.campaigns import campaign_key, display_name
from app.crm.outreach.db.accessors import (
    broadcast as broadcast_accessor,
    campaign as campaign_accessor,
    segment as segment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.schemas import Broadcast, BroadcastPage, Campaign, Segment


class BroadcastInvalid(Exception):
    """The draft names something this merchant does not have; ``problems``
    names each one."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def draft_problems(
    segment: Optional[Segment],
    workflow_status: Optional[str],
    badge: Optional[str],
    campaign: Optional[Campaign],
) -> List[str]:
    """PURE decide: every reason the draft cannot be stored. A workflow in
    any live-able state may be named — the launch (phase 2) is where it must
    be live; an archived one admits nobody, ever, so it is refused now."""
    problems: List[str] = []
    if segment is None:
        problems.append("segment_id: this merchant has no such segment")
    if workflow_status is None:
        problems.append("workflow_id: this merchant has no such workflow")
    elif workflow_status == "archived":
        problems.append("workflow_id: the workflow is archived — it admits nobody")
    if badge is not None and campaign is None:
        problems.append(
            f"campaign_key: {badge!r} is not one of this merchant's campaigns — "
            "create it first (POST /campaigns)"
        )
    return problems


async def create_broadcast(
    merchant_id: str,
    name: str,
    segment_id: str,
    workflow_id: str,
    campaign: Optional[str],
    created_by: Optional[str],
) -> Broadcast:
    """A new draft broadcast: the segment to freeze, the workflow to admit
    it into, and an optional badge. Raises ValueError (blank name, a
    malformed key) or BroadcastInvalid (with every problem)."""
    stored_name = display_name(name, "broadcast")
    badge = campaign_key(campaign) if campaign is not None else None
    segment = await segment_accessor.get_segment(merchant_id, segment_id)
    workflow_status = await workflow_accessor.workflow_status(merchant_id, workflow_id)
    found = await campaign_accessor.get_campaign(merchant_id, badge) if badge else None
    problems = draft_problems(segment, workflow_status, badge, found)
    if problems:
        raise BroadcastInvalid(problems)
    return await broadcast_accessor.insert_broadcast(
        merchant_id, stored_name, segment_id, workflow_id, badge, created_by
    )


async def get_broadcast(merchant_id: str, broadcast_id: str) -> Optional[Broadcast]:
    """One broadcast, or None when this merchant has no such broadcast —
    another merchant's id answers None too, never their row."""
    return await broadcast_accessor.get_broadcast(merchant_id, broadcast_id)


async def list_broadcasts(
    merchant_id: str, campaign: Optional[str], limit: int, offset: int
) -> BroadcastPage:
    """One page of the merchant's broadcasts, newest first, and the total —
    one season's when a badge is named."""
    badge = campaign_key(campaign) if campaign is not None else None
    return BroadcastPage(
        items=await broadcast_accessor.list_broadcasts(
            merchant_id, badge, limit, offset
        ),
        total=await broadcast_accessor.count_broadcasts(merchant_id, badge),
    )
