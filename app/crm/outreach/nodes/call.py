"""call — a normal buddy lead into today's dispatch machine.

ADR 0010: voice stays outside the gate, governed by its existing checks
(DND, blacklist, calling hours). enrollment_id is stamped after insert (the
050 customer-stamp pattern; the accessor's created hooks give the lead its
customer stamp + lead.pushed mirror for free). Each visit to the square
mints its own lead.

Which template it fires may be a question rather than a constant:
``template_rules`` is an if/else-if ladder over facts already in hand, the
square's own ``template_id`` the ``else``. The square does NOT branch for
it — one arrow out, one lead, one visit counter; only the cargo differs.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import NAMESPACE_URL, uuid5

from app.core.logger import logger
from app.crm.identity.contracts import customer_facts
from app.crm.outreach import predicates
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.nodes.context import lead_request_id, run_facts
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


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    """The square needs its default template, and every arm must name a real
    field — judged at publish so a typo is a sentence the author reads, never
    a run that silently fires the default forever.

    Op/value fit is Condition's own validator (the sealed where-grammar), so
    nothing is re-spelled here. No labels are checked: an arm names a
    template, not an edge, and a call square's arrows stay plain.
    """
    if not node.template_id:
        return [
            f"call node {node.id} needs a template_id — the template it fires, "
            "and the `else` when no arm of template_rules holds"
        ]
    node_ids = [n.id for n in definition.nodes]
    return [
        f"call node {node.id}: {problem}"
        for field in sorted(predicates.fields_named(node.template_rules))
        for problem in predicates.field_problems(field, node_ids)
    ]


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


async def _chosen_template_id(
    run: EnrollmentRun, node: WorkflowNode, facts: Dict[str, Any]
) -> str:
    """Which template this visit fires: the first arm whose conditions ALL
    hold, else the square's own template_id.

    The one read an arm may cost — the customer's predicate-safe facts
    through identity's contract — is paid only when an arm names customer.*,
    exactly as a condition square pays it. A customer with no row makes those
    arms not hold, so the default fires; a read that FAILS propagates and
    parks the run for retry, because a blip must never quietly downgrade a
    branded call to the generic template.
    """
    if not node.template_rules:
        return str(node.template_id)
    stage_facts = run.context.get("facts")
    stage_facts = stage_facts if isinstance(stage_facts, dict) else {}
    customer = None
    if predicates.needs_customer(node.template_rules):
        customer = await customer_facts(run.merchant_id, str(run.customer_id))
    arm = predicates.first_matching(node.template_rules, facts, stage_facts, customer)
    return arm.template_id if arm is not None else str(node.template_id)


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Enqueue a lead into today's dispatch machine.

    Idempotent per VISIT: a lease retry re-issues the same insert and the
    existing row is adopted. The accessor turns a duplicate key into None
    like every failure, so the square asks whether its own row is there.
    """
    phone = run.context.get("phone")
    if not phone:
        raise NodeParked(f"call node {node.id}: no phone in run context")

    # The facts an arm is judged on are the same ones the template will be
    # filled from — and they are judged BEFORE the phone is added below, so
    # no arm can read a handle value (predicates.py: HANDLE_LIKE).
    facts: Dict[str, Any] = run_facts(run.context, node)
    template_id = await _chosen_template_id(run, node, facts)

    template = await get_template_by_id(template_id)
    if template is None:
        raise NodeParked(f"call node {node.id}: template {template_id} not found")
    if template.merchant_id is not None and template.merchant_id != run.merchant_id:
        # The chosen one, whichever arm named it: an arm may not reach
        # another merchant's template.
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
    payload: Dict[str, Any] = {**facts, "customer_mobile_number": phone}

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
        logger.info(
            f"walker: run {run.id} lead {lead_id} already exists "
            f"(lease retry of visit {visit}) — continuing"
        )

    await update_lead_enrollment_id(lead_id, str(run.id))
    logger.info(f"walker: run {run.id} pushed lead {lead_id} (node {node.id})")
    return {f"lead_{node.id}": lead_id, _visits_key(node.id): visit}
