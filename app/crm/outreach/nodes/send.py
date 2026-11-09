"""send — proposes ONE manifest row, status queued, with no verdict.

gate-mechanics §1: the dispatcher gate-checks at the last responsible
moment, so nothing here calls a provider (04-connectivity: one call site,
send(), inside connectivity). dedupe_key = run:node, so a lease retry is
absorbed by the manifest's unique (canon T16 col 23).

Who it is addressed to is the CHANNEL's answer (gate_handle_kind_for): a
phone channel takes the run's phone, as it always has; a channel addressed
by a row-kind id (Xyne's user id) takes the customer's live id of that kind
— never a copy on the run (context.py keeps those out). And a channel that
takes structured variables gets a declared list as the list itself, read
from the letter the run points at, not the one line of text the run holds.
"""

import json
from typing import Any, Dict, List

from app.core.logger import logger
from app.crm.connectivity.contracts import (
    gate_handle_kind_for,
    queue_message,
    takes_structured_variables,
)
from app.crm.identity.contracts import get_live_handle
from app.crm.outreach.nodes.blocks import blocks_for
from app.crm.outreach.nodes.context import playbook_key, send_variables
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode
from app.crm.record.contracts import AmbiguousTopic, declared_lists
from app.crm.shared.handles import ROW_KIND_NAMES

#: The most one send may carry as lists: enough for a form's options, small
#: enough that one manifest row stays a row.
STRUCTURED_MAX_ITEMS = 100
STRUCTURED_MAX_BYTES = 16 * 1024


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
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
    problems.extend(_variable_map_problems(node))
    return problems


def _variable_map_problems(node: WorkflowNode) -> List[str]:
    """PURE: the shape laws of a send node's {blank: fact} map — names on
    both sides, and one parameter style per template (a provider takes
    positional "1","2" OR named, never a mix; refusing here beats a
    refusal at dispatch). Whether each fact is DECLARED is the catalog
    law, checked at publish (plans.py) where the catalog is at hand."""
    problems = []
    for blank, fact in node.variables.items():
        if not blank.strip() or not fact.strip():
            problems.append(
                f"send node {node.id}: variables map needs a template blank on "
                f"the left and a run fact on the right (got {blank!r}: {fact!r})"
            )
    positional = [b for b in node.variables if b.isascii() and b.isdigit()]
    if positional and len(positional) != len(node.variables):
        problems.append(
            f'send node {node.id}: variables mix positional ("1", "2") and '
            "named blanks — a template takes one style"
        )
    return problems


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Propose the send; never call a provider. A None from queue_message
    means a lease retry re-proposed the same run:node and the manifest
    already has it — carry on."""
    address = await _address(run, node)
    if not (node.channel and node.template):
        raise NodeParked(f"send node {node.id}: needs channel and template")
    if not definition.purpose_key:
        raise NodeParked(f"send node {node.id}: plan has no purpose_key")

    rendered, chosen = await blocks_for(run, node, definition, node.variables.values())
    lists = await _lists(run, node)
    try:
        variables = send_variables(
            {
                blank: fact
                for blank, fact in node.variables.items()
                if blank not in lists
            },
            run.context,
            node,
            extra=rendered,
        )
    except KeyError as e:
        raise NodeParked(
            f"send node {node.id}: mapped fact {e.args[0]!r} is not in the run "
            "context — the entry event did not carry it"
        ) from e
    except ValueError as e:
        raise NodeParked(f"send node {node.id}: {e}") from e
    variables.update(lists)

    dedupe_key = f"{run.id}:{node.id}"
    try:
        message_id = await queue_message(
            merchant_id=run.merchant_id,
            customer_id=str(run.customer_id),
            channel=node.channel,
            address=address,
            source_kind="workflow",
            source_id=str(run.id),
            purpose_key=definition.purpose_key,
            template_id=node.template,
            variables=variables,
            dedupe_key=dedupe_key,
        )
    except ValueError as e:
        raise NodeParked(f"send node {node.id}: {e}") from e
    if message_id is None:
        logger.info(
            f"walker: run {run.id} send {dedupe_key} already queued (lease retry)"
        )
        return {}
    # message_id as a FIELD, to join against dispatch's terminal line.
    logger.bind(message_id=message_id).info(
        f"walker: run {run.id} queued message {message_id} (node {node.id})"
    )
    written: Dict[str, Any] = {f"message_{node.id}": message_id}
    if chosen:
        written[playbook_key(node.id)] = chosen
    return written


async def _address(run: EnrollmentRun, node: WorkflowNode) -> str:
    """Who this send goes to, by the channel's own address kind.

    A row-kind channel reads the customer's LIVE id of that kind, at send
    time — the id identity holds now, not whatever a letter said when the
    run began. Every other channel takes the run's phone, exactly as before
    channels could say otherwise (an unregistered channel included: the
    queue refuses it by name)."""
    kind = gate_handle_kind_for(node.channel) if node.channel else None
    if kind is not None and kind in ROW_KIND_NAMES:
        value = await get_live_handle(run.merchant_id, str(run.customer_id), kind)
        if not value:
            raise NodeParked(
                f"send node {node.id}: the customer has no {kind} to send to"
            )
        return value
    phone = run.context.get("phone")
    if not phone:
        raise NodeParked(f"send node {node.id}: no phone in run context")
    return str(phone)


async def _lists(run: EnrollmentRun, node: WorkflowNode) -> Dict[str, Any]:
    """{blank: list} for each mapped fact that is a declared list of the
    run's founding letter — only on a channel that takes structured
    variables; {} everywhere else, so every other send is unchanged.

    Read from the letter (record keeps it; the run keeps a pointer), within
    a bound: a list over it parks the run by name rather than posting a
    truncated set of options as if it were the whole."""
    if not node.channel or not node.variables:
        return {}
    if not takes_structured_variables(node.channel):
        return {}
    event_id = run.context.get("source_event_id")
    if not event_id:
        return {}
    try:
        found = await declared_lists(
            run.merchant_id, str(event_id), set(node.variables.values())
        )
    except AmbiguousTopic as e:
        raise NodeParked(f"send node {node.id}: {e}") from e
    lists = {
        blank: found[fact] for blank, fact in node.variables.items() if fact in found
    }
    for blank, values in lists.items():
        if values is None:
            # A declared list the letter does not carry: sending the run's
            # line of text instead would post a form whose picker is empty
            # and still record it accepted.
            raise NodeParked(
                f"send node {node.id}: the starting letter carries no "
                f"{node.variables[blank]} list"
            )
        if len(values) > STRUCTURED_MAX_ITEMS:
            raise NodeParked(
                f"send node {node.id}: {blank} has {len(values)} items — at most "
                f"{STRUCTURED_MAX_ITEMS} ride a send"
            )
    size = len(json.dumps(lists, ensure_ascii=False, default=str).encode("utf-8"))
    if size > STRUCTURED_MAX_BYTES:
        raise NodeParked(
            f"send node {node.id}: its lists are {size} bytes — at most "
            f"{STRUCTURED_MAX_BYTES} ride a send"
        )
    return lists
