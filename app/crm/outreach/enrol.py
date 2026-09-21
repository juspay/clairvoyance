"""enrol() (W2) — the ONLY creator of workflow runs, both doors (reactive
entry rules now, broadcasts in phase 2). Admission guards are the plan's
own words (canon: entry carries reenter + cooldown, enforced for BOTH
doors); the open-run partial unique absorbs every race.

gather (admission facts) -> decide (PURE) -> apply (insert), one atom.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app.core.logger import logger
from app.crm.outreach.db import DbTxn, UniqueViolation, atomically
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    version as version_accessor,
)
from app.crm.outreach.nodes import is_wait
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
    WorkflowEntry,
    WorkflowEntryAt,
    WorkflowNode,
)
from app.crm.outreach.window import held_alarm

#: Where entry-side lines file themselves — the walker has its own.
LOG_COMPONENT = "crm.outreach.entry"


def _log_skipped(
    merchant_id: str,
    workflow_id: str,
    skip_reason: str,
    *,
    customer_id: Optional[str] = None,
    workflow_status: Optional[str] = None,
) -> None:
    """One shape for every refusal to start a run. INFO, because most are
    the plan working as written (a cooldown, a run already open); what
    they feed is the comparison — events arriving while runs stop
    starting, grouped by which refusal grew.

    Each identifier rides under its own name, and only when it applies: one
    field holding a customer on one path and a plan's status on another is
    not something a query can group by.
    """
    named = {
        name: value
        for name, value in (
            ("customer_id", customer_id),
            ("workflow_status", workflow_status),
        )
        if value is not None
    }
    logger.bind(
        component=LOG_COMPONENT,
        merchant_id=merchant_id,
        workflow_id=workflow_id,
        skip_reason=skip_reason,
        **named,
    ).info(f"enrol skipped: {skip_reason} (workflow {workflow_id})")


def _admission(
    door: WorkflowEntry,
    runs: int,
    latest_entered_at: Optional[datetime],
    now: datetime,
) -> Tuple[bool, str]:
    """PURE decide: may this customer start a run through this door?
    Returns (admit, reason) — the reason is logged, never stored
    (skips-with-rows are the broadcast door's T18 concern, phase 2)."""
    if runs and not door.reenter:
        return False, "reenter_disabled"
    if latest_entered_at is not None and door.cooldown_hours > 0:
        cooled_at = latest_entered_at + timedelta(hours=door.cooldown_hours)
        if now < cooled_at:
            return False, "cooldown_active"
    return True, "admitted"


def _first_wake(
    start: WorkflowNode, now: datetime, max_age_days: float
) -> Tuple[datetime, str]:
    """Arrival scheduling: the token arrives on the door's start square; a
    wait's alarm is window.alarm (its minutes, the window's next opening,
    or the end of the run's life), an action node's alarm is now (the
    canon 'enrolled = waiting with an immediate wake'). "Is it a wait?" is
    the registry's answer, never a type string — a listening first node
    once fell through here and enrolled with a zero listening window.

    With the alarm, the run's LANE (migration 077): cold when the start
    square's window held the alarm to a later opening, hot otherwise."""
    if is_wait(start):
        wake, held = held_alarm(start, now, now + timedelta(days=max_age_days))
        return wake, "cold" if held else "hot"
    return now, "hot"


async def enrol(
    *,
    merchant_id: str,
    workflow: Workflow,
    customer_id: str,
    context: Dict[str, Any],
    enrollment_key: Optional[str] = None,
    door: Optional[WorkflowEntryAt] = None,
) -> Optional[EnrollmentRun]:
    """Admit one customer into one live plan through one door (phase 15:
    the door names the square the run starts on; None = the plan's first
    door). Returns the run, or None when a guard (or the open-run unique)
    said no — a refusal is a normal outcome, never an error. context
    carries pointers + the small facts the sends need ({source_event_id,
    phone, ...}), never payloads."""
    if workflow.status != "live" or not workflow.definition:
        _log_skipped(
            merchant_id,
            str(workflow.id),
            "not_live",
            workflow_status=workflow.status,
        )
        return None
    definition = WorkflowDefinition.model_validate(workflow.definition)
    try:
        run = await atomically(
            _enrol_in_txn,
            merchant_id,
            workflow,
            definition,
            door or definition.entries[0],
            customer_id,
            context,
            enrollment_key or customer_id,
        )
    except UniqueViolation:
        # The open-run partial unique IS the race arbiter: two entry
        # events, one token. Already in flow — a normal outcome.
        _log_skipped(
            merchant_id, str(workflow.id), "open_run_exists", customer_id=customer_id
        )
        return None
    if run is not None:
        # THE denominator: parks and exits only alarm as a share of runs
        # started, and this is the only line that counts one — logged HERE,
        # after the atom returns, so a commit failure cannot count a run
        # that was rolled back. source_event_id is the trail's other half —
        # the letter that caused this run; null when nothing triggered it.
        source_event_id = context.get("source_event_id")
        logger.bind(
            component=LOG_COMPONENT,
            merchant_id=merchant_id,
            workflow_id=str(workflow.id),
            run_id=str(run.id),
            customer_id=customer_id,
            node=run.current_node,
            source_event_id=str(source_event_id) if source_event_id else None,
        ).info(
            f"enrolled: run {run.id} (workflow {workflow.id}, "
            f"customer {customer_id}, node {run.current_node})"
        )
    return run


async def _enrol_in_txn(
    txn: DbTxn,
    merchant_id: str,
    workflow: Workflow,
    definition: WorkflowDefinition,
    door: WorkflowEntryAt,
    customer_id: str,
    context: Dict[str, Any],
    enrollment_key: str,
) -> Optional[EnrollmentRun]:
    """ATOMIC: the admission facts and the insert share one fate — the
    guards must judge the same history the new row joins, and the
    source-event idempotency read must not race a sibling tick — and the
    templates this document sends are held SHARED (shared/locks.py) so a
    retirement cannot commit between its count and this insert."""
    source_event_id = context.get("source_event_id")
    if source_event_id and await enrollment_accessor.source_event_used(
        txn, merchant_id, str(workflow.id), customer_id, str(source_event_id)
    ):
        # This event already made its run (at-least-once scan). Named like
        # every other refusal: unlogged, it is the one way "events arriving,
        # no runs starting" can be normal and invisible at once.
        _log_skipped(
            merchant_id,
            str(workflow.id),
            "source_event_replayed",
            customer_id=customer_id,
        )
        return None

    # Keyed plan: the guards judge THIS key's history (B2 — "one run per
    # <field>" means reenter/cooldown are about the order, not the
    # customer). Unkeyed: the key is the customer id and the read is hers.
    facts = await enrollment_accessor.admission_facts(
        txn,
        merchant_id,
        str(workflow.id),
        customer_id,
        enrollment_key=enrollment_key if door.key else None,
    )
    now = datetime.now(timezone.utc)
    admit, reason = _admission(door, facts["runs"], facts["latest_entered_at"], now)
    if not admit:
        _log_skipped(merchant_id, str(workflow.id), reason, customer_id=customer_id)
        return None

    start = next((node for node in definition.nodes if node.id == door.start), None)
    if start is None:  # the validator forbids this; drift is an error, not a run
        raise ValueError(f"door {door.topic!r} starts on unknown node {door.start!r}")
    await version_accessor.lock_templates_shared(
        txn, merchant_id, definition.send_templates()
    )
    wake, lane = _first_wake(start, now, definition.exits.max_age_days)
    run = await enrollment_accessor.insert_enrollment(
        txn,
        merchant_id,
        str(workflow.id),
        workflow.version,
        customer_id,
        door.start,
        wake,
        context,
        enrollment_key,
        lane,
    )
    # No log here: "enrolled" is emitted by enrol() AFTER the atom commits,
    # or a failed commit would count a run that never existed.
    return run
