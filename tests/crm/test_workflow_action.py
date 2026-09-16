"""The action node (modules/05-outreach): a square that asks a CONNECTOR to
do one thing for the run. The plan document names a connector, an action and
that action's own args — never a URL, a credential or a transport, which is
what lets the thing carrying the write change under a published plan.

Two failures, two answers: a DEFECT (unknown action, args that do not fit, a
refusal from the destination) parks; a BAD MOMENT (timeout, 5xx, 429) leaves
as itself so the walker's lease ladder re-sends it."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
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
    ) -> Dict[str, Any]:
        calls.append(
            {
                "merchant_id": merchant_id,
                "connector": connector_key,
                "action": action,
                "args": args,
                "context": context,
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

    # The action's OWN normalised facts, kept rather than discarded — under
    # the square's `action_` bookkeeping key, so they stay out of run_facts
    # and can never reach a template. This assertion is what stops
    # "responses are normalised" from being a promise with no reader.
    # (`reply_tag-vip: done` rides along — the answer a `done`/`failed`
    # arrow would pick on, cleared when the token leaves the square.)
    assert result == {"action_tag-vip": {"ok": True}, "reply_tag-vip": "done"}
    assert calls == [
        {
            "merchant_id": "m1",
            "connector": "shopify",
            "action": "add_tag",
            # Stringified, and the literal left alone beside the resolved one.
            # EVERY value the connector needs is here: the args are the whole
            # contract, resolved from the plan's own placeholders.
            "args": {"order_id": "5408422249", "tags": ["CONFIRM", "vip"]},
            # Bookkeeping ONLY — the two halves of the idempotency key. A
            # provider reading data from here would be reading outreach's
            # context shape from inside connectivity.
            "context": {"run_id": str(run.id), "node_id": "tag-vip"},
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


# --- enh A/03: an action's answer becomes facts, and may branch -----------------


def _definition_with_arrows(*edges: Any, args: Dict[str, Any] | None = None):
    return WorkflowDefinition(
        entry={"topic": "checkout.initiated"},
        nodes=[
            {
                "id": "tag-vip",
                "type": "action",
                "connector": "shopify",
                "action": "add_tag",
                "args": args or {"order_id": "{id}", "tags": ["vip"]},
            },
            {"id": "ring", "type": "call", "template_id": "tpl"},
            {"id": "wait-1d", "type": "wait", "minutes": 1440},
        ],
        edges=list(edges),
        goals=[{"topics": ["order.placed"]}],
    )


async def test_the_answers_facts_land_at_the_top_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merchant endpoint's answer (its `facts`) is what a LATER square
    reads: written at the top level like the founding letter's facts, so
    the next call carries {payment_link} AND still carries the latest
    letter's own facts (an action is not a letter and must not demote
    one). Nothing else of the answer leaves the bookkeeping key; a value
    the entry would drop is dropped here too."""
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)

    async def with_facts(*a: Any, **k: Any) -> Dict[str, Any]:
        return {
            "ok": True,
            "status": 200,
            "facts": {
                "payment_link": "https://p/1",
                "phone": "+91",
                "flag": True,
                "nested": {"x": 1},
                "gone": None,
            },
        }

    monkeypatch.setattr(action_node, "perform_action", with_facts)
    run = _run(
        {
            "id": "1",
            "loan_state": "CLA",
            "facts": {"listen": {"loan_state": "OFFERED", "offers": "1. A"}},
            "latest_letter": "listen",
        }
    )
    written = await execute_action(run, _node(), _DEFINITION)
    assert written["payment_link"] == "https://p/1"
    assert written["reply_tag-vip"] == "done"
    assert "facts" not in written and "latest_letter" not in written
    assert "phone" not in written and "flag" not in written and "nested" not in written
    facts = run_facts({**run.context, **written})
    assert facts["payment_link"] == "https://p/1"
    assert (
        facts["loan_state"] == "OFFERED" and facts["offers"] == "1. A"
    )  # the letter still wins
    assert "ok" not in facts and "status" not in facts


def test_a_declared_fact_may_not_take_a_walker_name() -> None:
    problems = _validate_action(
        _node(
            connector="merchant_http",
            action="request",
            args={
                "path": "/x",
                "facts": {"phone": "data.phone", "payment_link": "data.link"},
            },
        ),
        _DEFINITION,
    )
    assert problems == [
        "action node tag-vip: response fact 'phone' is a walker name — pick another"
    ]


async def test_a_defect_takes_the_failed_arrow_when_the_plan_drew_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `done`/`failed` arrows a refusal is an ANSWER, not a parked run:
    the author said what to do when the endpoint says no. Without the
    arrow, today's behaviour — the run parks."""
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls, raises=ActionError("refused (404)"))
    drawn = _definition_with_arrows(
        ["tag-vip", "ring", "done"], ["tag-vip", "wait-1d", "failed"]
    )
    written = await execute_action(_run({"id": "1"}), _node(), drawn)
    assert written["reply_tag-vip"] == "failed"
    assert written["action_tag-vip"] == {"ok": False, "error": "refused (404)"}
    assert "facts" not in written

    plain = _definition_with_arrows(["tag-vip", "ring"])
    with pytest.raises(NodeParked, match="refused"):
        await execute_action(_run({"id": "1"}), _node(), plain)

    # a BAD MOMENT is never an answer, arrows or not
    _install(monkeypatch, calls, raises=RuntimeError("502"))
    with pytest.raises(RuntimeError):
        await execute_action(_run({"id": "1"}), _node(), drawn)


async def test_placeholders_resolve_inside_a_nested_map_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An http body or query is a map inside args: `{customer_id}` there is
    the same lookup as at the top, and the catalog law walks it too."""
    from app.crm.outreach.nodes.action import placeholder_names

    args = {"path": "/link", "body": {"customer_id": "{cid}", "meta": {"n": "{n}"}}}
    assert placeholder_names(args) == ["cid", "n"]
    calls: List[Dict[str, Any]] = []
    _install(monkeypatch, calls)
    await execute_action(_run({"cid": "FK1", "n": 3}), _node(args=args), _DEFINITION)
    assert calls[0]["args"] == {
        "path": "/link",
        "body": {"customer_id": "FK1", "meta": {"n": "3"}},
    }
