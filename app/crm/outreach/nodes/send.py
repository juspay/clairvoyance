"""send — proposes ONE manifest row, status queued, with no verdict.

gate-mechanics §1: the dispatcher gate-checks at the last responsible
moment, so nothing here calls a provider (04-connectivity: one call site,
send(), inside connectivity). dedupe_key = run:node, so a lease retry is
absorbed by the manifest's unique (canon T16 col 23).
"""

from typing import Any, Dict, List

from app.core.logger import logger
from app.crm.connectivity.contracts import message_id_for_dedupe, queue_message
from app.crm.outreach.nodes.context import send_variables
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode


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


def describe(
    node: WorkflowNode, context: Dict[str, Any], definition: WorkflowDefinition
) -> Dict[str, Any]:
    """PURE: the message this square WOULD post (enh A/05) — the same
    channel, template and resolved blanks execute would hand connectivity,
    built by the same send_variables, so a dry run cannot promise a
    message the real send would refuse. Raises what execute parks on; the
    simulator renders it beside the step."""
    if not context.get("phone"):
        raise ValueError("no phone in the letter — this send would park")
    if not definition.purpose_key:
        raise ValueError("the plan has no purpose — this send would park")
    return {
        "channel": node.channel,
        "template": node.template,
        "variables": send_variables(node.variables, context, node),
    }


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Propose the send; never call a provider. A None from queue_message
    means a lease retry re-proposed the same run:node and the manifest
    already has it — carry on."""
    phone = run.context.get("phone")
    if not phone:
        raise NodeParked(f"send node {node.id}: no phone in run context")
    if not (node.channel and node.template):
        raise NodeParked(f"send node {node.id}: needs channel and template")
    if not definition.purpose_key:
        raise NodeParked(f"send node {node.id}: plan has no purpose_key")

    try:
        variables = send_variables(node.variables, run.context, node)
    except KeyError as e:
        raise NodeParked(
            f"send node {node.id}: mapped fact {e.args[0]!r} is not in the run "
            "context — the entry event did not carry it"
        ) from e
    except ValueError as e:
        raise NodeParked(f"send node {node.id}: {e}") from e

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
            variables=variables,
            dedupe_key=dedupe_key,
        )
    except ValueError as e:
        raise NodeParked(f"send node {node.id}: {e}") from e
    if message_id is None:
        # A lease retry re-proposed a send the manifest already holds. The
        # id still has to reach the run: the listening square after this
        # one matches letters on message_<node> (phase 18), and a retry
        # that wrote nothing would leave that square deaf to its own
        # receipt (enh A/06, N12). One read on the insert's own unique.
        message_id = await message_id_for_dedupe(run.merchant_id, dedupe_key)
        if message_id is None:
            logger.warning(
                f"walker: run {run.id} send {dedupe_key} was absorbed but no row "
                f"names it — message_{node.id} not written"
            )
            return {}
        logger.info(
            f"walker: run {run.id} send {dedupe_key} already queued as "
            f"{message_id} (lease retry)"
        )
        return {f"message_{node.id}": message_id}
    logger.info(f"walker: run {run.id} queued message {message_id} (node {node.id})")
    return {f"message_{node.id}": message_id}
