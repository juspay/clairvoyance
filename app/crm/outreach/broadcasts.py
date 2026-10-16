"""Broadcasts (canon T17 + T18, canon/06-outreach.md) — the launch button. A
broadcast never sends (the workflow does) and never owns the workflow (ten
broadcasts may launch the same one).

Phase 1 — the draft: gather (the segment, the workflow, the badge — each this
merchant's?) -> decide (PURE: every problem at once) -> apply (insert). The
tenant-pinned FKs (088) refuse the same things at the table, so even a
delete racing between the gather and the insert cannot cross merchants.

Phase 2 — the launch and the freeze:
- schedule aims a draft at a moment (or now); the workflow must be live.
- At that moment the broadcaster loop (walker role, worker_main) claims it
  (scheduled -> resolving, resolved_at = now: the moment the audience is
  asked — deliberately late, so Friday's send is aimed with Friday's aim)
  and freezes the segment into recipient rows, one page per atom; then
  resolving -> dispatching. Phase 3 admits the queue into the workflow.
- resolved_at is also the freeze's lease: a freeze whose worker died is
  claimed again once it is FREEZE_LEASE_MINUTES old, and the re-run is a
  no-op for everyone already queued (the recipients' PK).
- A segment that cannot be answered fails the broadcast (canon: stuck
  resolving is a segment problem).
- cancel stops future admissions: the broadcast and its still-queued rows,
  one atom; runs already started resolve by their own rules.
"""

from datetime import datetime, timezone
from typing import List, Optional

from app.core.logger import logger
from app.crm.outreach.campaigns import campaign_key, display_name
from app.crm.outreach.db import DbTxn, atomically
from app.crm.outreach.db.accessors import (
    broadcast as broadcast_accessor,
    broadcast_recipient as recipient_accessor,
    campaign as campaign_accessor,
    segment as segment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.schemas import Broadcast, BroadcastPage, Campaign, Segment
from app.crm.outreach.segments import (
    SegmentNotFound,
    SegmentRuleInvalid,
    segment_member_ids,
)
from app.crm.record.contracts import MAX_MATCHING_PAGE

LOG_COMPONENT = "crm.outreach.broadcasts"

#: One freeze page: as many people as one segment page carries.
FREEZE_PAGE = MAX_MATCHING_PAGE

#: How long a freeze may run before another worker may take it over. A
#: freeze writes one page per atom, seconds each, so even a lakh-strong
#: audience finishes well inside it; a takeover only repeats queued rows,
#: which the PK absorbs.
FREEZE_LEASE_MINUTES = 15

#: The statuses a broadcast never leaves.
FINISHED = ("sent", "closed", "cancelled", "failed")


class BroadcastNotFound(Exception):
    """This merchant has no broadcast with this id."""


class BroadcastStateConflict(Exception):
    """The broadcast is past the point this action needs (``status`` says
    where it is)."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


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


def schedule_problems(
    workflow_status: Optional[str],
    scheduled_for: Optional[datetime],
    now: datetime,
) -> List[str]:
    """PURE decide: every reason the broadcast cannot be aimed. The workflow
    must be live at launch (a draft or paused plan admits nobody); a moment
    needs its timezone and may not be in the past — none means now."""
    problems: List[str] = []
    if workflow_status != "live":
        problems.append(
            f"workflow_id: the workflow is {workflow_status or 'gone'} — only a "
            "live workflow can be launched"
        )
    if scheduled_for is not None:
        if scheduled_for.tzinfo is None:
            problems.append(
                "scheduled_for: needs a timezone (2026-10-17T10:00:00+05:30)"
            )
        elif scheduled_for < now:
            problems.append(
                "scheduled_for: is in the past — leave it out to launch now"
            )
    return problems


async def schedule_broadcast(
    merchant_id: str, broadcast_id: str, scheduled_for: Optional[datetime]
) -> Broadcast:
    """Aim a draft (or re-aim a scheduled broadcast) at scheduled_for, or
    now. Nothing is frozen here: the broadcaster loop does that at the
    moment. Raises BroadcastNotFound, BroadcastStateConflict (already
    launched or finished) or BroadcastInvalid (with every problem)."""
    broadcast = await broadcast_accessor.get_broadcast(merchant_id, broadcast_id)
    if broadcast is None:
        raise BroadcastNotFound(broadcast_id)
    if broadcast.status not in ("draft", "scheduled"):
        raise BroadcastStateConflict(broadcast.status)
    workflow_status = await workflow_accessor.workflow_status(
        merchant_id, str(broadcast.workflow_id)
    )
    problems = schedule_problems(
        workflow_status, scheduled_for, datetime.now(timezone.utc)
    )
    if problems:
        raise BroadcastInvalid(problems)
    scheduled = await broadcast_accessor.schedule_broadcast(
        merchant_id, broadcast_id, scheduled_for
    )
    if scheduled is not None:
        return scheduled
    # Launched or cancelled between the read and the update: say where it is.
    current = await broadcast_accessor.get_broadcast(merchant_id, broadcast_id)
    if current is None:
        raise BroadcastNotFound(broadcast_id)
    raise BroadcastStateConflict(current.status)


async def cancel_broadcast(merchant_id: str, broadcast_id: str) -> Broadcast:
    """Stop the broadcast admitting anyone else. Runs it already started
    resolve by their own rules (canon). Raises BroadcastNotFound or
    BroadcastStateConflict (already finished)."""
    return await atomically(_cancel_in_txn, merchant_id, broadcast_id)


async def _cancel_in_txn(txn: DbTxn, merchant_id: str, broadcast_id: str) -> Broadcast:
    """ATOMIC: the broadcast's status and its queue share one fate — a
    cancelled broadcast never keeps a queued row a later admission could
    take. The row lock also orders the cancel against a freeze page: the
    page waiting on it then finds the broadcast cancelled and writes
    nothing."""
    status = await broadcast_accessor.lock_broadcast(txn, merchant_id, broadcast_id)
    if status is None:
        raise BroadcastNotFound(broadcast_id)
    if status in FINISHED:
        raise BroadcastStateConflict(status)
    await recipient_accessor.cancel_queued_recipients(txn, broadcast_id)
    return await broadcast_accessor.cancel_broadcast(txn, merchant_id, broadcast_id)


async def claim_due_freezes(batch: int) -> List[Broadcast]:
    """The broadcaster loop's claim (worker_main, walker role): broadcasts
    whose moment has come, and freezes whose worker died — each moved to
    resolving with resolved_at = now in one statement that is also the
    lock (SKIP LOCKED)."""
    return await broadcast_accessor.claim_due_freezes(batch, FREEZE_LEASE_MINUTES)


async def freeze_broadcast(broadcast: Broadcast) -> None:
    """The broadcaster loop's handler: read the segment ONCE, page by page,
    into queued recipient rows; then resolving -> dispatching. A segment
    that cannot be answered fails the broadcast. A cancel that lands
    mid-freeze stops it at the next page."""
    merchant_id, broadcast_id = broadcast.merchant_id, str(broadcast.id)
    log = logger.bind(
        component=LOG_COMPONENT, merchant_id=merchant_id, broadcast_id=broadcast_id
    )
    after: Optional[str] = None
    queued = 0
    while True:
        try:
            customer_ids = await segment_member_ids(
                merchant_id, str(broadcast.segment_id), after, FREEZE_PAGE
            )
        except (SegmentNotFound, SegmentRuleInvalid) as e:
            await broadcast_accessor.fail_freeze(merchant_id, broadcast_id)
            log.error(f"freeze failed: the segment cannot be answered ({e})")
            return
        if customer_ids:
            added = await atomically(
                _freeze_page_in_txn, merchant_id, broadcast_id, customer_ids
            )
            if added is None:
                log.info("freeze stopped: the broadcast is no longer resolving")
                return
            queued += added
        if len(customer_ids) < FREEZE_PAGE:
            break
        after = customer_ids[-1]
    finished = await broadcast_accessor.finish_freeze(merchant_id, broadcast_id)
    log.bind(queued=queued).info(
        f"froze {queued} recipients — "
        f"{finished.status if finished else 'no longer resolving'}"
    )


async def _freeze_page_in_txn(
    txn: DbTxn, merchant_id: str, broadcast_id: str, customer_ids: List[str]
) -> Optional[int]:
    """ATOMIC: a freeze page and the broadcast's status share one fate — the
    page lands only while the broadcast is still resolving. A cancel that
    holds the lock first wins, and this page writes nothing (None)."""
    status = await broadcast_accessor.lock_broadcast(txn, merchant_id, broadcast_id)
    if status != "resolving":
        return None
    return await recipient_accessor.insert_recipients(txn, broadcast_id, customer_ids)
