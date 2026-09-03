"""The walker (W3) — the clock that moves tokens. NOT an engine (canon:
"wake_at + the document IS the engine"): claim due runs off the partial
index, read the plan the run ENTERED UNDER, execute the current node,
write the next alarm. Correctness rides the wake_at lease + idempotent
writes, never worker uniqueness — scale is replicas.

Which document (ADR 0023, rollout phase 12): the run's own pin —
crm_workflow_enrollment.workflow_version names the crm_workflow_version
row it executes (definitions.py resolves and caches it), so a run
finishes on the version it entered under while new entrants take the
newest; `on_publish: migrate` re-pins open runs inside the publish atom
instead. The live row is still read, for its STATUS only (archived
ejects, paused snoozes); its definition column is never what a run
executes.

The node vocabulary — what each square does when the token lands, and
whether landing means waiting — lives in nodes.py (NODE_TYPES); the walker
dispatches through it and never matches a type string.

The goal is re-checked at every claim BEFORE acting (canon: never "did
you forget?" to someone who just paid) — the entry processor's
goal-cancel is the fast path; this is the belt-and-suspenders.
"""

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config.static import (
    CRM_WALKER_LEASE_SECONDS,
    CRM_WALKER_MAX_ATTEMPTS,
)
from app.core.logger import logger
from app.crm.outreach.db import accessor
from app.crm.outreach.definitions import definition_for
from app.crm.outreach.entry import reply_key
from app.crm.outreach.nodes import NODE_TYPES, NodeParked, is_wait
from app.crm.outreach.plans import TIMEOUT
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.crm.record.contracts import customer_has_event

# One claim executes consecutive immediate nodes (call -> next wait) in a
# single visit; the bound is a runaway-document guard, not a feature.
_MAX_STEPS_PER_VISIT = 10

# Transient-failure retry: exponential from the lease, capped, ±20% jitter
# (canon T20: "backoff with jitter written into wake_at").
_RETRY_CAP_SECONDS = 3600


def retry_delay_seconds(attempts: int, base: int) -> int:
    """PURE: how long the next retry waits. attempts already counts this
    claim, so the first retry waits one base."""
    delay = min(base * 2 ** max(attempts - 1, 0), _RETRY_CAP_SECONDS)
    return max(1, round(delay * random.uniform(0.8, 1.2)))


async def claim_due_runs(batch: int) -> List[EnrollmentRun]:
    """The walker's claim for the drain loop (worker-runtime.md): the
    wake_at lease push — one UPDATE that moves the alarm one lease window
    forward IS the lock, so replicas never collide and a crashed claim
    self-heals when the pushed alarm comes due again."""
    return await accessor.claim_due_runs(batch, CRM_WALKER_LEASE_SECONDS)


async def walk_run(run: EnrollmentRun) -> None:
    """Move one claimed token as far as it can go this visit. The claim
    already pushed wake_at one lease window, so every failure path below
    retries by simply doing nothing — the clock brings the run back.

    Every write this visit makes is conditional on that lease (P1,
    rollout phase 03): the claim's wake_at is the generation token, and
    every event-side writer (a reply, a repeat patch, a goal-cancel)
    moves it. A miss means the run changed under us — defer: the lease
    already re-arms the run, and the next claim re-reads it WITH the
    reply and takes the right branch. Action nodes are idempotent
    (dedupe run:node, uuid5 lead), so a re-executed visit is exactly as
    safe as the lease retry this file already relied on."""
    lease = run.wake_at
    if lease is None:
        # A claimed run always carries its lease (the claim wrote it, and
        # waiting rows have wake_at NOT NULL). Anything else is a caller
        # bug — and a write without a token would be a blind overwrite.
        logger.error(f"walker: run {run.id} claimed without a lease — skipping")
        return
    try:
        workflow = await accessor.get_workflow(run.merchant_id, str(run.workflow_id))
        if workflow is None or workflow.status == "archived":
            if not await accessor.exit_run(str(run.id), "ejected", lease):
                _deferred(run, "eject")
            return
        if workflow.status == "paused":
            return  # the lease push IS the snooze; re-checked next wake
        definition = await definition_for(run)
        if definition is None:
            # No version row for the pin: an honest park — never a fallback
            # to the live document, which would execute a plan the run did
            # not enter under (versions are never deleted, ADR 0023 §5, so
            # this is drift, not life).
            raise NodeParked(f"definition v{run.workflow_version} missing")
        await _advance(run, definition, lease)
    except NodeParked as e:
        if await accessor.park_run(str(run.id), str(e), lease):
            logger.warning(f"walker: run {run.id} parked — {e}")
        else:
            _deferred(run, "park")
    except Exception as e:
        if run.attempts >= CRM_WALKER_MAX_ATTEMPTS:
            if await accessor.park_run(str(run.id), f"attempts exhausted: {e}", lease):
                logger.error(f"walker: run {run.id} parked after retries — {e}")
            else:
                _deferred(run, "park")
        else:
            retry_in = retry_delay_seconds(run.attempts, CRM_WALKER_LEASE_SECONDS)
            if await accessor.record_run_error(str(run.id), str(e), retry_in, lease):
                logger.warning(f"walker: run {run.id} retries in {retry_in}s — {e}")
            else:
                _deferred(run, "retry")


def _deferred(run: EnrollmentRun, write: str) -> None:
    """A CAS miss: the run moved under the lease (a reply or repeat landed
    mid-visit). Nothing to undo — the event side's alarm stands and the
    next claim re-reads the run as it now is."""
    logger.info(
        f"walker: run {run.id} changed under the lease ({write} skipped) — "
        f"deferring to the next wake"
    )


async def _advance(
    run: EnrollmentRun, definition: WorkflowDefinition, lease: datetime
) -> None:
    nodes = {node.id: node for node in definition.nodes}
    outgoing = definition.outgoing()
    now = datetime.now(timezone.utc)

    # The hard ceiling first: a run older than the plan's max age exits
    # timed_out no matter which square it stands on.
    max_age = timedelta(days=definition.exits.max_age_days)
    if now - run.entered_at > max_age:
        if not await accessor.exit_run(str(run.id), "timed_out", lease):
            _deferred(run, "timed_out")
        return

    # Goal re-check at fire time — one indexed EXISTS per tier via
    # record's contract, never a foreign SELECT. Tiers are judged
    # keyed-first (goal_tiers, phase 06): "THIS cart recovered" beats
    # "she bought something", and the run exits with the tier's reason.
    # Measured from the founding letter's own time (G7), not the row's.
    since = goal_since(run)
    for tier in definition.goal_tiers():
        where: Optional[Tuple[str, str]] = None
        if tier.key:
            value = run.context.get(tier.key.run)
            if value in (None, ""):
                continue  # this run cannot match a keyed tier
            where = (tier.key.event, str(value))
        if await customer_has_event(
            run.merchant_id, str(run.customer_id), tier.topics, since, where
        ):
            if not await accessor.exit_run(str(run.id), tier.exit_reason, lease):
                _deferred(run, tier.exit_reason)
            return

    current_id = run.current_node
    context = dict(run.context)
    for _ in range(_MAX_STEPS_PER_VISIT):
        node = nodes.get(current_id)
        if node is None:
            # A pinned version never loses a node under a run (pin), and
            # the migrate validator forbids stranding — so this is drift
            # (e.g. a run parked across an archive/re-create): honest park.
            raise NodeParked(
                f"node {current_id} not in definition v{run.workflow_version}"
            )

        execute = NODE_TYPES[node.type].execute
        if execute is not None:  # a wait's action IS the alarm
            context.update(await execute(run, node, definition))

        next_id = pick_next(node, outgoing.get(current_id, []), context)
        if next_id is None:
            if not await accessor.exit_run(
                str(run.id),
                "completed",
                lease,
                current_node=current_id,
                context=context,
            ):
                _deferred(run, "completed")
            return

        next_node = nodes.get(next_id)
        if next_node is None:
            raise NodeParked(f"edge points at unknown node {next_id}")
        if is_wait(next_node) and next_node.minutes:
            # Arrival scheduling: the wait's alarm starts now.
            if not await accessor.advance_run(
                str(run.id),
                next_id,
                datetime.now(timezone.utc) + timedelta(minutes=next_node.minutes),
                context,
                lease,
            ):
                _deferred(run, f"advance to {next_id}")
            return
        current_id = next_id  # action node: execute in this same visit

    raise NodeParked(
        f"{_MAX_STEPS_PER_VISIT} immediate nodes in one visit — runaway document"
    )


def goal_since(run: EnrollmentRun) -> datetime:
    """PURE: the moment "after the run began" is measured from — the
    founding letter's own time (entered_event_at, stamped by entry.py:
    G7), else the row's insert time for runs written before the stamp
    existed. Total: an unreadable stamp falls back rather than failing
    the visit."""
    stamp = run.context.get("entered_event_at")
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            return run.entered_at
        if parsed.tzinfo is not None:
            return parsed
    return run.entered_at


def pick_next(
    node: WorkflowNode, arrows: List[Tuple[str, Optional[str]]], context: Dict[str, Any]
) -> Optional[str]:
    """PURE: which arrow leaves this square. A plain node has one. A
    wait_event node takes the arrow labelled with its answer, or
    "timeout" when the alarm fired first; no matching arrow = the end."""
    if node.type != "wait_event":
        return arrows[0][0] if arrows else None
    answer = context.get(reply_key(node.id))
    wanted = TIMEOUT if answer is None else answer
    for dst, on in arrows:
        if on == wanted:
            return dst
    return None
