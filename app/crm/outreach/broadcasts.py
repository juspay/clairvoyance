"""Broadcasts (canon T17 + T18, canon/06-outreach.md) — the launch button. A
broadcast never sends (the workflow does) and never owns the workflow (ten
broadcasts may launch the same one).

Phase 1 — the draft: gather (the segment, the workflow, the badge — each this
merchant's?) -> decide (PURE: every problem at once) -> apply (insert). The
tenant-pinned FKs (089) refuse the same things at the table, so even a
delete racing between the gather and the insert cannot cross merchants.

Phase 2 — the launch and the freeze:
- schedule aims a draft at a moment (or now); the workflow must be live.
- At that moment the broadcaster (its own role, worker_main) claims it
  (scheduled -> resolving, resolved_at = now, one attempt spent) and
  freezes the segment into recipient rows, one page per atom, each page
  renewing resolved_at; then resolving -> dispatching. resolved_at ends at
  the last page — when the audience was frozen (canon T17 col 14),
  deliberately late, so Friday's send is aimed with Friday's aim.
  dispatched_at stays NULL: "fired" is the first admission, phase 3's.
- resolved_at is also the freeze's lease: a freeze whose worker died is
  claimed again once its last page is FREEZE_LEASE_MINUTES old, and resumes
  after the last id already queued (the recipients' PK absorbs any
  overlap).
- A segment that cannot be answered fails the broadcast (canon: stuck
  resolving is a segment problem). Any other error is kept as last_error
  and retried at the next claim; the error of the last allowed attempt
  (CRM_BROADCAST_FREEZE_MAX_ATTEMPTS) fails the broadcast, with the reason.
  A failed broadcast cancels its queued rows in the same atom — it will
  never dispatch, and a row dies at a guard or becomes a run (canon T18).
  A freeze that finishes clears attempts and last_error: a retry that
  succeeded leaves nothing to show.
- cancel stops future admissions: the broadcast and its still-queued rows,
  one atom; runs already started resolve by their own rules.

Phase 3 — admission (admission.py, run by the same broadcaster loop): every
queued recipient becomes a run stamped source_broadcast_id, or a skipped row
with its reason; then dispatching -> sent. The schedule refuses what
admission could only skip or park: a keyed first door, a fact a broadcast
run never carries (catalog_laws.broadcast_problems).

Phase 4 — pause, resume, close (and the report, broadcast_report.py):
- pause holds a dispatching broadcast's queue: admission stops after the
  person in flight, the rest stay queued; resume picks up where it stopped.
  Runs already started are not touched — the broadcast never owns them.
- close: once the reporting window ends the funnel is final (canon T17 col
  11). The window is the last run's whole life (the workflow's
  max_age_days) after it entered, plus CLOSE_GRACE for the last call
  reports — the corpus's "fired Friday 10:00, final Sunday" (T17 col 18) is
  a time after the fire, never "when every run has ended": a one-call plan's
  runs end when the call is QUEUED, and one parked run would hold it open
  forever. The broadcaster's loop runs the close job beside its reaper.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from app.core.config.static import CRM_BROADCAST_FREEZE_MAX_ATTEMPTS
from app.core.logger import logger
from app.crm.outreach.campaigns import campaign_key, display_name
from app.crm.outreach.catalog_laws import (
    WorkflowValidationError,
    broadcast_problems,
    gather_catalogs,
)
from app.crm.outreach.db import DbTxn, atomically
from app.crm.outreach.db.accessors import (
    broadcast as broadcast_accessor,
    broadcast_recipient as recipient_accessor,
    campaign as campaign_accessor,
    enrollment as enrollment_accessor,
    segment as segment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.schemas import (
    Broadcast,
    BroadcastPage,
    Campaign,
    Segment,
    WorkflowDefinition,
    WorkflowExits,
)
from app.crm.outreach.segments import (
    SegmentNotFound,
    SegmentRuleInvalid,
    segment_member_ids,
)
from app.crm.record.contracts import MAX_MATCHING_PAGE

LOG_COMPONENT = "crm.outreach.broadcasts"

#: One freeze page: as many people as one segment page carries.
FREEZE_PAGE = MAX_MATCHING_PAGE

#: How long a freeze may go without landing a page before another worker
#: may take it over. Every page renews it and takes seconds, so only a dead
#: (or stuck) worker's freeze is taken over; the takeover resumes after the
#: last queued id, and the PK absorbs any overlap.
FREEZE_LEASE_MINUTES = 15

#: A launch moment this far in the past is still taken as now: a client
#: that sends "now" reaches us after its own request latency. The claim
#: takes such a moment at once.
SCHEDULE_GRACE = timedelta(seconds=60)

#: How much of an error a broadcast keeps in last_error: enough to name it,
#: never a whole traceback.
LAST_ERROR_CHARS = 500

#: The statuses a broadcast never leaves.
FINISHED = ("sent", "closed", "cancelled", "failed")

#: After the last run's whole life, how long the report stays open for the
#: last call reports before it goes final (phase 4, the D2 ruling).
CLOSE_GRACE = timedelta(days=1)

#: How many sent broadcasts one close pass looks at. Sent broadcasts are a
#: merchant's deliberate launches, closed within days, so this is far above
#: what is ever waiting.
CLOSE_BATCH = 500


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
    needs its timezone and may not be in the past beyond SCHEDULE_GRACE —
    none means now."""
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
        elif scheduled_for < now - SCHEDULE_GRACE:
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
    workflow = await workflow_accessor.get_workflow(
        merchant_id, str(broadcast.workflow_id)
    )
    problems = schedule_problems(
        workflow.status if workflow else None,
        scheduled_for,
        datetime.now(timezone.utc),
    )
    if workflow is not None and workflow.status == "live" and workflow.definition:
        # Phase 3 admits through the plan's first door with no founding
        # event: a keyed door, or a fact only an event would carry, would
        # skip or park everyone — refused now, with every reason.
        try:
            catalogs = await gather_catalogs(merchant_id, workflow.definition)
        except WorkflowValidationError as e:
            problems.extend(e.problems)
        else:
            problems.extend(
                broadcast_problems(
                    WorkflowDefinition.model_validate(workflow.definition), catalogs
                )
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
    """The broadcaster's claim (worker_main, broadcaster role): broadcasts
    whose moment has come, and freezes whose worker died — each moved to
    resolving with resolved_at = now and one attempt spent, in one statement
    that is also the lock (SKIP LOCKED)."""
    return await broadcast_accessor.claim_due_freezes(batch, FREEZE_LEASE_MINUTES)


async def freeze_broadcast(broadcast: Broadcast) -> None:
    """The broadcaster's handler: read the segment ONCE, page by page, into
    queued recipient rows, resuming after the last id already frozen; then
    resolving -> dispatching. A segment that cannot be answered fails the
    broadcast with its reason. Any other error is kept as last_error and
    raised: the broadcast stays resolving, and the next claim retries it once
    the lease runs out — unless this was the last allowed attempt
    (CRM_BROADCAST_FREEZE_MAX_ATTEMPTS), which fails it at once. A failure
    cancels the queued rows with it. A cancel that lands mid-freeze stops it
    at the next page.

    A dynamic segment's pages come from record's contract, which reads the
    READ REPLICA — fine here: the freeze only reads ids and writes them with
    ON CONFLICT DO NOTHING, an audience a few seconds behind is the design
    (frozen just before dispatch), and nothing acts twice on a stale row."""
    merchant_id, broadcast_id = broadcast.merchant_id, str(broadcast.id)
    log = logger.bind(
        component=LOG_COMPONENT,
        merchant_id=merchant_id,
        broadcast_id=broadcast_id,
        attempts=broadcast.attempts,
    )
    if broadcast.attempts > CRM_BROADCAST_FREEZE_MAX_ATTEMPTS:
        # A worker died mid-attempt (nothing caught the error): the claim
        # spent one more than allowed. The last error caught, if any, says why.
        await _fail(
            log,
            merchant_id,
            broadcast_id,
            f"gave up after {CRM_BROADCAST_FREEZE_MAX_ATTEMPTS} attempts: "
            f"{broadcast.last_error or 'no error recorded'}",
        )
        return
    queued = 0
    try:
        after = await recipient_accessor.last_frozen_customer(merchant_id, broadcast_id)
        while True:
            customer_ids = await segment_member_ids(
                merchant_id, str(broadcast.segment_id), after, FREEZE_PAGE
            )
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
    except (SegmentNotFound, SegmentRuleInvalid) as e:
        await _fail(
            log, merchant_id, broadcast_id, f"the segment cannot be answered: {e}"
        )
        return
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        if broadcast.attempts >= CRM_BROADCAST_FREEZE_MAX_ATTEMPTS:
            # The last allowed attempt: fail now, not one lease later.
            await _fail(
                log,
                merchant_id,
                broadcast_id,
                f"gave up after {broadcast.attempts} attempts: {error}",
            )
            return
        # Its own statement, outside the atom that broke, so the reason
        # survives the rollback. Raised on: the scaffold logs it.
        await broadcast_accessor.record_freeze_error(
            merchant_id, broadcast_id, error[:LAST_ERROR_CHARS]
        )
        raise
    log.bind(queued=queued).info(
        f"queued {queued} recipients — "
        f"{finished.status if finished else 'no longer resolving'}"
    )


async def _freeze_page_in_txn(
    txn: DbTxn, merchant_id: str, broadcast_id: str, customer_ids: List[str]
) -> Optional[int]:
    """ATOMIC: a freeze page, the broadcast's status and its lease share one
    fate — the page lands only while the broadcast is still resolving, and
    the lease moves on only with a page that landed. A cancel that holds the
    lock first wins, and this page writes nothing (None)."""
    status = await broadcast_accessor.lock_broadcast(txn, merchant_id, broadcast_id)
    if status != "resolving":
        return None
    added = await recipient_accessor.insert_recipients(txn, broadcast_id, customer_ids)
    await broadcast_accessor.renew_freeze_lease(txn, merchant_id, broadcast_id)
    return added


async def _fail(log: Any, merchant_id: str, broadcast_id: str, reason: str) -> None:
    """The freeze cannot finish: resolving -> failed with its reason, and
    its queued rows cancelled with it."""
    failed = await atomically(
        _fail_freeze_in_txn, merchant_id, broadcast_id, reason[:LAST_ERROR_CHARS]
    )
    if failed is None:
        log.info("freeze not failed: the broadcast is no longer resolving")
        return
    log.error(f"freeze failed: {reason}")


async def _fail_freeze_in_txn(
    txn: DbTxn, merchant_id: str, broadcast_id: str, reason: str
) -> Optional[Broadcast]:
    """ATOMIC: a failed broadcast and its queue share one fate — it will
    never dispatch, so it keeps no queued row a later admission could take
    (canon T18: a row dies at a guard or becomes a run). Only from
    resolving: a cancel that holds the lock first keeps its word (None)."""
    status = await broadcast_accessor.lock_broadcast(txn, merchant_id, broadcast_id)
    if status != "resolving":
        return None
    await recipient_accessor.cancel_queued_recipients(txn, broadcast_id)
    return await broadcast_accessor.fail_freeze(txn, merchant_id, broadcast_id, reason)


async def pause_broadcast(merchant_id: str, broadcast_id: str) -> Broadcast:
    """Hold a dispatching broadcast's queue: the person being admitted
    finishes, everyone after waits, queued. Runs it already started carry on
    by their own rules. Raises BroadcastNotFound or BroadcastStateConflict
    (not dispatching — a scheduled one is re-aimed or cancelled instead)."""
    return await _move(merchant_id, broadcast_id, "dispatching", "paused")


async def resume_broadcast(merchant_id: str, broadcast_id: str) -> Broadcast:
    """Admit the rest of a paused broadcast's queue, from where it stopped.
    Raises BroadcastNotFound or BroadcastStateConflict (not paused)."""
    return await _move(merchant_id, broadcast_id, "paused", "dispatching")


async def _move(
    merchant_id: str, broadcast_id: str, from_status: str, to_status: str
) -> Broadcast:
    moved = await broadcast_accessor.move_broadcast(
        merchant_id, broadcast_id, from_status, to_status
    )
    if moved is not None:
        logger.bind(
            component=LOG_COMPONENT, merchant_id=merchant_id, broadcast_id=broadcast_id
        ).info(f"broadcast {from_status} -> {to_status}")
        return moved
    # Not in from_status (or not this merchant's): say where it is.
    current = await broadcast_accessor.get_broadcast(merchant_id, broadcast_id)
    if current is None:
        raise BroadcastNotFound(broadcast_id)
    raise BroadcastStateConflict(current.status)


def report_window_end(
    last_entered_at: Optional[datetime], max_age_days: float
) -> Optional[datetime]:
    """PURE: when the broadcast's numbers go final — its last run's whole
    life after that run entered, plus CLOSE_GRACE. None when it started no
    run: there is nothing to wait for."""
    if last_entered_at is None:
        return None
    return last_entered_at + timedelta(days=max_age_days) + CLOSE_GRACE


async def max_age_days(broadcast: Broadcast) -> float:
    """The run life of the broadcast's workflow, as published now — the
    model's default when it is gone or has no document.
    ponytail: the latest version's, not each run's pinned one; a republish
    that shortens max_age_days can close a report a little early."""
    workflow = await workflow_accessor.get_workflow(
        broadcast.merchant_id, str(broadcast.workflow_id)
    )
    if workflow is None or not workflow.definition:
        return WorkflowExits().max_age_days
    return WorkflowDefinition.model_validate(workflow.definition).exits.max_age_days


async def close_due_broadcasts() -> int:
    """The close job (the broadcaster's loop, beside the reaper): every sent
    broadcast whose reporting window has ended -> closed. A cheap check on
    the first fire before the exact one on the last run's entry: the last
    entry is never before the first fire, so a window not over from the fire
    is not over at all. One that admitted nobody closes at once. A broadcast
    the job cannot judge is logged and left sent: housekeeping must never
    stop the loop's freezes and admissions."""
    now = datetime.now(timezone.utc)
    closed = 0
    for broadcast in await broadcast_accessor.close_candidates(CLOSE_BATCH):
        try:
            closed += await _close_if_due(broadcast, now)
        except Exception as e:
            logger.bind(
                component=LOG_COMPONENT,
                merchant_id=broadcast.merchant_id,
                broadcast_id=str(broadcast.id),
            ).error(f"close check failed: {type(e).__name__}: {e}")
    return closed


async def _close_if_due(broadcast: Broadcast, now: datetime) -> int:
    """1 when this broadcast's window has ended and it is now closed."""
    merchant_id, broadcast_id = broadcast.merchant_id, str(broadcast.id)
    if broadcast.dispatched_at is not None:
        age = await max_age_days(broadcast)
        soonest = broadcast.dispatched_at + timedelta(days=age) + CLOSE_GRACE
        if soonest > now:
            return 0
        last = await enrollment_accessor.last_broadcast_entry(merchant_id, broadcast_id)
        if (report_window_end(last, age) or soonest) > now:
            return 0
    if await broadcast_accessor.close_broadcast(merchant_id, broadcast_id) is None:
        return 0  # no longer sent: another pod closed it first
    logger.bind(
        component=LOG_COMPONENT, merchant_id=merchant_id, broadcast_id=broadcast_id
    ).info("broadcast closed: the reporting window ended")
    return 1
