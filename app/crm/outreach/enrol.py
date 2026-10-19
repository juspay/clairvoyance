"""enrol() (W2) — the ONLY creator of workflow runs, both doors: the
reactive entry rules call enrol(); a broadcast's admission (broadcasts.py,
phase 3) calls enrol_step inside its own per-person atom. Admission guards are the plan's
own words (canon: entry carries reenter + cooldown, enforced for BOTH
doors); the open-run partial unique absorbs every race.

gather (admission facts) -> decide (PURE) -> apply (insert), one atom.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.crm.identity.contracts import person_records
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
from app.crm.outreach.window import alarm

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
    Returns (admit, reason) — the event door logs the reason; the broadcast
    door writes it on the recipient row (canon T18 skip_reason)."""
    if runs and not door.reenter:
        return False, "reenter_disabled"
    if latest_entered_at is not None and door.cooldown_hours > 0:
        cooled_at = latest_entered_at + timedelta(hours=door.cooldown_hours)
        if now < cooled_at:
            return False, "cooldown_active"
    return True, "admitted"


def _first_wake(start: WorkflowNode, now: datetime, max_age_days: float) -> datetime:
    """Arrival scheduling: the token arrives on the door's start square; a
    wait's alarm is window.alarm (its minutes, the window's next opening,
    or the end of the run's life), an action node's alarm is now (the
    canon 'enrolled = waiting with an immediate wake'). "Is it a wait?" is
    the registry's answer, never a type string — a listening first node
    once fell through here and enrolled with a zero listening window."""
    if is_wait(start):
        return alarm(start, now, now + timedelta(days=max_age_days))
    return now


async def enrol(
    *,
    merchant_id: str,
    workflow: Workflow,
    customer_id: str,
    context: Dict[str, Any],
    enrollment_key: Optional[str] = None,
    door: Optional[WorkflowEntryAt] = None,
    person: Optional[List[str]] = None,
) -> Optional[EnrollmentRun]:
    """Admit one customer into one live plan through one door (phase 15:
    the door names the square the run starts on; None = the plan's first
    door). Returns the run, or None when a guard (or the open-run unique)
    said no — a refusal is a normal outcome, never an error. context
    carries pointers + the small facts the sends need ({source_event_id,
    phone, ...}), never payloads. ``person`` is every record of this
    customer (identity's person_records) when the caller already has it;
    the guards judge the person's history, never one record's."""
    if workflow.status != "live" or not workflow.definition:
        _log_skipped(
            merchant_id,
            str(workflow.id),
            "not_live",
            workflow_status=workflow.status,
        )
        return None
    definition = WorkflowDefinition.model_validate(workflow.definition)
    records = person or await person_records(merchant_id, customer_id)
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
            records,
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
    records: List[str],
) -> Optional[EnrollmentRun]:
    """ATOMIC: the admission facts and the insert share one fate — the
    guards must judge the same history the new row joins, and the
    source-event idempotency read must not race a sibling tick — and the
    templates this document sends are held SHARED (shared/locks.py) so a
    retirement cannot commit between its count and this insert."""
    run, reason = await enrol_step(
        txn,
        merchant_id=merchant_id,
        workflow=workflow,
        definition=definition,
        door=door,
        customer_id=customer_id,
        context=context,
        enrollment_key=enrollment_key,
        records=records,
    )
    if run is None:
        _log_skipped(merchant_id, str(workflow.id), reason, customer_id=customer_id)
    # No "enrolled" log here: enrol() emits it AFTER the atom commits, or a
    # failed commit would count a run that never existed.
    return run


async def enrol_step(
    txn: DbTxn,
    *,
    merchant_id: str,
    workflow: Workflow,
    definition: WorkflowDefinition,
    door: WorkflowEntryAt,
    customer_id: str,
    context: Dict[str, Any],
    enrollment_key: str,
    records: List[str],
    source_broadcast_id: Optional[str] = None,
    wake_jitter: timedelta = timedelta(0),
) -> Tuple[Optional[EnrollmentRun], str]:
    """A sub-step of the CALLER'S atom (the txn is theirs): the guards, then
    the insert. Returns (run, "admitted") or (None, the refusal's word) and
    logs nothing — the event door logs a refusal (_enrol_in_txn above), a
    broadcast writes it on the recipient row (canon T18 skip_reason).

    ``records`` is every record of the person (identity's person_records).
    ``source_broadcast_id`` stamps the run a broadcast admitted (canon T20
    col 18); ``wake_jitter`` spreads a broadcast's first wakes, because
    mass writes must never come due the same second (canon T20 col 8)."""
    source_event_id = context.get("source_event_id")
    if source_event_id and await enrollment_accessor.source_event_used(
        txn, merchant_id, str(workflow.id), records, str(source_event_id)
    ):
        # This event already made its run (at-least-once scan). Named like
        # every other refusal: unlogged, it is the one way "events arriving,
        # no runs starting" can be normal and invisible at once.
        return None, "source_event_replayed"

    # Keyed plan: the guards judge THIS key's history (B2 — "one run per
    # <field>" means reenter/cooldown are about the order, not the
    # customer). Unkeyed: the key is the customer id and the read is hers —
    # every record of her (a merge staples, never melts).
    facts = await enrollment_accessor.admission_facts(
        txn,
        merchant_id,
        str(workflow.id),
        records,
        enrollment_key=enrollment_key if door.key else None,
    )
    now = datetime.now(timezone.utc)
    admit, reason = _admission(door, facts["runs"], facts["latest_entered_at"], now)
    if not admit:
        return None, reason
    if facts["open_runs"]:
        # Re-entry allowed, but a run of hers is still open — started on
        # another of her records, which the open-run unique (per key, and
        # unkeyed the key is the record's id) cannot see. For a customer
        # never merged this is the same refusal the unique gives below.
        # ponytail: two records enrolled in the SAME instant both pass this
        # read; the unique cannot span ids. Only a merge racing an enrol.
        return None, "open_run_exists"

    start = next((node for node in definition.nodes if node.id == door.start), None)
    if start is None:  # the validator forbids this; drift is an error, not a run
        raise ValueError(f"door {door.topic!r} starts on unknown node {door.start!r}")
    await version_accessor.lock_templates_shared(
        txn, merchant_id, definition.send_templates()
    )
    run = await enrollment_accessor.insert_enrollment(
        txn,
        merchant_id,
        str(workflow.id),
        workflow.version,
        customer_id,
        door.start,
        _first_wake(start, now, definition.exits.max_age_days) + wake_jitter,
        context,
        enrollment_key,
        source_broadcast_id,
    )
    return run, "admitted"
