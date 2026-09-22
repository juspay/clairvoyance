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
from app.core.logger.context import set_log_context, update_log_context
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.definitions import definition_for
from app.crm.outreach.nodes import NODE_TYPES, branches, is_wait
from app.crm.outreach.nodes.context import (
    CUT_SHORT_BY_KEY,
    OUTCOME_KEY,
    dispatch_id,
    reply_key,
    without_reply,
)
from app.crm.outreach.nodes.spec import ELSE, NodeParked
from app.crm.outreach.nodes.wait import TIMEOUT
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.crm.outreach.steps import (
    ARRIVED_BY_WALK,
    StepRecord,
    as_rows,
    closing,
    first_arrival,
    step,
)
from app.crm.outreach.window import alarm, opens_at
from app.crm.record.contracts import customer_has_event

LOG_COMPONENT = "crm.outreach.walker"

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
    self-heals when the pushed alarm comes due again.

    Resets the log context first: the ids the last run stamped are still
    standing (same task), and the pass line is not about that run.
    batch_full says only that the claim hit its LIMIT — one full batch may
    be exactly the last due rows, so it is not proof of backlog on its
    own. SUSTAINED full batches are: the alert rule counts consecutive
    ones, which is how "the walker is behind" is known without a query the
    claim's own wake_at push would have made meaningless anyway."""
    set_log_context(component=LOG_COMPONENT)
    runs = await enrollment_accessor.claim_due_runs(batch, CRM_WALKER_LEASE_SECONDS)
    if runs:
        logger.bind(claimed=len(runs), batch_full=len(runs) >= batch).info(
            f"walker pass: claimed {len(runs)} due run(s)"
        )
    return runs


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
    safe as the lease retry this file already relied on.

    Stamped before the lease check, so even the earliest failure line
    carries the run's ids; the nodes this visit executes inherit them."""
    set_log_context(
        component=LOG_COMPONENT,
        merchant_id=run.merchant_id,
        workflow_id=str(run.workflow_id),
        run_id=str(run.id),
        node=run.current_node,
    )
    lease = run.wake_at
    if lease is None:
        # A claimed run always carries its lease (the claim wrote it, and
        # waiting rows have wake_at NOT NULL). Anything else is a caller
        # bug — and a write without a token would be a blind overwrite.
        logger.error(f"walker: run {run.id} claimed without a lease — skipping")
        return
    try:
        workflow = await workflow_accessor.get_workflow(
            run.merchant_id, str(run.workflow_id)
        )
        if workflow is None or workflow.status == "archived":
            # The ejected run's last square still closes (canon T26). The
            # pinned document is read for its node TYPE alone, off the LRU
            # that the normal path would have hit a line later anyway.
            #
            # Never at the cost of the eject itself: before T26 this path
            # did not read a document at all, and a version row that no
            # longer validates would now park a run that an archived plan
            # should simply release. The record never stops the token — the
            # closing row is skipped and the exit proceeds.
            try:
                ejected = await definition_for(run)
            except Exception as e:
                logger.warning(
                    f"walker: run {run.id} ejecting without a closing step — "
                    f"definition v{run.workflow_version} unreadable: {e}"
                )
                ejected = None
            if await enrollment_accessor.exit_run(
                str(run.id),
                "ejected",
                lease,
                steps=as_rows(closing(run, ejected, "ejected")),
            ):
                _log_exit(run, "ejected")
            else:
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
        if await enrollment_accessor.park_run(str(run.id), str(e), lease):
            # A defect needs the document fixed; resuming alone re-parks it.
            # park_kind, NOT reason_class: that name carries dispatch's
            # fixed vocabulary, and one column holding two groups by neither.
            logger.bind(park_kind="defect", permanent=True).warning(
                f"walker: run {run.id} parked — {e}"
            )
        else:
            _deferred(run, "park")
    except Exception as e:
        if run.attempts >= CRM_WALKER_MAX_ATTEMPTS:
            if await enrollment_accessor.park_run(
                str(run.id), f"attempts exhausted: {e}", lease
            ):
                # The other kind: transient, never settled. Same dead end,
                # different fix — hence a field, not a message prefix.
                logger.bind(park_kind="attempts_exhausted", permanent=True).error(
                    f"walker: run {run.id} parked after retries — {e}"
                )
            else:
                _deferred(run, "park")
        else:
            retry_in = retry_delay_seconds(run.attempts, CRM_WALKER_LEASE_SECONDS)
            if await enrollment_accessor.record_run_error(
                str(run.id), str(e), retry_in, lease
            ):
                # permanent=False keeps this out of the failure counts:
                # the ladder is not spent, so nothing is owed yet.
                logger.bind(
                    retry_in_s=retry_in, attempts=run.attempts, permanent=False
                ).warning(f"walker: run {run.id} retries in {retry_in}s — {e}")
            else:
                _deferred(run, "retry")


def _log_exit(run: EnrollmentRun, reason: str) -> None:
    """How a run ended is otherwise written only to its row, where no
    alert rule can see it — and a rising `timed_out` share is how silent
    breakage upstream shows up."""
    logger.bind(exit_reason=reason).info(f"walker: run {run.id} exited {reason}")


def _deferred(run: EnrollmentRun, write: str) -> None:
    """A CAS miss: the run moved under the lease (a reply or repeat landed
    mid-visit). Nothing to undo — the event side's alarm stands, the
    buffered history is discarded with the move it belonged to (the INSERT
    selects FROM the UPDATE's own RETURNING), and the next claim re-reads
    the run as it now is."""
    logger.info(
        f"walker: run {run.id} changed under the lease ({write} skipped) — "
        f"deferring to the next wake"
    )


async def _advance(
    run: EnrollmentRun, definition: WorkflowDefinition, lease: datetime
) -> None:
    """One visit: exits judged first, then the token moves as far as it
    can without waiting.

    The order is the contract — age ceiling, then goal, then the board —
    because both exits must end a run standing on ANY square, including
    one whose action would otherwise fire. The loop executes consecutive
    immediate squares under the one claim and stops when the next square
    waits; a wait's alarm IS its action. Raises NodeParked for anything
    the document itself got wrong.
    """
    nodes = {node.id: node for node in definition.nodes}
    outgoing = definition.outgoing()
    now = datetime.now(timezone.utc)

    # The hard ceiling first: a run older than the plan's max age exits
    # timed_out no matter which square it stands on.
    max_age = timedelta(days=definition.exits.max_age_days)
    if now - run.entered_at > max_age:
        if await enrollment_accessor.exit_run(
            str(run.id),
            "timed_out",
            lease,
            steps=as_rows(closing(run, definition, "timed_out")),
        ):
            _log_exit(run, "timed_out")
        else:
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
            if await enrollment_accessor.exit_run(
                str(run.id),
                tier.exit_reason,
                lease,
                steps=as_rows(closing(run, definition, tier.exit_reason)),
            ):
                _log_exit(run, tier.exit_reason)
            else:
                _deferred(run, tier.exit_reason)
            return

    current_id = run.current_node
    context = dict(run.context)
    # The letter that woke this run in place, if one did (canon T26): it
    # dates the square we are about to close and names how this visit began.
    # POPPED, not read — written back it would age into a later visit and
    # credit the wrong square, the way a stale reply would.
    cut_short_by = context.pop(CUT_SHORT_BY_KEY, None)
    cut_short_by = str(cut_short_by) if cut_short_by else None
    arrived_by = first_arrival(run, cut_short_by)
    arrived_at = run.node_arrived_at
    walked: List[StepRecord] = []
    first = True
    for _ in range(_MAX_STEPS_PER_VISIT):
        # walk_run stamped the square the token ARRIVED on; a park three
        # squares later would blame it. update_, not set_ — set_ would
        # drop merchant/workflow/run.
        update_log_context(node=current_id)
        node = nodes.get(current_id)
        if node is None:
            # A pinned version never loses a node under a run (pin), and
            # the migrate validator forbids stranding — so this is drift
            # (e.g. a run parked across an archive/re-create): honest park.
            raise NodeParked(
                f"node {current_id} not in definition v{run.workflow_version}"
            )

        if node.window is not None and context.get(reply_key(node.id)) is None:
            # The timer fired, not a letter. Outside the window's hours the
            # run holds on this square, still listening, until it opens. A
            # letter is never held; publish refuses a letter arrow from here
            # that reaches a call without waiting (plans.py), so together
            # nothing is queued at night for the dialler to ring at 7 AM.
            opening = opens_at(now, node.window)
            if opening > now:
                # TRAP 1 (canon T26): the hold calls advance_run with this
                # square's OWN id. The token has NOT left it, so no row
                # closes here — and the arrival is restamped only when THIS
                # visit walked into the square, never when the run was
                # already standing on it. Keyed on "we called advance", an
                # overnight hold would write a zero-length step every
                # morning and make "waiting since Friday" render as
                # "waiting since 9am".
                #
                # Squares this visit already finished before reaching the
                # hold still flush: they were left, and they share the fate
                # of the write that records the hold.
                if not await enrollment_accessor.advance_run(
                    str(run.id),
                    node.id,
                    opening,
                    # The letter that re-armed this run is put BACK: the
                    # square it woke is still the square the token stands
                    # on, and the visit that finally closes it is the one
                    # that owes the pointer. Consuming it here would leave
                    # that row saying `timer` — a plausible-looking lie, and
                    # the hold is reached almost only by a re-arm, so this
                    # is the common path, not a corner.
                    (
                        {**context, CUT_SHORT_BY_KEY: cut_short_by}
                        if cut_short_by
                        else context
                    ),
                    lease,
                    node_arrived_at=None if first else arrived_at,
                    steps=as_rows(walked),
                ):
                    _deferred(run, f"hold on {node.id}")
                return

        execute = NODE_TYPES[node.type].execute
        dispatched: Optional[str] = None
        said: Optional[str] = None
        if execute is not None:  # a wait's action IS the alarm
            # The square sees the context AS WALKED, not the one the run was
            # claimed with. This visit runs consecutive immediate squares
            # (call -> condition -> call) under one claim and only persists at
            # the end, so a word reading run.context would read a snapshot
            # that is already stale by its own earlier squares — two call
            # squares in one visit both spent the same daily allowance, and
            # the second overwrote the first's ledger write. The copy is
            # shallow and read-only to the word: patches still come back as
            # return values, never as mutations.
            patch = dict(
                await execute(
                    run.model_copy(update={"context": context}), node, definition
                )
            )
            # A plain square may say how it was left (phase 20: a call square
            # at the plan's ceiling). The word is for the trail row below —
            # popped here so it is never written into the run's context.
            said = patch.pop(OUTCOME_KEY, None)
            context.update(patch)
            dispatched = dispatch_id(patch, node.id)

        next_id = pick_next(node, outgoing.get(current_id, []), context)
        outcome: Optional[str] = None if said is None else str(said)
        if branches(node):
            # The answer that resolved this square IS its outcome (canon
            # T26) — a reply, a condition's rule label, a split's arm, or
            # the timeout when the alarm won. Read BEFORE the clear below,
            # which is the whole reason a condition's branch reaches disk
            # nowhere else.
            answer = context.get(reply_key(node.id))
            outcome = TIMEOUT if answer is None else str(answer)
            # Leaving a branching square: its answer is spent (phase 15).
            # A door may start a run on any square, so this one can be
            # revisited — a stale reply would resolve the revisit at once.
            context = without_reply(context, node.id)
        left_at = datetime.now(timezone.utc)

        if next_id is None:
            walked += step(
                node,
                arrived_at,
                left_at,
                arrived_by,
                outcome,
                None,
                run,
                cut_short_by,
                first,
                dispatched,
            )
            if await enrollment_accessor.exit_run(
                str(run.id),
                "completed",
                lease,
                current_node=current_id,
                context=context,
                steps=as_rows(walked),
            ):
                _log_exit(run, "completed")
            else:
                _deferred(run, "completed")
            return

        next_node = nodes.get(next_id)
        if next_node is None:
            raise NodeParked(f"edge points at unknown node {next_id}")
        if is_wait(next_node):
            # Arrival scheduling: the wait's alarm starts now — its minutes,
            # the window's next opening, or the end of the run's life
            # (window.alarm). A wait already due moves on in this same visit.
            wake = alarm(next_node, left_at, run.entered_at + max_age)
            if wake > left_at:
                walked += step(
                    node,
                    arrived_at,
                    left_at,
                    arrived_by,
                    outcome,
                    next_id,
                    run,
                    cut_short_by,
                    first,
                    dispatched,
                )
                if not await enrollment_accessor.advance_run(
                    str(run.id),
                    next_id,
                    wake,
                    context,
                    lease,
                    # The token really did move, so the new square's arrival
                    # is this one's exit — gapless by construction.
                    node_arrived_at=left_at,
                    steps=as_rows(walked),
                ):
                    _deferred(run, f"advance to {next_id}")
                return

        walked += step(
            node,
            arrived_at,
            left_at,
            arrived_by,
            outcome,
            next_id,
            run,
            cut_short_by,
            first,
            dispatched,
        )
        arrived_at = left_at  # the next square's arrival is this one's exit
        arrived_by = ARRIVED_BY_WALK
        cut_short_by = None  # the letter cut short ONE square, not the chain
        first = False
        current_id = next_id  # an action, or a due wait: this same visit

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
    branching node (a listening wait, a condition, a split) takes
    the arrow labelled with its answer, or "timeout" when the alarm fired
    first, else the "else" arrow (phase 18) when it has one; no matching
    arrow = the end."""
    if not branches(node):
        return arrows[0][0] if arrows else None
    answer = context.get(reply_key(node.id))
    wanted = TIMEOUT if answer is None else answer
    for dst, on in arrows:
        if on == wanted:
            return dst
    for dst, on in arrows:
        if on == ELSE:
            return dst
    return None
