"""call — a normal buddy lead into today's dispatch machine.

ADR 0010: voice stays outside the gate, governed by its existing checks
(DND, blacklist, calling hours). enrollment_id is stamped after insert (the
050 customer-stamp pattern; the accessor's created hooks give the lead its
customer stamp + lead.pushed mirror for free). Each visit to the square
mints its own lead.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

from app.core.logger import logger
from app.crm.outreach.capacity import lines_for_template
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.nodes.blocks import blocks_for
from app.crm.outreach.nodes.context import (
    lead_request_id,
    playbook_key,
    run_facts,
)
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.nodes.wait import TOPIC_KEY
from app.crm.outreach.schemas import (
    CALL_REPORT_TOPIC,
    EnrollmentRun,
    WorkflowDefinition,
    WorkflowNode,
)
from app.database.accessor import (
    abort_queued_leads_by_enrollment,
    count_queued_leads_by_enrollment,
    create_lead_call_tracker,
    create_lead_call_tracker_if_lines_free,
    get_call_execution_config_by_template_id,
    get_lead_by_id,
    get_template_by_id,
    update_lead_enrollment_id,
)
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallStatus

# The report a call writes about itself when it ends (breeze_buddy/crm_mirror
# — source telephony). An awaiting call square listens for it, matched on
# the lead id it queued, and takes its edge when it lands.
CALL_COMPLETED = CALL_REPORT_TOPIC

# How long cancel_queued_calls keeps looking for a lead the dialler had
# locked at the moment of the abort.
_ABORT_RETRIES = 3
_ABORT_RETRY_SECONDS = 0.3

# Outcomes that are about the CUSTOMER, not about us: this phone cannot be
# dialled, or the customer asked never to be called. Walking on to the next
# call would fail the same way, so an awaiting square exits the run instead
# (ruled 22 Sep 2026); the outcome is left in context for the report. Every
# other failed outcome — a number misconfigured, no config, a pre-check that
# ran out, the reaper's UNKNOWN after a call that WAS placed — is ours or
# transient and takes the plain edge like a no-answer, so the ladder goes on.
EJECT_OUTCOMES = frozenset({"INVALID_PHONE", "BLACKLISTED"})


class NoRoom(Exception):
    """A cold run's call square found no free line on its number: the call
    was NOT queued. The walker puts the run back as due now (nodes/call.py
    execute; walker._advance) and it tries again next pass, oldest first."""


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    problems: List[str] = []
    if not node.template_id:
        problems.append(f"call node {node.id} needs a template_id")
    if node.await_:
        if node.key not in (None, TOPIC_KEY):
            problems.append(
                f"call node {node.id}: an awaiting call branches on the letter's "
                f"name — its key is {TOPIC_KEY} or left out"
            )
        if node.match is not None:
            problems.append(
                f"call node {node.id}: an awaiting call matches its own report "
                "on the lead it queued — match is not written on it"
            )
    elif node.topics or node.key:
        problems.append(
            f"call node {node.id}: only an awaiting call (await: true) listens "
            "for topics"
        )
    return problems


def awaiting_key(node_id: str) -> str:
    """Where an awaiting call square keeps the lead it is waiting on. Under
    the `lead_` prefix so run_facts filters it and it never reaches a
    template; present exactly while the square waits, cleared when it
    moves — so a lease retry re-enters the wait instead of queuing again."""
    return f"lead_awaiting_{node_id}"


def report_outcome(context: Dict[str, Any], node_id: str) -> Optional[str]:
    """PURE: the outcome the call report left under this square's facts
    (entry.py files a heard letter's scalars as context.facts.<square>),
    or None when no report has been heard."""
    facts = context.get("facts")
    if not isinstance(facts, dict):
        return None
    mine = facts.get(node_id)
    if not isinstance(mine, dict):
        return None
    outcome = mine.get("outcome")
    return str(outcome) if outcome not in (None, "") else None


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

    lead_row: Dict[str, Any] = dict(
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
            # The dialler never reads a CRM table (boundary rule 4): the
            # lane rides on the lead so the walker's cold budget can
            # count this plan's queued cold calls (migration 078).
            "lane": run.lane,
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
    # The overnight drain (capacity.py): a COLD run's call is written only
    # while its number has a free line — the count and the write are one
    # statement, so two walkers cannot both take the last line. A hot run's
    # call is written unconditionally: a customer acting now is never
    # gated, and hot demand is the number's to absorb.
    cold = run.lane == "cold"
    lines = await lines_for_template(str(template.id)) if cold else None
    if cold and lines is None:
        raise NodeParked(
            f"call node {node.id}: template {template.name} has no telephony "
            f"number pinned — its lines cannot be sized"
        )
    try:
        if lines is not None:
            lead = await create_lead_call_tracker_if_lines_free(
                list(lines.template_ids), lines.capacity, **lead_row
            )
        else:
            lead = await create_lead_call_tracker(**lead_row)
    except UniqueViolation:
        # Same meaning as None, so it falls to the same lookup. The accessor
        # swallows this today; kept for the day it narrows.
        lead = None

    if lead is None:
        # None means every failure, a duplicate key included, so the row's
        # existence tells them apart: ours means this visit already ran under
        # a lost lease — adopt it. Absent means a real failure — or, for a
        # cold run, no free line: not now, come back next pass.
        lead = await get_lead_by_id(lead_id)
        if lead is None:
            if lines is not None:
                raise NoRoom(f"call node {node.id}: no free line on {lines.number_id}")
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
    if node.await_:
        written[awaiting_key(node.id)] = lead_id
    if chosen:
        written[playbook_key(node.id)] = chosen
    return written


async def cancel_queued_calls(run_id: str, reason: str) -> None:
    """The calls a run still has queued end with the reason that ended
    them: the run exited (a goal, its max age, its plan archived), a
    merchant letter superseded the call, or its report never came. A
    queued call outlives the run otherwise — one waiting out the night
    would ring, next morning, a customer the run let go at midnight. A call
    already ringing is left to end on its own. Fail-open: the accessor
    logs and aborts nothing on a database error."""
    aborted = await abort_queued_leads_by_enrollment(run_id, reason)
    # The abort leaves a LOCKED lead alone — the dialler holds it, maybe
    # mid-dial — but the dialler also locks a lead for a moment to defer
    # it. A miss in that moment would let a superseded call ring later with
    # old facts, so look again, briefly, while a queued lead remains.
    for _ in range(_ABORT_RETRIES):
        left = await count_queued_leads_by_enrollment(run_id)
        if not left:
            break
        await asyncio.sleep(_ABORT_RETRY_SECONDS)
        aborted += await abort_queued_leads_by_enrollment(run_id, reason)
    if aborted:
        logger.info(
            f"run {run_id}: {reason} — aborted queued calls "
            f"{[lead.id for lead in aborted]}"
        )
