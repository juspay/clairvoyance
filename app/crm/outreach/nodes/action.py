"""action — synchronous, no dispatch machine: ask a connector to DO one
thing for this run (connectivity's perform_action).

The square names a connector and an action and nothing else — no URL, no
credential, no transport — so the thing that carries the write can change
under a published plan. Two failures, two answers: a DEFECT (unknown
action, args that do not fit, a 4xx) parks; a BAD MOMENT (timeout, 5xx,
429) leaves as itself and the lease ladder re-sends. Safe to repeat: the
action carries a deterministic (run, node) key.
"""

from typing import Any, Dict, List

from app.crm.connectivity.contracts import (
    ActionError,
    action_names,
    perform_action,
    validate_action_args,
)
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode


def validate(node: WorkflowNode, definition: WorkflowDefinition) -> List[str]:
    """The connector and the action must exist, and the args must fit the
    action's own model — all at PUBLISH.

    An unknown action discovered at run time is a parked run an operator has
    to find; discovered here it is a sentence the author reads while they are
    still editing. The args are validated against the registry's model rather
    than by anything spelled here, which is what keeps this function free of
    a branch per connector.

    SHAPE only. Placeholders are left in place for the check — `{id}` is not
    a real order id yet — so the model is validated over a copy with every
    `{...}` value replaced by a plausible non-empty stand-in. Whether each
    placeholder NAMES a fact the door's topic declares is the catalog law,
    checked at publish in catalog_laws.py where the catalog is at hand; it is
    the same allow-list a send node's variables answer to.
    """
    problems: List[str] = []
    if not node.connector:
        problems.append(f"action node {node.id} needs a connector")
    if not node.action:
        problems.append(f"action node {node.id} needs an action")
    if problems:
        return problems

    known = action_names(str(node.connector))
    if not known:
        return [
            f"action node {node.id}: no connector '{node.connector}', or it "
            f"declares no actions"
        ]
    if node.action not in known:
        return [
            f"action node {node.id}: connector '{node.connector}' has no "
            f"action '{node.action}' (has: {', '.join(known)})"
        ]

    bad = validate_action_args(
        str(node.connector), str(node.action), args_for_check(node.args)
    )
    if bad:
        problems.append(f"action node {node.id}: bad args ({', '.join(bad)})")
    return problems


def describe(
    node: WorkflowNode, context: Dict[str, Any], definition: WorkflowDefinition
) -> Dict[str, Any]:
    """PURE: the write this square WOULD ask a connector to perform (enh
    A/05) — the app, the verb and the args with every {placeholder}
    resolved by the same resolved_args execute uses. A missing fact raises
    KeyError naming it, which is the park an author most wants to see
    before a customer does."""
    facts = run_facts(context, node)
    try:
        args = resolved_args(node.args, facts)
    except KeyError as e:
        raise ValueError(
            f"the letter carries no {e.args[0]!r} — this action would park"
        ) from e
    return {"connector": node.connector, "action": node.action, "args": args}


async def execute(
    run: EnrollmentRun, node: WorkflowNode, definition: WorkflowDefinition
) -> Dict[str, Any]:
    """Ask a connector to do one thing for this run, through connectivity's
    contract.

    Nothing here knows a URL, a credential or a transport. The node named a
    connector and an action; the installation supplies the rest. That is what
    lets the thing carrying the write change — a relay today, the provider
    directly once we hold its token — without a plan document, this function,
    or a stored workflow version changing.

    The ARGS are the whole message. Every value the connector needs is
    resolved here, from the plan's own `{placeholder}`s against the run's
    facts, and handed over as the action's typed model — the send node's law
    (canon T19 col 6: a send's `variables` map is EXACTLY what is posted)
    applied to the fourth verb. ``context`` carries the run and node ids and
    nothing else: a provider that reached into it for data would be reading
    outreach's context shape from inside connectivity, and the args model
    would stop being the contract it claims to be.

    Two failures, two exception classes, because the walker treats them
    differently:

    - `ActionError` is a DEFECT — no such connector or action, args that do
      not fit, a 4xx from the destination. NodeParked, the same honest stop
      the call square makes for a missing template.
    - Anything else is a BAD MOMENT — a timeout, a 5xx, a 429. It leaves as
      itself so the walker's ladder backs off and re-sends, instead of
      parking a run because a deploy was mid-flight.

    Retrying is safe by design: the action is at-least-once and carries a
    deterministic `(run, node)` key, so a re-send after a lost response is
    recognisable as the same write rather than a second one.
    """
    if not node.connector or not node.action:
        # The validator refuses this at publish; a version predating the
        # validator would otherwise reach the registry with None.
        raise NodeParked(f"action node {node.id}: no connector/action to perform")

    facts = run_facts(run.context, node)
    try:
        args = resolved_args(node.args, facts)
    except KeyError as e:
        raise NodeParked(
            f"action node {node.id}: no fact {e.args[0]} for a placeholder in args"
        ) from e

    try:
        result = await perform_action(
            run.merchant_id,
            node.connector,
            node.action,
            args,
            # Bookkeeping ONLY — the idempotency key's two halves. Never
            # data: connectivity's own contract says so, and an action that
            # learned to read a fact from here would couple a provider to
            # this module's context shape.
            {"run_id": str(run.id), "node_id": node.id},
        )
    except ActionError as e:
        raise NodeParked(f"action node {node.id}: {e}") from e
    # The action's OWN normalised facts, kept under the square's bookkeeping
    # key rather than discarded. `action_` is a bookkeeping prefix, so this
    # stays out of run_facts and can never reach a template — but it is on
    # the run for whoever triages it, and a test asserts its shape, which is
    # what stops "responses are normalised" from becoming a promise with no
    # reader.
    return {f"action_{node.id}": result}


# --- the pure arg helpers (this square's own; nothing else resolves args) ---


def args_for_check(args: Dict[str, Any]) -> Dict[str, Any]:
    """PURE: the args with every placeholder replaced by a stand-in, so the
    SHAPE can be validated at publish without knowing what the run holds.

    `{id}` is not an order id yet; refusing it would make every real plan
    unpublishable, and skipping validation for any node carrying one would
    make the check useless exactly where authors make mistakes. A non-empty
    stand-in satisfies min_length without asserting anything about the value.
    """
    return {key: _stand_in(value) for key, value in args.items()}


def _stand_in(value: Any) -> Any:
    """PURE: one placeholder -> a plausible non-empty value, recursively
    through lists (tags are a list of them)."""
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        return "x"
    if isinstance(value, list):
        return [_stand_in(item) for item in value]
    return value


def placeholder_names(args: Dict[str, Any]) -> List[str]:
    """PURE: every fact name an args map asks the run for, in order, with
    duplicates dropped. What the catalog law checks against the door's
    declared fields — the same allow-list a send node's variables answer to."""
    names: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            name = value[1:-1]
            if name and name not in names:
                names.append(name)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for value in args.values():
        walk(value)
    return names


def resolved_args(args: Dict[str, Any], facts: Dict[str, Any]) -> Dict[str, Any]:
    """PURE: the args with every `{placeholder}` replaced by the run's fact
    of that name. Raises KeyError naming the first fact that is missing —
    the caller parks, because an action performed on a half-resolved
    argument writes to the wrong thing rather than to nothing.
    """
    return {key: _resolve(value, facts) for key, value in args.items()}


def _resolve(value: Any, facts: Dict[str, Any]) -> Any:
    """PURE: one arg value, resolved. Whole-string placeholders only — the
    same rule the rest of the board uses — so `{id}` becomes the fact and
    `order-{id}` is left alone as the literal an author wrote."""
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        name = value[1:-1]
        fact = facts.get(name)
        if fact is None or fact == "":
            raise KeyError(name)
        return str(fact)
    if isinstance(value, list):
        return [_resolve(item, facts) for item in value]
    return value
