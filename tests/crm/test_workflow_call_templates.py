"""The call square's template, chosen by condition.

A call square used to name one template and fire it. Choosing between two
meant drawing a `condition` square and TWO call squares — duplicating the
visit counter, the arrows and the stage label to swap a single id.

`template_rules` is the if/else-if ladder for that choice, over the same
fields and the same sealed where-grammar a condition square speaks. The
square itself does not branch: one arrow out, one lead, one counter — only
the cargo differs. The node's own `template_id` is the `else`, so no arm
holding fires the default rather than parking the run.
"""

from typing import Any, Dict, List, Optional

import pytest

import app.crm.outreach.nodes.call as call_node
from app.crm.identity.schemas import CustomerFacts
from app.crm.outreach.nodes import NODE_TYPES, branches
from app.crm.outreach.nodes.call import execute, validate
from app.crm.outreach.nodes.spec import NodeParked
from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode

_BRANDED = {
    "if": [{"field": "context.brand", "op": "exists"}],
    "template_id": "tpl-branded",
}


def _node(rules: Optional[List[Dict[str, Any]]] = None) -> WorkflowNode:
    return WorkflowNode(
        id="rescue-call",
        type="call",
        template_id="tpl-generic",
        template_rules=rules or [],
    )


def _definition(node: WorkflowNode) -> WorkflowDefinition:
    return WorkflowDefinition(
        entry={"topic": "orders/create"},
        nodes=[node],
        edges=[],
        goals=[{"topics": ["orders/paid"]}],
    )


def _run(context: Optional[Dict[str, Any]] = None) -> EnrollmentRun:
    return EnrollmentRun(
        id="6f6603bf-1bf5-4f46-b242-58e9f40833d2",
        merchant_id="m1",
        workflow_id="11111111-1111-1111-1111-111111111111",
        workflow_version=1,
        customer_id="22222222-2222-2222-2222-222222222222",
        status="waiting",
        current_node="rescue-call",
        wake_at=None,
        entered_at="2026-09-09T11:26:00Z",
        exited_at=None,
        exit_reason=None,
        context={"phone": "+919110752252", **(context or {})},
        enrollment_key="k1",
        attempts=0,
        last_error=None,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    asked: List[str],
    *,
    payloads: Optional[List[Dict[str, Any]]] = None,
    template_merchant: Optional[str] = None,
    customer: Optional[CustomerFacts] = None,
    reads: Optional[List[str]] = None,
) -> None:
    """The accessors as they really behave, recording which template id the
    square asked for — that id IS the outcome under test."""

    async def fake_template(template_id: str) -> Any:
        asked.append(template_id)
        return type(
            "T",
            (),
            {
                "id": template_id,
                "name": f"name-of-{template_id}",
                "reseller_id": "r1",
                "merchant_id": template_merchant,
            },
        )()

    async def fake_config(_id: str) -> Any:
        return type("C", (), {"initial_offset": 0})()

    async def fake_create(**kw: Any) -> Any:
        if payloads is not None:
            payloads.append(kw["payload"])
        return type("L", (), {"id": kw["id"]})()

    async def fake_stamp(_lead_id: str, _run_id: str) -> None:
        return None

    async def fake_customer_facts(merchant_id: str, customer_id: str) -> Any:
        if reads is not None:
            reads.append(customer_id)
        return customer

    monkeypatch.setattr(call_node, "get_template_by_id", fake_template)
    monkeypatch.setattr(
        call_node, "get_call_execution_config_by_template_id", fake_config
    )
    monkeypatch.setattr(call_node, "create_lead_call_tracker", fake_create)
    monkeypatch.setattr(call_node, "update_lead_enrollment_id", fake_stamp)
    monkeypatch.setattr(call_node, "customer_facts", fake_customer_facts)


# --- the choice ---------------------------------------------------------------


async def test_a_square_with_no_arms_fires_its_own_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The untouched path: every plan written before this existed."""
    asked: List[str] = []
    _install(monkeypatch, asked)
    node = _node()

    await execute(_run({"brand": "Nike"}), node, _definition(node))

    assert asked == ["tpl-generic"]


async def test_an_absent_fact_falls_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`context.brand` is not there, so the `exists` arm does not hold and
    the square's own template — the `else` — fires. A missing field is not
    an error and never parks the run."""
    asked: List[str] = []
    _install(monkeypatch, asked)
    node = _node([_BRANDED])

    await execute(_run(), node, _definition(node))

    assert asked == ["tpl-generic"]


async def test_a_present_fact_takes_the_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[str] = []
    _install(monkeypatch, asked)
    node = _node([_BRANDED])

    await execute(_run({"brand": "Nike"}), node, _definition(node))

    assert asked == ["tpl-branded"]


async def test_a_null_fact_reads_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer who posts `brand: null` said "nothing here", not "a brand
    named null" — run_facts drops it and the arm does not hold."""
    asked: List[str] = []
    _install(monkeypatch, asked)
    node = _node([_BRANDED])

    await execute(_run({"brand": None}), node, _definition(node))

    assert asked == ["tpl-generic"]


async def test_the_arms_are_an_if_else_if_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three arms and a default, judged in document order: the first that
    holds names the template and the rest are never reached. This is what
    makes it a ladder rather than a two-way split."""
    node = _node(
        [
            {
                "if": [{"field": "context.brand", "op": "not_exists"}],
                "template_id": "tpl-no-brand",
            },
            {
                "if": [
                    {"field": "context.brand", "op": "is", "value": "Nike"},
                    {"field": "context.cart_value", "op": ">", "value": 5000},
                ],
                "template_id": "tpl-nike-highvalue",
            },
            {
                "if": [{"field": "context.brand", "op": "is", "value": "Nike"}],
                "template_id": "tpl-nike",
            },
        ]
    )
    definition = _definition(node)

    async def fired(context: Dict[str, Any]) -> str:
        asked: List[str] = []
        _install(monkeypatch, asked)
        await execute(_run(context), node, definition)
        return asked[0]

    assert await fired({}) == "tpl-no-brand"
    assert await fired({"brand": "Nike", "cart_value": 9000}) == "tpl-nike-highvalue"
    # Both Nike arms could hold; document order decides, so the narrow one
    # placed above wins and the broad one below is simply not reached.
    assert await fired({"brand": "Nike", "cart_value": 100}) == "tpl-nike"
    assert await fired({"brand": "Adidas"}) == "tpl-generic"


async def test_conditions_within_one_arm_are_anded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arm holds only when ALL of its conditions do — OR is two arms."""
    node = _node(
        [
            {
                "if": [
                    {"field": "context.brand", "op": "is", "value": "Nike"},
                    {"field": "context.cart_value", "op": ">", "value": 5000},
                ],
                "template_id": "tpl-both",
            }
        ]
    )
    definition = _definition(node)

    async def fired(context: Dict[str, Any]) -> str:
        asked: List[str] = []
        _install(monkeypatch, asked)
        await execute(_run(context), node, definition)
        return asked[0]

    assert await fired({"brand": "Nike", "cart_value": 9000}) == "tpl-both"
    assert await fired({"brand": "Nike", "cart_value": 100}) == "tpl-generic"
    assert await fired({"cart_value": 9000}) == "tpl-generic"


# --- what the choice costs ----------------------------------------------------


async def test_the_customer_is_read_once_when_an_arm_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[str] = []
    reads: List[str] = []
    _install(
        monkeypatch,
        asked,
        customer=CustomerFacts(primary_locale="hi", has_phone=True),
        reads=reads,
    )
    node = _node(
        [
            {
                "if": [{"field": "customer.primary_locale", "op": "is", "value": "hi"}],
                "template_id": "tpl-hindi",
            }
        ]
    )

    await execute(_run(), node, _definition(node))

    assert asked == ["tpl-hindi"]
    assert len(reads) == 1, "the customer read is paid exactly once"


async def test_the_customer_is_never_read_unasked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one DB read an arm may cost is paid only when an arm asks for
    it — a square judging context alone touches identity not at all."""
    asked: List[str] = []
    reads: List[str] = []
    _install(monkeypatch, asked, reads=reads)
    node = _node([_BRANDED])

    await execute(_run({"brand": "Nike"}), node, _definition(node))

    assert asked == ["tpl-branded"]
    assert reads == []


async def test_a_square_with_no_arms_reads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[str] = []
    reads: List[str] = []
    _install(monkeypatch, asked, reads=reads)
    node = _node()

    await execute(_run(), node, _definition(node))

    assert reads == []


# --- what the choice may not do -----------------------------------------------


async def test_a_chosen_template_may_not_belong_to_another_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merchant check guards whichever template an arm named, not just
    the default — an arm is not a way round it."""
    asked: List[str] = []
    _install(monkeypatch, asked, template_merchant="someone-else")
    node = _node([_BRANDED])

    with pytest.raises(NodeParked, match="another merchant"):
        await execute(_run({"brand": "Nike"}), node, _definition(node))

    assert asked == ["tpl-branded"], "it was chosen, then refused"


async def test_an_arm_never_reads_the_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lead payload carries the handle, so the choice is judged BEFORE
    it is added: a rule naming context.customer_mobile_number finds nothing,
    exactly as the handle laws require."""
    asked: List[str] = []
    payloads: List[Dict[str, Any]] = []
    _install(monkeypatch, asked, payloads=payloads)
    node = _node(
        [
            {
                "if": [{"field": "context.customer_mobile_number", "op": "exists"}],
                "template_id": "tpl-leaked",
            }
        ]
    )

    await execute(_run(), node, _definition(node))

    assert asked == ["tpl-generic"], "the handle is invisible to an arm"
    assert payloads[0]["customer_mobile_number"] == "+919110752252"


# --- publish ------------------------------------------------------------------


def test_an_arm_naming_a_nonsense_field_is_refused() -> None:
    node = _node(
        [{"if": [{"field": "nonsense.field", "op": "exists"}], "template_id": "t"}]
    )

    problems = validate(node, _definition(node))

    assert len(problems) == 1
    assert "is not a condition field" in problems[0]
    assert "call node rescue-call" in problems[0]


def test_an_arm_may_not_read_a_handle() -> None:
    node = _node(
        [
            {
                "if": [{"field": "customer.attributes.phone", "op": "exists"}],
                "template_id": "t",
            }
        ]
    )

    problems = validate(node, _definition(node))

    assert len(problems) == 1
    assert "a handle is never readable by a predicate" in problems[0]


def test_a_well_formed_ladder_publishes_clean() -> None:
    node = _node([_BRANDED])

    assert validate(node, _definition(node)) == []
    assert NODE_TYPES["call"].validate(node, _definition(node)) == []


def test_a_square_still_needs_its_default_template() -> None:
    node = WorkflowNode(id="rescue-call", type="call", template_rules=[_BRANDED])

    problems = validate(node, _definition(node))

    assert len(problems) == 1
    assert "needs a template_id" in problems[0]


def test_the_call_square_still_does_not_branch() -> None:
    """Choosing cargo is not choosing a road: the square keeps its one plain
    arrow, so pick_next takes it without consulting any reply."""
    assert NODE_TYPES["call"].branches is False
    assert branches(_node([_BRANDED])) is False
