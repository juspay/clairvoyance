"""condition — labelled edges chosen by a predicate over what is already
known (enh A/01).

A square that reads facts in hand and picks an edge WITHOUT waiting: "cart
above 5,000 -> call, else WhatsApp", "no phone on file -> end". Rules are
judged in document order; the first whose conditions ALL hold names the
edge, none takes the mandatory ``else``. A predicate never raises, never
parks: a missing field, a non-numeric side of an ordering op, a customer we
cannot read — each is ``else``. The op grammar is the ONE where-grammar the
corpus sealed (shared/predicate.Condition, the door's own ``where``); the
FIELD grammar is outreach/predicates.py.
"""

from typing import Any, Dict, List, Optional

from app.crm.identity.contracts import customer_facts
from app.crm.outreach import predicates
from app.crm.outreach.nodes.context import reply_key, run_facts
from app.crm.outreach.nodes.wait_event import ELSE
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    """The rules must name real fields, every label must have an edge and an
    `else` edge must exist — all at publish. Op/value fit is Condition's own
    validator (the sealed where-grammar), so nothing is re-spelled here."""
    problems: List[str] = []
    if not node.rules:
        return [f"condition node {node.id} needs at least one rule"]
    seen: set = set()
    for rule in node.rules:
        if rule.on in seen:
            problems.append(f"condition node {node.id}: label {rule.on!r} twice")
        seen.add(rule.on)
    node_ids = [n.id for n in definition.nodes]
    for field in sorted(predicates.fields_named(node.rules)):
        problems.extend(
            f"condition node {node.id}: {p}"
            for p in predicates.field_problems(field, node_ids)
        )
    labels = {on for _, on in definition.outgoing().get(node.id, [])}
    for rule in node.rules:
        if rule.on not in labels:
            problems.append(
                f"condition node {node.id}: rule {rule.on!r} has no edge labelled with it"
            )
    if ELSE not in labels:
        problems.append(
            f"condition node {node.id} needs an 'else' edge — no rule holding is "
            "the honest outcome, never a parked run"
        )
    return problems


def decide(node: WorkflowNode, context: Dict[str, Any]) -> Optional[str]:
    """PURE: the label this square names from facts alone — the dry run's
    half of execute (enh A/05).

    No customer, deliberately. A simulation has no customer to read, and
    inventing one would answer a question the author did not ask; a rule
    that names customer.* simply does not hold, which is the same thing
    execute does for a customer it cannot read. The dry run says so beside
    the step rather than leaving the author to wonder."""
    facts = run_facts(context, node)
    stage_facts = context.get("facts")
    stage_facts = stage_facts if isinstance(stage_facts, dict) else {}
    return predicates.choose(node.rules, facts, stage_facts, None)


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Pick the edge from what is already known. The one read a condition
    may cost — the customer's predicate-safe facts through identity's
    contract — is paid only when a rule names customer.*; a customer we
    cannot read makes those rules not hold, never an error. The answer
    rides reply_<node> exactly like a listening square's, so pick_next and
    the reply clearing on advance treat both alike."""
    facts = run_facts(run.context, node)
    stage_facts = run.context.get("facts")
    stage_facts = stage_facts if isinstance(stage_facts, dict) else {}
    customer = None
    if predicates.needs_customer(node.rules):
        customer = await customer_facts(run.merchant_id, str(run.customer_id))
    chosen = predicates.choose(node.rules, facts, stage_facts, customer)
    return {reply_key(node.id): chosen if chosen is not None else ELSE}
