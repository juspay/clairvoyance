"""call — a normal buddy lead into today's dispatch machine.

ADR 0010: voice stays outside the gate, governed by its existing checks
(DND, blacklist, calling hours). enrollment_id is stamped after insert (the
050 customer-stamp pattern; the accessor's created hooks give the lead its
customer stamp + lead.pushed mirror for free). Each visit to the square
mints its own lead.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import NAMESPACE_URL, uuid5

from app.core.logger import logger
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.nodes.blocks import blocks_for
from app.crm.outreach.nodes.context import (
    CALLS_TODAY_KEY,
    OUTCOME_KEY,
    calls_today,
    lead_request_id,
    max_calls_reached,
    playbook_key,
    run_facts,
    today_on,
)
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_template_id,
    get_lead_by_id,
    get_template_by_id,
    update_lead_enrollment_id,
)
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallStatus

# CALLS_TODAY_KEY (nodes/context.py) is the day-stamped ledger, and
# max_calls_reached() the predicate that reads it. Both live there, not here,
# because the CONDITION square asks the same predicate to route — one
# implementation, so the square that dials and the square that branches can
# never disagree. The ledger is its own key, NOT the visit counters: those
# key the lead ids (uuid5 run:node:visit) and must stay monotonic for the
# run's life. Resetting them at midnight would re-derive yesterday's id, the
# primary key would absorb it as a lease retry, and the first call of the new
# day would silently never be placed (the 967a86df scar, from the other side).
MAX_CALLS_OUTCOME = "max_calls"


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    if not node.template_id:
        return [f"call node {node.id} needs a template_id"]
    return []


def _visits_key(node_id: str) -> str:
    """Where a call square's visit counter lives in the run's context.

    `lead_` is already a bookkeeping prefix, so run_facts filters it for free
    and it can never reach a template or an action arg.
    """
    return f"lead_visits_{node_id}"


def _visits_so_far(context: Dict[str, Any], node_id: str) -> int:
    """PURE: how many times this run has completed this call square.

    Absent, or unreadable, reads as 0 — so an old run's next id is the `:1`
    it would have had. A wrong count mints a fresh lead; raising here would
    park a run over bookkeeping.
    """
    value = context.get(_visits_key(node_id))
    return value if isinstance(value, int) and value >= 0 else 0


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Enqueue a lead into today's dispatch machine.

    Idempotent per VISIT: a lease retry re-issues the same insert and the
    existing row is adopted. The accessor turns a duplicate key into None
    like every failure, so the square asks whether its own row is there.
    """
    # The plan's daily ceiling, judged before anything is read or written
    # (phase 20): at it, this square places no call and the run takes its
    # normal arrow. The ledger is not touched — it counts calls PLACED — so
    # the patch is the same on every re-run of this visit and a lease retry
    # changes nothing. The wait after this square, if it listens for
    # call.completed, will hear nothing and leave by its alarm; the runbook
    # shows the condition on max_calls_reached that routes past it.
    ceiling = definition.exits.max_calls_per_day
    day = today_on(definition.exits)
    if max_calls_reached(run.context, definition.exits):
        logger.bind(
            lead_skip=MAX_CALLS_OUTCOME,
            calls_today=calls_today(run.context, day),
            day=day,
        ).info(
            f"walker: run {run.id} at max_calls_per_day={ceiling} on {day} "
            f"— call square {node.id} places no call"
        )
        # The trail word only. No fact is written: a condition computes the
        # same predicate (nodes/condition.py), so there is nothing to go stale.
        return {OUTCOME_KEY: MAX_CALLS_OUTCOME}

    phone = run.context.get("phone")
    if not phone:
        raise NodeParked(f"call node {node.id}: no phone in run context")

    template = await get_template_by_id(str(node.template_id))
    if template is None:
        raise NodeParked(f"call node {node.id}: template {node.template_id} not found")
    if template.merchant_id is not None and template.merchant_id != run.merchant_id:
        raise NodeParked(f"call node {node.id}: template belongs to another merchant")
    config = await get_call_execution_config_by_template_id(str(template.id))
    if config is None:
        raise NodeParked(
            f"call node {node.id}: no call_execution_config for template "
            f"{template.name}"
        )

    # Deterministic per (run, node, VISIT). The counter lives in the run's
    # context, so a retry of one visit re-derives its own id and a revisit
    # gets a new one.
    visit = _visits_so_far(run.context, node.id) + 1
    lead_id = str(uuid5(NAMESPACE_URL, f"crm-workflow-lead:{run.id}:{node.id}:{visit}"))

    next_attempt_at = datetime.now(timezone.utc) + timedelta(
        seconds=config.initial_offset
    )
    # The template-variable bridge: every small fact the entry processor
    # carried (item, cart_value, ...) reaches the agent via the lead
    # payload — {placeholder}s in the template resolve from these keys.
    # reporting_webhook_url rides too: the lead machine reads it from the
    # lead payload to report the call's outcome back to the merchant.
    # The finished blocks ride BESIDE the scalars, into the payload only:
    # the run context has a size ceiling and canon keeps the row small,
    # so a rendered walk never touches it.
    rendered, chosen = await blocks_for(run, node, definition, node.blocks)
    payload: Dict[str, Any] = {
        **run_facts(run.context, node),
        **rendered,
        "customer_mobile_number": phone,
    }

    try:
        lead = await create_lead_call_tracker(
            id=lead_id,
            reseller_id=template.reseller_id,
            template=template.name,
            template_id=str(template.id),
            merchant_id=run.merchant_id,
            next_attempt_at=next_attempt_at,
            payload=payload,
            attempt_count=0,
            meta_data={
                "workflow_id": str(run.workflow_id),
                "enrollment_id": str(run.id),
            },
            request_id=lead_request_id(
                run.context,
                str(run.id),
                (
                    run.enrollment_key
                    if any(door.key for door in definition.entries)
                    else None
                ),
            ),
            execution_mode=ExecutionMode.TELEPHONY,
            status=LeadCallStatus.BACKLOG,
        )
    except UniqueViolation:
        # Same meaning as None, so it falls to the same lookup. The accessor
        # swallows this today; kept for the day it narrows.
        lead = None

    if lead is None:
        # None means every failure, a duplicate key included, so the row's
        # existence tells them apart: ours means this visit already ran under
        # a lost lease — adopt it. Absent means a real failure.
        lead = await get_lead_by_id(lead_id)
        if lead is None:
            raise RuntimeError(f"call node {node.id}: lead insert returned None")
        logger.bind(lead_id=lead_id).info(
            f"walker: run {run.id} lead {lead_id} already exists "
            f"(lease retry of visit {visit}) — continuing"
        )

    await update_lead_enrollment_id(lead_id, str(run.id))
    # lead_id as a FIELD — the join to Buddy's dial line needs a column.
    logger.bind(lead_id=lead_id).info(
        f"walker: run {run.id} pushed lead {lead_id} (node {node.id})"
    )
    written: Dict[str, Any] = {
        f"lead_{node.id}": lead_id,
        _visits_key(node.id): visit,
    }
    if ceiling is not None:
        # Re-stamping with today's date IS the reset: a ledger carried over
        # from yesterday is replaced, never added to. Nothing else is written
        # — there is no cached "reached" flag to clear, because the predicate
        # is computed from this ledger every time it is asked. A ceiling taken
        # away mid-run (`max_calls_per_day: null`) therefore frees the run on
        # the next question, with no stale `true` to survive it.
        written[CALLS_TODAY_KEY] = {"day": day, "n": calls_today(run.context, day) + 1}
    if chosen:
        written[playbook_key(node.id)] = chosen
    return written
