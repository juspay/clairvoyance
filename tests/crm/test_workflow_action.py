"""The action node (modules/05-outreach): a square that asks a CONNECTOR to
do one thing for the run. The plan document names a connector, an action and
that action's own args — never a URL, a credential or a transport, which is
what lets the thing carrying the write change under a published plan.

Two failures, two answers: a DEFECT (unknown action, args that do not fit, a
refusal from the destination) parks; a BAD MOMENT (timeout, 5xx, 429) leaves
as itself so the walker's lease ladder re-sends it."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

import app.crm.outreach.nodes.action as action_node
from app.crm.connectivity.contracts import ActionError
from app.crm.outreach.nodes.action import (
    execute as execute_action,
    validate as _validate_action,
)
from app.crm.outreach.nodes.context import run_facts
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

_DEFINITION = WorkflowDefinition(
    entry={"topic": "checkout.initiated"},
    nodes=[
        {
            "id": "tag-vip",
            "type": "action",
            "connector": "shopify",
            "action": "add_tag",
            "args": {"order_id": "{id}", "tags": ["vip"]},
        }
    ],
    edges=[],
    goals=[{"topics": ["order.placed"]}],
)


def _run(context: Dict[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="tag-vip",
        wake_at=NOW,
        entered_at=NOW - timedelta(minutes=5),
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="c-1",
        attempts=1,
        last_error=None,
    )


def _node(**overrides: Any) -> WorkflowNode:
    fields: Dict[str, Any] = {
        "id": "tag-vip",
        "type": "action",
        "connector": "shopify",
        "action": "add_tag",
        "args": {"order_id": "{id}", "tags": ["vip"]},
    }
    fields.update(overrides)
    return WorkflowNode(**fields)


# --- publish: an author hears it while they are still editing ------------------


def test_a_square_that_names_nobody_is_refused() -> None:
    assert _validate_action(_node(connector=None), _DEFINITION) == [
        "action node tag-vip needs a connector"
    ]
    assert _validate_action(_node(action=None), _DEFINITION) == [
        "action node tag-vip needs an action"
    ]


def test_an_unknown_connector_or_action_is_refused_by_name() -> None:
    assert _validate_action(_node(connector="zendesk"), _DEFINITION) == [
        "action node tag-vip: no connector 'zendesk', or it declares no actions"
    ]
    # A connector with no action face reads the same way, deliberately.
    assert _validate_action(_node(connector="whatsapp"), _DEFINITION) == [
        "action node tag-vip: no connector 'whatsapp', or it declares no actions"
    ]
    # The alternatives are named: `add_tags` is one sentence from right.
    assert _validate_action(_node(action="add_tags"), _DEFINITION) == [
        "action node tag-vip: connector 'shopify' has no action 'add_tags' "
        "(has: add_note, add_tag, update_order)"
    ]


def test_the_args_are_checked_against_the_connectors_own_model() -> None:
    assert _validate_action(_node(), _DEFINITION) == []
    assert _validate_action(_node(args={}), _DEFINITION) == [
        "action node tag-vip: bad args (order_id, tags)"
    ]
    assert _validate_action(_node(args={"order_id": "{id}"}), _DEFINITION) == [
        "action node tag-vip: bad args (tags)"
    ]


def test_a_placeholder_is_not_mistaken_for_a_bad_argument() -> None:
    """`{id}` is not an order id yet. Refusing it would make every real plan
    unpublishable; skipping the check for any node carrying one would make it
    useless exactly where authors make mistakes."""
    node = _node(args={"order_id": "{id}", "tags": ["{outcome}", "vip"]})
    assert _validate_action(node, _DEFINITION) == []


# --- run time ------------------------------------------------------------------


def _install(monkeypatch: pytest.MonkeyPatch, calls: List[Dict[str, Any]], **kw: Any):
    """Stand in for connectivity's contract, the only thing this node calls."""

    async def fake_perform(
        merchant_id: str,
        connector_key: str,
        action: str,
        args: Dict[str, Any],
        context: Dict[str, Any],
        door_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        calls.append(
            {
                "merchant_id": merchant_id,
                "connector": connector_key,
                "action": action,
                "args": args,
                "context": context,
                "door_name": door_name,
            }
        )
        if "raises" in kw:
            raise kw["raises"]
        return {"ok": True}

    monkeypatch.setattr(action_node, "perform_action", fake_perform)


async def test_the_args_reach_the_connector_with_placeholders_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    run = _run({"id": 5408422249, "outcome": "CONFIRM"})

    result = await execute_action(
        run,
        _node(args={"order_id": "{id}", "tags": ["{outcome}", "vip"]}),
        _DEFINITION,
    )

    # The action's OWN normalised result, kept rather than discarded — under
    # the square's `action_` bookkeeping key with the visit it was, so it
    # stays out of run_facts and can never reach a template. This assertion
    # is what stops "responses are normalised" from being a promise with no
    # reader. Shopify declares no facts, so nothing else is written.
    assert result == {"action_tag-vip": {"ok": True, "visit": 1}}
    assert calls == [
        {
            "merchant_id": "m1",
            "connector": "shopify",
            "action": "add_tag",
            # Stringified, and the literal left alone beside the resolved one.
            # EVERY value the connector needs is here: the args are the whole
            # contract, resolved from the plan's own placeholders.
            "args": {"order_id": "5408422249", "tags": ["CONFIRM", "vip"]},
            # Bookkeeping ONLY — the idempotency key's parts. A provider
            # reading data from here would be reading outreach's context
            # shape from inside connectivity.
            "context": {"run_id": str(run.id), "node_id": "tag-vip", "visit": 1},
            # No door_name on the node: the connector's newest usable door.
            "door_name": None,
        }
    ]


async def test_the_marker_is_bookkeeping_and_never_a_template_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    run = _run({"id": "1", "cart_value": 900})
    patch = await execute_action(run, _node(), _DEFINITION)

    merged = {**run.context, **patch}
    assert "action_tag-vip" not in run_facts(merged)
    assert run_facts(merged)["cart_value"] == 900


async def test_a_missing_fact_for_a_placeholder_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-resolved args write to the wrong order, not to nothing."""
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    with pytest.raises(NodeParked, match="no fact who for a placeholder"):
        await execute_action(
            _run({"id": 1, "outcome": "CONFIRM"}),
            _node(args={"tags": ["{who}"]}),
            _DEFINITION,
        )
    assert calls == []


async def test_a_defect_parks_and_a_bad_moment_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction the whole design rests on: parking a run because a
    deploy was mid-flight is as wrong as retrying a permanent refusal."""
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls, raises=ActionError("nautilus refused (401)"))
    with pytest.raises(NodeParked, match="nautilus refused"):
        await execute_action(_run({"id": "1"}), _node(), _DEFINITION)

    _install(monkeypatch, calls, raises=RuntimeError("nautilus answered 502"))
    with pytest.raises(RuntimeError) as caught:
        await execute_action(_run({"id": "1"}), _node(), _DEFINITION)
    assert not isinstance(caught.value, NodeParked)


async def test_a_version_predating_the_validator_parks_rather_than_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    with pytest.raises(NodeParked, match="no connector/action to perform"):
        await execute_action(_run({"id": "1"}), _node(action=None), _DEFINITION)
    assert calls == []


# --- a form's data: nested args, and the run's own facts (a Xyne request) ---


async def test_placeholders_inside_a_nested_object_are_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FlowJSON form is a dict of dicts; its placeholders resolve too —
    otherwise `{run_id}` reaches the provider as literal text and the reply
    can never be matched back to this run."""
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    run = _run({"id": "7", "phone": "+919876543210"})
    args = {
        "channel_id": "ch-1",
        "flow": {
            "data": {
                "enrollment_id": "{run_id}",
                "customer_mobile_number": "{customer_mobile_number}",
                "order": ["{id}", "literal"],
                "kind": "interview_availability",
            }
        },
    }

    await execute_action(run, _node(args=args), _DEFINITION)

    assert calls[0]["args"] == {
        "channel_id": "ch-1",
        "flow": {
            "data": {
                "enrollment_id": str(run.id),
                "customer_mobile_number": "+919876543210",
                "order": ["7", "literal"],
                "kind": "interview_availability",
            }
        },
    }


async def test_a_run_without_a_phone_parks_on_customer_mobile_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    node = _node(args={"flow": {"data": {"to": "{customer_mobile_number}"}}})
    with pytest.raises(NodeParked, match="customer_mobile_number"):
        await execute_action(_run({"id": "7"}), node, _DEFINITION)
    assert calls == []


def test_run_id_is_bookkeeping_and_never_a_template_fact() -> None:
    """A producer's own `run_id` field must not pose as the run's identity."""
    assert "run_id" not in run_facts({"run_id": "spoofed", "id": "1"})


# --- door_name: optional, picks one of the merchant's accounts -----------------


def test_door_name_is_optional_on_an_action_node() -> None:
    assert _node().door_name is None
    assert _node(door_name="second-shop").door_name == "second-shop"


async def test_the_node_door_name_reaches_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    await execute_action(_run({"id": "1"}), _node(door_name="second-shop"), _DEFINITION)
    assert calls[0]["door_name"] == "second-shop"


async def test_without_a_door_name_none_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    await execute_action(_run({"id": "1"}), _node(), _DEFINITION)
    assert calls[0]["door_name"] is None
