"""Broadcast admission (canon/06-outreach.md:41-43, T18) — broadcasts phase
3. Every recipient a broadcast froze ends one of two ways: a run of its
workflow, stamped source_broadcast_id, or a skipped row with its reason
(canon T18 col 9). The broadcast never sends; the run does.

One page at a time, at the broadcaster's own pace — no throttle here: the
walker and the dialler own their limits, and a broadcast is not shaped
around them.
- claim: up to ADMIT_PAGE queued rows -> claimed (SKIP LOCKED);
- gather: the workflow once, the page's customers in ONE identity read
  (and the survivors of any merged since the freeze), the merchant's
  merges once;
- decide (PURE): per person, the run to start or the skip word;
- apply: ONE atom per person — the guards, the run and the row's end share
  one fate (enrol.enrol_step), so a crash never leaves a run without
  "enrolled", or "enrolled" without a run;
- the first page stamps dispatched_at ("fired"); when nothing waits, sent.

Who a broadcast run is for: her surviving record when she was merged after
the freeze (canon/03-permission.md, the decision log's customer_id: "on a
merge: the survivor"); her
current phone and her name in the run's context — the facts a call or a
send may use, since a broadcast has no founding event. A paused workflow
holds the queue, as the walker holds its runs; an archived one admits
nobody. A paused broadcast (phase 4) stops after the person in flight; the
rest of the page goes back to the queue.
"""

import os
import random
import socket
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any, Dict, List, NamedTuple, Optional

from app.core.logger import logger
from app.crm.identity.contracts import get_customers, merged_pairs
from app.crm.outreach.db import DbTxn, UniqueViolation, atomically
from app.crm.outreach.db.accessors import (
    broadcast as broadcast_accessor,
    broadcast_recipient as recipient_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.enrol import enrol_step
from app.crm.outreach.schemas import (
    Broadcast,
    Workflow,
    WorkflowDefinition,
    WorkflowEntryAt,
)

LOG_COMPONENT = "crm.outreach.admission"

#: One admission page: the rows one claim takes, one atom per person after.
ADMIT_PAGE = 1000

#: How long a claimed row may wait before the reaper puts it back (canon T18
#: col 11). Far above a page's worst time, so only a dead pod's rows return.
ADMIT_LEASE_MINUTES = 10

#: Mass writes must never come due the same second (canon T20 col 8): each
#: admitted run's first wake is spread over this many seconds.
ADMIT_JITTER_SECONDS = 60

#: Which worker holds a claimed row (canon T18 col 10): the pod, the process.
WORKER = f"{socket.gethostname()}:{os.getpid()}"

# The skip words (canon T18 col 9: "chiefly the workflow's guards:
# already_in_flow (reenter) · cooldown_active, plus resolve failures").
# Vocabulary lives in code, never in a CHECK; the column's CHECK is on its
# presence only.
ALREADY_IN_FLOW = "already_in_flow"
COOLDOWN_ACTIVE = "cooldown_active"
NO_PHONE = "no_phone"
CUSTOMER_GONE = "customer_gone"
KEY_REQUIRED = "key_required"
WORKFLOW_NOT_LIVE = "workflow_not_live"
ADMISSION_FAILED = "admission_failed"
SKIP_REASONS = (
    ALREADY_IN_FLOW,
    COOLDOWN_ACTIVE,
    NO_PHONE,
    CUSTOMER_GONE,
    KEY_REQUIRED,
    WORKFLOW_NOT_LIVE,
    ADMISSION_FAILED,
)

# enrol's refusal words (logged by the event door, unchanged) -> the row's.
_SKIP_FOR = {
    "reenter_disabled": ALREADY_IN_FLOW,
    "open_run_exists": ALREADY_IN_FLOW,
    "source_event_replayed": ALREADY_IN_FLOW,
    "cooldown_active": COOLDOWN_ACTIVE,
}

# The squares that dial or message her: both read context.phone
# (nodes/call.py, nodes/send.py) and park a run that has none.
_PHONE_SQUARES = ("call", "send")


class Admission(NamedTuple):
    """One person's fate, decided before any write."""

    customer_id: str  # the recipient row's customer, as frozen
    person_id: Optional[str]  # who the run is for: the survivor if merged
    context: Dict[str, Any]
    skip: Optional[str]


class _RowReleased(Exception):
    """The reaper put this row back while the atom ran: its run must not
    commit, or the row and the run would disagree."""


def needs_phone(definition: WorkflowDefinition) -> bool:
    """PURE: does the plan dial or message her anywhere?"""
    return any(node.type in _PHONE_SQUARES for node in definition.nodes)


def decide_admissions(
    customer_ids: List[str],
    customers: Dict[str, Any],
    *,
    phone_needed: bool,
    keyed: bool,
) -> List[Admission]:
    """PURE decide: per recipient, the person and the facts her run starts
    with, or why nobody can be started. ``customers`` holds the page's
    customers AND the survivors of any merged since the freeze (one hop:
    identity path-compresses its merges)."""
    plans: List[Admission] = []
    for customer_id in customer_ids:
        if keyed:
            # Republished "one run per <field>" after the schedule refused
            # it: a broadcast has no such field to give.
            plans.append(Admission(customer_id, None, {}, KEY_REQUIRED))
            continue
        customer = customers.get(customer_id)
        if customer is not None and customer.status == "merged_away":
            survivor = customer.merged_into_id
            customer = customers.get(str(survivor)) if survivor else None
        if customer is None or customer.status != "active":
            plans.append(Admission(customer_id, None, {}, CUSTOMER_GONE))
            continue
        context: Dict[str, Any] = {}
        if customer.phone:
            context["phone"] = customer.phone
        elif phone_needed:
            plans.append(Admission(customer_id, None, {}, NO_PHONE))
            continue
        if customer.display_name:
            context["customer_name"] = customer.display_name
        plans.append(Admission(customer_id, str(customer.id), context, None))
    return plans


async def admission_candidates(batch: int) -> List[Broadcast]:
    """Broadcasts with admission work: dispatching, workflow not paused, a
    queued row to admit or nothing left waiting (then: sent)."""
    return await broadcast_accessor.admission_candidates(batch)


async def admit_page(broadcast: Broadcast) -> None:
    """The broadcaster's admission job: one page of a dispatching
    broadcast's queue into its workflow. A short page (or none) is the last:
    the broadcast is then sent once nothing waits."""
    merchant_id, broadcast_id = broadcast.merchant_id, str(broadcast.id)
    log = logger.bind(
        component=LOG_COMPONENT, merchant_id=merchant_id, broadcast_id=broadcast_id
    )
    claimed = await recipient_accessor.claim_page(broadcast_id, WORKER, ADMIT_PAGE)
    if not claimed:
        await _finish(broadcast, log)
        return
    workflow = await workflow_accessor.get_workflow(
        merchant_id, str(broadcast.workflow_id)
    )
    if workflow is not None and workflow.status == "paused":
        # Held, not skipped: the plan was paused after the claim. The
        # candidates read leaves this broadcast alone until it is live.
        await recipient_accessor.release_claims(broadcast_id, claimed)
        log.info(f"admission held: workflow paused ({len(claimed)} rows queued again)")
        return
    await broadcast_accessor.mark_fired(merchant_id, broadcast_id)
    definition: Optional[WorkflowDefinition] = None
    if workflow is None or workflow.status != "live" or not workflow.definition:
        plans = [Admission(c, None, {}, WORKFLOW_NOT_LIVE) for c in claimed]
    else:
        definition = WorkflowDefinition.model_validate(workflow.definition)
        plans = decide_admissions(
            claimed,
            await _customers(merchant_id, claimed),
            phone_needed=needs_phone(definition),
            keyed=bool(definition.entries[0].key),
        )
    losers_of: Dict[str, List[str]] = defaultdict(list)
    if any(plan.person_id for plan in plans):
        for loser, survivor in (await merged_pairs(merchant_id)).items():
            losers_of[survivor].append(loser)
    outcomes: Counter = Counter()
    held: List[str] = []
    for plan in plans:
        records = (
            [plan.person_id, *sorted(losers_of[plan.person_id])]
            if plan.person_id
            else []
        )
        outcome = await _admit_one(broadcast, workflow, definition, plan, records)
        outcomes[outcome] += 1
        if outcome == "held":
            held.append(plan.customer_id)
    if held:
        # Paused mid-page (phase 4): the rest of the page goes back to the
        # queue now, not after the reaper's lease, so a resume finds it.
        await recipient_accessor.release_claims(broadcast_id, held)
    log.bind(admitted=outcomes.get("enrolled", 0), **dict(outcomes)).info(
        f"admitted a page of {len(claimed)}: {dict(outcomes)}"
    )
    if len(claimed) < ADMIT_PAGE:
        await _finish(broadcast, log)


async def _admit_one(
    broadcast: Broadcast,
    workflow: Optional[Workflow],
    definition: Optional[WorkflowDefinition],
    plan: Admission,
    records: List[str],
) -> str:
    """One person, one atom; the outcome word for the page's count."""
    jitter = timedelta(seconds=random.uniform(0, ADMIT_JITTER_SECONDS))
    try:
        return await atomically(
            _admit_one_in_txn, broadcast, workflow, definition, plan, records, jitter
        )
    except _RowReleased:
        return "released"
    except UniqueViolation:
        # The open-run unique raced her: a run of hers began between the
        # guard's read and this insert. The atom rolled back; say so.
        await atomically(_settle_in_txn, broadcast, plan.customer_id, ALREADY_IN_FLOW)
        return ALREADY_IN_FLOW
    except Exception as e:
        # A poison row is skipped with its reason, never retried forever: the
        # reaper only returns rows a dead pod left claimed.
        logger.bind(
            component=LOG_COMPONENT,
            merchant_id=broadcast.merchant_id,
            broadcast_id=str(broadcast.id),
            customer_id=plan.customer_id,
        ).error(f"admission failed: {type(e).__name__}: {e}")
        await atomically(_settle_in_txn, broadcast, plan.customer_id, ADMISSION_FAILED)
        return ADMISSION_FAILED


async def _admit_one_in_txn(
    txn: DbTxn,
    broadcast: Broadcast,
    workflow: Optional[Workflow],
    definition: Optional[WorkflowDefinition],
    plan: Admission,
    records: List[str],
    jitter: timedelta,
) -> str:
    """ATOMIC: her run and her recipient row's end share one fate — a crash
    leaves the row claimed (the reaper queues it again) and no run; never a
    run without "enrolled", never "enrolled" without a run. The broadcast is
    held FOR SHARE: a cancel waits for this person and the next one sees it."""
    merchant_id, broadcast_id = broadcast.merchant_id, str(broadcast.id)
    status = await broadcast_accessor.share_lock_broadcast(
        txn, merchant_id, broadcast_id
    )
    if status == "cancelled":
        await _settle(txn, broadcast_id, plan.customer_id, "cancelled", None)
        return "cancelled"
    if status != "dispatching":
        return "held"  # paused: admit_page puts the row back in the queue
    if plan.skip or workflow is None or definition is None or plan.person_id is None:
        word = plan.skip or WORKFLOW_NOT_LIVE
        await _settle(txn, broadcast_id, plan.customer_id, "skipped", word)
        return word
    door: WorkflowEntryAt = definition.entries[0]
    run, reason = await enrol_step(
        txn,
        merchant_id=merchant_id,
        workflow=workflow,
        definition=definition,
        door=door,
        customer_id=plan.person_id,
        context=plan.context,
        enrollment_key=plan.person_id,
        records=records,
        source_broadcast_id=broadcast_id,
        wake_jitter=jitter,
    )
    if run is None:
        word = _SKIP_FOR.get(reason, reason)
        await _settle(txn, broadcast_id, plan.customer_id, "skipped", word)
        return word
    await _settle(txn, broadcast_id, plan.customer_id, "enrolled", None)
    return "enrolled"


async def _settle_in_txn(
    txn: DbTxn, broadcast: Broadcast, customer_id: str, reason: str
) -> None:
    """ATOMIC: a row skipped after its own atom failed — the broadcast row
    held FOR SHARE as every admission write is, so a cancel still orders
    against it."""
    status = await broadcast_accessor.share_lock_broadcast(
        txn, broadcast.merchant_id, str(broadcast.id)
    )
    if status == "dispatching":
        await recipient_accessor.settle_recipient(
            txn, str(broadcast.id), customer_id, "skipped", reason
        )


async def _settle(
    txn: DbTxn,
    broadcast_id: str,
    customer_id: str,
    status: str,
    reason: Optional[str],
) -> None:
    """The row's end inside the person's atom. A row the reaper already
    released is not ours: the whole atom rolls back, run included."""
    if not await recipient_accessor.settle_recipient(
        txn, broadcast_id, customer_id, status, reason
    ):
        raise _RowReleased(customer_id)


async def _customers(merchant_id: str, customer_ids: List[str]) -> Dict[str, Any]:
    """GATHER: the page's customers in one identity read, plus the survivors
    of any merged since the freeze — a second read only when there are."""
    customers: Dict[str, Any] = dict(await get_customers(merchant_id, customer_ids))
    survivors = sorted(
        {
            str(c.merged_into_id)
            for c in customers.values()
            if c.status == "merged_away" and c.merged_into_id
        }
        - set(customers)
    )
    if survivors:
        customers.update(await get_customers(merchant_id, survivors))
    return customers


async def _finish(broadcast: Broadcast, log: Any) -> None:
    sent = await broadcast_accessor.finish_dispatch(
        broadcast.merchant_id, str(broadcast.id)
    )
    if sent is not None:
        log.info("admission over: the broadcast is sent")


async def release_stale_claims() -> int:
    """The reaper's pass (canon T18 col 11): rows a dead pod left claimed
    go back to the queue after ADMIT_LEASE_MINUTES."""
    released = await recipient_accessor.reap_stale_claims(ADMIT_LEASE_MINUTES)
    if released:
        logger.bind(component=LOG_COMPONENT, released=released).warning(
            f"admission reaper: {released} stale claims queued again"
        )
    return released
