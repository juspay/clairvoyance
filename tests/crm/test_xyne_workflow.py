"""A workflow that enters on XyneSpaces' flow_action — the declare side of the
xyne integration: because the flow_action entry is a code-layer catalog entry
now, a workflow can key its first door on `entry.topic = "flow_action"` and
template its send node off the submission's declared variables.

The consumer and walker are generic (topic-driven, tested for every topic by
test_workflow_entry.py and test_worker_runtime.py) — nothing about triggering a
run off flow_action needs xyne-specific code. What a NEW source does owe is the
catalog: a workflow that filters/keys/templates on flow_action must only use
fields the xyne spec declares. That is what these pin — the catalog law
(entry_against_catalog) runs over the shipped flow_action entry, so a board
authored for it is judged on every CI run, not merely assumed to validate.
"""

from typing import Any, Dict, List

from app.crm.outreach import plans
from app.crm.outreach.catalog_laws import Catalogs
from app.crm.record.catalog import code_entries
from app.crm.record.schemas import CatalogEntry

TOPIC = "flow_action"


def _entry() -> CatalogEntry:
    """The code-layer flow_action entry — the one we declared."""
    (entry,) = [e for e in code_entries() if e.topic == TOPIC]
    return entry


def _catalogs() -> Catalogs:
    """The CODE catalog, as the server's gather builds it — now including the
    xyne flow_action entry (this is what makes the laws below real, not
    skipped)."""
    return {entry.topic: {f.path: f for f in entry.fields} for entry in code_entries()}


def _board(
    nodes: List[Dict[str, Any]],
    *,
    entry_where: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """A minimal workflow entering on flow_action."""
    return {
        "entry": {"topic": TOPIC, **({"where": entry_where} if entry_where else {})},
        "nodes": nodes,
        "edges": [],
        "goal": {"topics": ["order.placed"]},
        "purpose_key": "xyne.interview.slots",
    }


def _send_node(**words: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        "id": "confirm",
        "type": "send",
        "channel": "whatsapp",
        "template": "t",
    }
    node.update(words)
    return node


def test_a_flow_action_board_validates_against_the_code_catalog() -> None:
    """A send mapping a declared flow_action variable and filtering on another
    passes every catalog law — the board parses, publishes, and enrols."""
    board = _board(
        [_send_node(variables={"1": "selected_slots"})],
        entry_where=[
            {
                "field": "payload.actionId",
                "op": "is",
                "value": "submit-interview-availability",
            }
        ],
    )
    assert plans.validate_definition(board, catalogs=_catalogs()) == []


def test_a_where_on_a_declared_field_is_judged() -> None:
    """The entry's where clause is checked against the flow_action fields —
    `type` is declared, so the filter is legal."""
    board = _board(
        [_send_node(variables={"1": "selected_slots"})],
        entry_where=[{"field": "payload.type", "op": "is", "value": "submit"}],
    )
    assert plans.validate_definition(board, catalogs=_catalogs()) == []


def test_a_send_mapping_an_undeclared_field_is_refused() -> None:
    """The catalog law actually runs over flow_action: a send templating off a
    field the spec never declares is refused at publish, not at run time."""
    board = _board([_send_node(variables={"1": "loyalty_tier"})])
    problems = plans.validate_definition(board, catalogs=_catalogs())
    assert any("loyalty_tier" in p and "not a declared variable" in p for p in problems)


def test_a_where_on_an_undeclared_field_is_refused() -> None:
    """Same for the entry's where: a filter on a field flow_action never has
    is refused, so a stale condition cannot quietly match nothing."""
    board = _board(
        [_send_node(variables={"1": "selected_slots"})],
        entry_where=[{"field": "totally_bogus_field", "op": "is", "value": "x"}],
    )
    problems = plans.validate_definition(board, catalogs=_catalogs())
    assert any("totally_bogus_field" in p for p in problems)


def test_an_unknown_topic_is_not_flow_action() -> None:
    """A board entering on a topic no layer declares is refused before it can
    filter/key/template — the guard that makes declaring flow_action matter."""
    board = _board(
        [_send_node()], entry_where=[{"field": "payload.x", "op": "is", "value": "y"}]
    )
    board["entry"]["topic"] = "some.unseen_topic"
    problems = plans.validate_definition(board, catalogs=_catalogs())
    assert any("not in the catalog" in p for p in problems)


def test_the_variable_fields_a_board_can_map_are_the_declared_ones() -> None:
    """The declared variable fields of flow_action are exactly the names a
    send node may map — action name, envelope ids, and the one derived tool."""
    declared = {f.path for f in _entry().fields if f.variable}
    assert declared == {
        "payload.actionId",
        "payload.type",
        "payload.values.selected_dates",
        "selected_slots",
        "payload.context.messageId",
        "payload.context.conversationId",
        "payload.context.userId",
        "payload.context.flowJSON.data.kind",
    }
