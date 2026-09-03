"""The node vocabulary — ONE registry (modules/05-outreach, ruled 31 Aug
2026): every square the board speaks is one entry here, carrying the three
things a type must answer — how the publish validator checks it, what the
walker does when the token lands on it, and whether landing on it means
waiting. The validator iterates NODE_TYPES, the walker dispatches through
it, enrol() asks it for the first alarm; none of them matches type
strings. Adding a type = one entry + one word in the schema's Literal, and
a test pins the two together so a half-added type fails CI instead of
shipping a walker that doesn't speak what the validator accepted.
Precedent: record's EXTRACTORS registry.

Where each action lands:
  wait — time passes; the alarm was set on arrival (arrival scheduling).
  wait_event — the alarm OR an event, whichever first (W5): the consumer
         writes the answer into context[reply_<node>] and wakes the run;
         the edge whose `on` equals the answer is taken, else "timeout".
  call — a normal buddy lead into today's dispatch machine (ADR 0010:
         voice stays outside the gate, governed by its existing checks —
         DND, blacklist, calling hours), enrollment_id stamped after
         insert (the 050 customer-stamp pattern; the accessor's created
         hooks give the lead its customer stamp + lead.pushed mirror for
         free). The Redis schedule nudge is deliberately skipped — the
         dispatch reconciler heals within 60s, and outreach importing
         app.ai for a best-effort ZADD is a coupling not worth one minute
         of latency on a 30-minute flow.
  send — proposes one manifest row, status queued, NO verdict
         (gate-mechanics §1: the dispatcher gate-checks at the last
         responsible moment). dedupe_key = run:node, so a lease retry is
         absorbed by the manifest's unique (canon T16 col 23).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

from app.core.logger import logger
from app.crm.connectivity.contracts import queue_message
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_template_id,
    get_template_by_id,
    update_lead_enrollment_id,
)
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallStatus

# The walker's own bookkeeping in a run's context — never a template
# variable, never a lead payload key: pointers, the phone (re-added under
# its canonical key by the call node), per-node results and answers.
_BOOKKEEPING_KEYS = (
    "source_event_id",
    "entered_event_at",  # entry.py: when the founding letter happened (G7)
    "goal",  # entry.py: the letter that ended the run, and its amount (phase 09)
    "phone",
    "customer_mobile_number",
    "repeat_event_ids",  # repeat.py: which letters already patched this run
    "repeat_items",  # repeat.py: accumulate's list — never a template variable
    "facts",  # entry.py: each square's letter, by square (phase 16) — flattened below
    "current_node",  # run_facts: computed from the square, never a producer's
    "current_stage",
)
_BOOKKEEPING_PREFIXES = ("lead_", "message_", "reply_")

# The one $-word a wait_event may branch on (rollout phase 15): the
# event's TOPIC rather than a payload field.
TOPIC_KEY = "$topic"


def reply_key(node_id: str) -> str:
    """Where a wait_event square's answer lives in the run's context."""
    return f"reply_{node_id}"


def without_reply(context: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    """PURE: the context with this square's answer cleared — written when
    the token leaves the square (phase 15). A door may start a run on any
    square, so a square can be revisited; a stale answer left behind
    would resolve the revisit at once, on the old reply."""
    return {key: value for key, value in context.items() if key != reply_key(node_id)}


# Facts the lead machine consumes itself, not the template: kept in the
# call payload, dropped from send variables.
_LEAD_ONLY_KEYS = ("reporting_webhook_url",)

# The merchant's own id for the thing this call is about. Buddy's reporter
# echoes lead.request_id back to the merchant as orderId on every outcome
# webhook — nautilus matches it to the Shopify order, so it must be THEIR
# id, not ours. The run id is the fallback for a plan with no order.
_REQUEST_ID_KEYS = ("order_id", "request_id")

Validate = Callable[[WorkflowNode, WorkflowDefinition], List[str]]
Execute = Callable[
    [EnrollmentRun, WorkflowNode, WorkflowDefinition], Awaitable[Dict[str, Any]]
]


class NodeParked(Exception):
    """A deterministic execution failure: parking is the honest outcome
    (a missing template, a module not yet deployed). Transient failures
    raise anything else and retry on the lease."""


@dataclass(frozen=True)
class NodeSpec:
    """What one word of the vocabulary must answer. execute is None for a
    wait: landing on it IS the action (the alarm), so is_wait and execute
    are two views of one fact and the registry test pins them together."""

    validate: Validate
    execute: Optional[Execute]
    is_wait: bool


def is_wait(node: WorkflowNode) -> bool:
    """The one place that answers "does landing here mean waiting?" —
    enrol()'s first alarm, the walker's step and its next alarm all ask
    this; none may match a type string."""
    return NODE_TYPES[node.type].is_wait


# --- validate: per-type publish laws (graph laws stay in plans.py) ---


def _validate_wait(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    if not (node.minutes and node.minutes > 0):
        return [f"wait node {node.id} needs minutes > 0"]
    return []


def _validate_send(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    problems = []
    if not node.template:
        problems.append(f"send node {node.id} needs a template")
    if not node.channel:
        problems.append(f"send node {node.id} needs a channel")
    if not definition.purpose_key:
        problems.append(
            f"send node {node.id}: the plan needs a purpose_key "
            "(what its sends are for, e.g. utility.order.cod_confirm)"
        )
    return problems


def _validate_call(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    if not node.template_id:
        return [f"call node {node.id} needs a template_id"]
    return []


def _validate_wait_event(
    node: WorkflowNode, definition: WorkflowDefinition
) -> List[str]:
    problems = []
    if not (node.minutes and node.minutes > 0):
        problems.append(f"wait_event node {node.id} needs minutes > 0")
    if not node.topics:
        problems.append(f"wait_event node {node.id} needs topics")
    if not node.key:
        problems.append(f"wait_event node {node.id} needs a payload key")
    elif node.key.startswith("$") and node.key != TOPIC_KEY:
        problems.append(
            f"wait_event node {node.id}: key {node.key!r} — the only $-word is "
            f"{TOPIC_KEY} (branch on the event's topic)"
        )
    return problems


# --- execute: what the walker does when the token lands ---


async def execute_call(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """ADR 0010: enqueue a lead into today's dispatch machine. The lead is
    idempotent per visit via a deterministic id — a lease-retry after a
    crash re-issues the same insert and the PK absorbs it."""
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
            request_id=lead_request_id(run.context, str(run.id)),
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


async def execute_send(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Propose the send; never call a provider (04-connectivity: one call
    site, send(), inside connectivity). A None from queue_message means a
    lease retry re-proposed the same run:node and the manifest already has
    it — carry on."""
    phone = run.context.get("phone")
    if not phone:
        raise NodeParked(f"send node {node.id}: no phone in run context")
    if not (node.channel and node.template):
        raise NodeParked(f"send node {node.id}: needs channel and template")
    if not definition.purpose_key:
        raise NodeParked(f"send node {node.id}: plan has no purpose_key")

    dedupe_key = f"{run.id}:{node.id}"
    try:
        message_id = await queue_message(
            merchant_id=run.merchant_id,
            customer_id=str(run.customer_id),
            channel=node.channel,
            address=str(phone),
            source_kind="workflow",
            source_id=str(run.id),
            purpose_key=definition.purpose_key,
            template_id=node.template,
            variables=send_variables(run.context, node),
            dedupe_key=dedupe_key,
        )
    except ValueError as e:
        raise NodeParked(f"send node {node.id}: {e}") from e
    if message_id is None:
        logger.info(
            f"walker: run {run.id} send {dedupe_key} already queued (lease retry)"
        )
        return {}
    logger.info(f"walker: run {run.id} queued message {message_id} (node {node.id})")
    return {f"message_{node.id}": message_id}


# --- the small pure helpers the actions share ---


def lead_request_id(context: Dict[str, Any], run_id: str) -> str:
    """PURE: the merchant's order/request id from the run's facts, else
    a traceable wf-<run id>."""
    for key in _REQUEST_ID_KEYS:
        value = context.get(key)
        if value not in (None, ""):
            return str(value)
    return f"wf-{run_id}"


def run_facts(
    context: Dict[str, Any], node: Optional[WorkflowNode] = None
) -> Dict[str, Any]:
    """PURE: the run's small facts = context minus the walker's own
    bookkeeping. The ONE filter both the call payload and the send
    variables derive from, so they can never disagree on what is ours.

    Phase 16: each square's letter lives under context.facts.<square>
    (entry.py writes it on resume). Flattened here for templates: the
    top-level facts first, then the CURRENT square's override them (the
    most recent stage wins the call), and every square's stay reachable as
    facts_<square>_<key>. With the square given, current_node (and
    current_stage when the square is labelled) ride along, so one call
    template can say "you stopped at {current_stage}"."""
    facts = {
        key: value
        for key, value in context.items()
        if key not in _BOOKKEEPING_KEYS and not key.startswith(_BOOKKEEPING_PREFIXES)
    }
    by_square = context.get("facts")
    by_square = by_square if isinstance(by_square, dict) else {}
    for square, letter in by_square.items():
        if isinstance(letter, dict):
            for key, value in letter.items():
                facts[f"facts_{square}_{key}"] = value
    if node is not None:
        current = by_square.get(node.id)
        if isinstance(current, dict):
            facts.update(current)
        facts["current_node"] = node.id
        if node.stage:
            facts["current_stage"] = node.stage
    return facts


def send_variables(
    context: Dict[str, Any], node: Optional[WorkflowNode] = None
) -> Dict[str, Any]:
    """PURE: the template's fill-ins = the run's facts minus what only
    the lead machine reads, and only what a provider can render: text and
    numbers. A bool or a None among the variables makes the WhatsApp face
    refuse the whole message terminally (connectivity renders nothing it
    cannot spell — "True" or "None" inside a customer's message is
    corruption that looks delivered), and with every listened letter's
    facts riding along (phase 16) such values are ordinary, not rare."""
    return {
        key: value
        for key, value in run_facts(context, node).items()
        if key not in _LEAD_ONLY_KEYS
        and isinstance(value, (str, int, float))
        and not isinstance(value, bool)
    }


# --- THE registry: the vocabulary, one entry per word ---

NODE_TYPES: Dict[str, NodeSpec] = {
    "wait": NodeSpec(validate=_validate_wait, execute=None, is_wait=True),
    "send": NodeSpec(validate=_validate_send, execute=execute_send, is_wait=False),
    "call": NodeSpec(validate=_validate_call, execute=execute_call, is_wait=False),
    "wait_event": NodeSpec(validate=_validate_wait_event, execute=None, is_wait=True),
}
