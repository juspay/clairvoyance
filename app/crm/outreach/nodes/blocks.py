"""The one async seam the playbook needs (modules/05-outreach §The
playbook) — where a square asks for its blocks.

Not in ``nodes/context.py``: that file is a leaf which imports the schemas
and nothing else from outreach, and this needs the playbook and identity.
Everything downstream of here is pure (outreach/playbook.py).
"""

from typing import Dict, Iterable, Tuple

from app.crm.identity.contracts import customer_facts
from app.crm.outreach import playbook, predicates
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode


async def blocks_for(
    run: EnrollmentRun,
    node: WorkflowNode,
    definition: WorkflowDefinition,
    asked: Iterable[str],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """ONLY the blocks this square asked for: (rendered, chosen).

    `rendered` is {block: text} for the payload; `chosen` is {block: line}
    — the NAME of the row that won, which the caller records as
    playbook_<node> for the funnel. Names only: the text rides the payload
    and never the run row (canon T20 col 12).

    A `when` that reads customer.* costs the same single identity read a
    condition square pays, and it is paid only when a row asks for it.

    Three squares ask, each in its own words and never implicitly — a call
    in its `blocks` list, a send on the right of `variables`, an action
    inside `args`. A block nobody names is never evaluated.
    """
    if definition is None or definition.playbook is None:
        return {}, {}
    wanted = [name for name in asked if name in definition.playbook.blocks]
    if not wanted:
        return {}, {}
    facts = run_facts(run.context, node)
    stage_facts = run.context.get("facts")
    stage_facts = stage_facts if isinstance(stage_facts, dict) else {}
    customer = None
    if playbook.needs_customer(definition, wanted):
        customer = await customer_facts(run.merchant_id, str(run.customer_id))
    return playbook.resolve(
        definition,
        wanted,
        facts,
        stage_facts,
        customer,
        predicates.RunLens(run.context, definition.exits),
    )
