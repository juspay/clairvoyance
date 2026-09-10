"""call — a normal buddy lead into today's dispatch machine.

ADR 0010: voice stays outside the gate, governed by its existing checks
(DND, blacklist, calling hours). enrollment_id is stamped after insert (the
050 customer-stamp pattern; the accessor's created hooks give the lead its
customer stamp + lead.pushed mirror for free). The Redis schedule nudge is
deliberately skipped — the dispatch reconciler heals within 60s, and
outreach importing app.ai for a best-effort ZADD is a coupling not worth
one minute of latency on a 30-minute flow.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import NAMESPACE_URL, uuid5

from app.core.logger import logger
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.nodes.context import lead_request_id, run_facts
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_template_id,
    get_template_by_id,
    update_lead_enrollment_id,
)
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallStatus


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    if not node.template_id:
        return [f"call node {node.id} needs a template_id"]
    return []


def describe(
    node: WorkflowNode, context: Dict[str, Any], definition: WorkflowDefinition
) -> Dict[str, Any]:
    """PURE: the lead this square WOULD push (enh A/05) — the agent it
    names and the payload the template resolves its {placeholders} from,
    which is run_facts plus the number. The template row itself is NOT
    read: a dry run answers "what would go out", and whether that template
    exists is the publish validator's question, asked already."""
    phone = context.get("phone")
    if not phone:
        raise ValueError("no phone in the letter — this call would park")
    payload = run_facts(context, node)
    payload["customer_mobile_number"] = phone
    return {"template_id": str(node.template_id), "payload": payload}


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Enqueue a lead into today's dispatch machine. The lead is idempotent
    per visit via a deterministic id — a lease-retry after a crash re-issues
    the same insert and the PK absorbs it."""
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

    # Deterministic per (run, node): a lease-retry after a crash between
    # the insert and the advance re-issues the SAME id, and the PK (plus
    # the UniqueViolation below) absorbs the duplicate — exactly-once
    # calls without a coordination table.
    lead_id = str(uuid5(NAMESPACE_URL, f"crm-workflow-lead:{run.id}:{node.id}"))

    next_attempt_at = datetime.now(timezone.utc) + timedelta(
        seconds=config.initial_offset
    )
    # The template-variable bridge: every small fact the entry processor
    # carried (item, cart_value, ...) reaches the agent via the lead
    # payload — {placeholder}s in the template resolve from these keys.
    # reporting_webhook_url rides too: the lead machine reads it from the
    # lead payload to report the call's outcome back to the merchant.
    payload: Dict[str, Any] = run_facts(run.context, node)
    payload["customer_mobile_number"] = phone

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
        if lead is None:
            raise RuntimeError(f"call node {node.id}: lead insert returned None")
    except UniqueViolation:
        logger.info(
            f"walker: run {run.id} lead {lead_id} already exists "
            f"(lease retry) — continuing"
        )
    await update_lead_enrollment_id(lead_id, str(run.id))
    logger.info(f"walker: run {run.id} pushed lead {lead_id} (node {node.id})")
    return {f"lead_{node.id}": lead_id}
