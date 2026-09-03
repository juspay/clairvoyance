"""enrol() (W2) — the ONLY creator of workflow runs, both doors (reactive
entry rules now, broadcasts in phase 2). Admission guards are the plan's
own words (canon: entry carries reenter + cooldown, enforced for BOTH
doors); the open-run partial unique absorbs every race.

gather (admission facts) -> decide (PURE) -> apply (insert), one atom.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app.core.logger import logger
from app.crm.outreach.db import DbTxn, UniqueViolation, accessor, atomically
from app.crm.outreach.nodes import is_wait
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
    WorkflowEntry,
    WorkflowEntryAt,
    WorkflowNode,
)


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


def _first_wake(start: WorkflowNode, now: datetime) -> datetime:
    """Arrival scheduling: the token arrives on the door's start square; a
    wait node's alarm is arrival + delay, an action node's alarm is now
    (the canon 'enrolled = waiting with an immediate wake'). "Is it a
    wait?" is the registry's answer, never a type string — a wait_event
    first node used to fall through here and enrol with a zero listening
    window."""
    if is_wait(start) and start.minutes:
        return now + timedelta(minutes=start.minutes)
    return now


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
        logger.info(
            f"enrol skipped: workflow {workflow.id} not live "
            f"(status={workflow.status})"
        )
        return None
    definition = WorkflowDefinition.model_validate(workflow.definition)
    try:
        return await atomically(
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
        logger.info(
            f"enrol skipped: open run exists (workflow {workflow.id}, "
            f"customer {customer_id})"
        )
        return None


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
    if source_event_id and await accessor.source_event_used(
        txn, merchant_id, str(workflow.id), customer_id, str(source_event_id)
    ):
        return None  # this event already made its run (at-least-once scan)

    # Keyed plan: the guards judge THIS key's history (B2 — "one run per
    # <field>" means reenter/cooldown are about the order, not the
    # customer). Unkeyed: the key is the customer id and the read is hers.
    facts = await accessor.admission_facts(
        txn,
        merchant_id,
        str(workflow.id),
        customer_id,
        enrollment_key=enrollment_key if door.key else None,
    )
    now = datetime.now(timezone.utc)
    admit, reason = _admission(door, facts["runs"], facts["latest_entered_at"], now)
    if not admit:
        logger.info(
            f"enrol skipped: {reason} (workflow {workflow.id}, "
            f"customer {customer_id})"
        )
        return None

    start = next((node for node in definition.nodes if node.id == door.start), None)
    if start is None:  # the validator forbids this; drift is an error, not a run
        raise ValueError(f"door {door.topic!r} starts on unknown node {door.start!r}")
    await accessor.lock_templates_shared(txn, merchant_id, definition.send_templates())
    run = await accessor.insert_enrollment(
        txn,
        merchant_id,
        str(workflow.id),
        workflow.version,
        customer_id,
        door.start,
        _first_wake(start, now),
        context,
        enrollment_key,
    )
    logger.info(
        f"enrolled: run {run.id} (workflow {workflow.id}, "
        f"customer {customer_id}, node {run.current_node})"
    )
    return run
