"""The condition node (enh A/01): a square that reads facts already in
hand and picks a labelled edge without waiting. Rules are judged in
document order, the first whose conditions all hold wins, none -> `else`.
A predicate never raises, never parks: a missing field, a non-numeric
side of an ordering op, a customer we cannot read — every one of those is
`else`, the honest fallback.

The op grammar is the ONE where-grammar the corpus sealed
(shared/predicate.py; the door's `where` speaks it): text-strict `is`,
numeric `=` and ordering ops, `in`, `exists`. outreach/predicates.py owns
only the FIELD grammar — context.<key>, facts.<node>.<key>,
customer.<column>, customer.attributes.<name> — as a lookup.

Customer facts are the five whitelisted columns plus each asserted
attribute's WINNING claim (the facts.py ladder); handle VALUES are never
readable and handle-like attribute names are refused at publish."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

import app.crm.identity.facts as identity_facts
import app.crm.outreach.nodes.condition as condition_node
from app.crm.identity.contracts import CustomerFacts, customer_facts
from app.crm.identity.facts import winning_attributes
from app.crm.identity.schemas import CrmCustomer
from app.crm.outreach import predicates
from app.crm.outreach.nodes import NODE_TYPES
from app.crm.outreach.nodes.condition import (
    execute as execute_condition,
)
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import (
    ConditionRule,
    EnrollmentRun,
    WorkflowDefinition,
    WorkflowNode,
)
from app.crm.shared.predicate import Condition, matches

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _rule(on: str, **conditions: Any) -> ConditionRule:
    """{field: value} -> is; {field: (op, value)} -> that op."""
    ifs: List[Dict[str, Any]] = []
    for field, match in conditions.items():
        field = field.replace("__", ".")
        if isinstance(match, tuple):
            ifs.append({"field": field, "op": match[0], "value": match[1]})
        else:
            ifs.append({"field": field, "op": "is", "value": match})
    return ConditionRule.model_validate({"on": on, "if": ifs})


def _customer(**attributes: Any) -> CustomerFacts:
    return CustomerFacts(
        display_name="Priya",
        primary_locale="en-IN",
        timezone="Asia/Kolkata",
        has_phone=True,
        has_email=False,
        attributes=attributes,
    )


FACTS = {
    "requestedAmount": "50000.00",
    "loanState": "OFFERED",
    "current_node": "decide",
}
STAGE = {"quiet-30m": {"loanState": "KYC_COMPLETED", "eventName": "KYC_COMPLETED"}}


# --- choose: first match wins, none -> else ----------------------------------


def test_a_text_rule_is_exact_and_first_match_wins() -> None:
    rules = [
        _rule("offered", context__loanState="OFFERED"),
        _rule("any", context__loanState=("exists", None)),
    ]
    assert predicates.choose(rules, FACTS, {}, None) == "offered"
    assert predicates.choose(rules, {"loanState": "KYC"}, {}, None) == "any"
    assert predicates.choose(rules, {}, {}, None) is None


def test_ordering_ops_read_numeric_strings_and_refuse_text() -> None:
    big = [_rule("big", context__requestedAmount=(">=", 10000))]
    assert predicates.choose(big, FACTS, {}, None) == "big"
    assert predicates.choose(big, {"requestedAmount": "500"}, {}, None) is None
    assert predicates.choose(big, {"requestedAmount": "lots"}, {}, None) is None
    assert predicates.choose(big, {"requestedAmount": float("nan")}, {}, None) is None


def test_and_across_fields_and_in() -> None:
    rule = _rule(
        "hot",
        context__loanState=("in", ["OFFERED", "OFFER_SELECTED"]),
        context__requestedAmount=(">", 1000),
    )
    assert predicates.choose([rule], FACTS, {}, None) == "hot"
    assert predicates.choose([rule], {**FACTS, "loanState": "KYC"}, {}, None) is None


def test_a_stage_fact_and_a_missing_stage() -> None:
    # the node id carries a dash, so the rule is spelled out
    rule = ConditionRule.model_validate(
        {
            "on": "kyc-done",
            "if": [
                {
                    "field": "facts.quiet-30m.loanState",
                    "op": "is",
                    "value": "KYC_COMPLETED",
                }
            ],
        }
    )
    assert predicates.choose([rule], FACTS, STAGE, None) == "kyc-done"
    assert predicates.choose([rule], FACTS, {}, None) is None


def test_customer_columns_and_attribute_winners() -> None:
    has_phone = [_rule("call", customer__has_phone=True)]
    assert predicates.choose(has_phone, {}, {}, _customer()) == "call"
    assert predicates.choose(has_phone, {}, {}, None) is None, "no customer -> else"
    tier = [_rule("vip", customer__attributes__tier="gold")]
    assert predicates.choose(tier, {}, {}, _customer(tier="gold")) == "vip"
    assert predicates.choose(tier, {}, {}, _customer()) is None


def test_needs_customer_only_when_a_rule_names_one() -> None:
    assert predicates.needs_customer([_rule("x", context__a=1)]) is False
    assert predicates.needs_customer([_rule("x", customer__has_email=True)]) is True


# --- the field grammar, judged at publish -------------------------------------


@pytest.mark.parametrize(
    "field, ok",
    [
        ("context.requestedAmount", True),
        ("context.current_stage", True),
        ("facts.quiet-30m.loanState", True),
        ("customer.has_phone", True),
        ("customer.display_name", True),
        ("customer.attributes.tier", True),
        ("customer.phone", False),
        ("customer.email", False),
        ("customer.attributes.phone", False),
        ("customer.attributes.igsid", False),
        ("customer.attributes._handle_history", False),
        ("facts.nobody.loanState", False),
        ("payload.requestedAmount", False),
        ("requestedAmount", False),
    ],
)
def test_field_grammar(field: str, ok: bool) -> None:
    problems = predicates.field_problems(field, {"decide", "quiet-30m"})
    assert (problems == []) is ok, (field, problems)


# --- the customer side: identity's new contract ------------------------------


def _claim(v: Any, e: str, at: str) -> Dict[str, Any]:
    return {"v": v, "e": e, "k": 1.0, "at": at}


def test_winning_attributes_takes_the_ladder_and_hides_inferred_and_handles() -> None:
    history = {
        "gender": [
            _claim("F", "observed", "2026-09-05T00:00:00+00:00"),
            _claim("M", "declared", "2026-09-01T00:00:00+00:00"),
        ],
        "tier": [_claim("gold", "inferred", "2026-09-06T00:00:00+00:00")],
        "phone": [_claim("+919999999999", "declared", "2026-09-06T00:00:00+00:00")],
        "_handle_history": [{"phone": "+911111111111"}],
        "name": [_claim("Priya", "observed", "2026-09-06T00:00:00+00:00")],
    }
    assert winning_attributes(history) == {"gender": "M", "name": "Priya"}


def test_customer_facts_reads_the_row_through_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Accessor:
        async def get_customer(self, merchant_id: str, customer_id: str) -> Any:
            if customer_id != "c-1":
                return None
            return CrmCustomer(
                id=uuid4(),
                merchant_id=merchant_id,
                display_name="Priya",
                primary_locale=None,
                timezone="Asia/Kolkata",
                phone="+919876543210",
                email=None,
                status="active",
                first_seen_at=NOW,
                last_seen_at=NOW,
                created_at=NOW,
                updated_at=NOW,
                attributes={
                    "tier": [_claim("gold", "declared", "2026-09-01T00:00:00+00:00")]
                },
            )

    monkeypatch.setattr(identity_facts, "accessor", _Accessor())
    got = asyncio.run(customer_facts("m1", "c-1"))
    assert got == CustomerFacts(
        display_name="Priya",
        primary_locale=None,
        timezone="Asia/Kolkata",
        has_phone=True,
        has_email=False,
        attributes={"tier": "gold"},
    )
    assert asyncio.run(customer_facts("m1", "nobody")) is None


# --- the node: validator + execute -------------------------------------------


def _doc(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "entry": {"topic": "ORDER_CREATED"},
        "nodes": [
            {"id": "wait-30m", "type": "wait", "minutes": 30},
            {
                "id": "decide",
                "type": "condition",
                "rules": [
                    {
                        "on": "big",
                        "if": [
                            {
                                "field": "context.requestedAmount",
                                "op": ">=",
                                "value": 5000,
                            }
                        ],
                    }
                ],
            },
            {"id": "rescue-call", "type": "call", "template_id": "tpl-1"},
            {"id": "wait-1d", "type": "wait", "minutes": 1440},
        ],
        "edges": [
            ["wait-30m", "decide"],
            ["decide", "rescue-call", "big"],
            ["decide", "wait-1d", "else"],
        ],
        "goals": [{"topics": ["GRANTED"]}],
    }
    base.update(overrides)
    return base


def test_a_condition_board_validates() -> None:
    assert validate_definition(_doc()) == []


def test_the_registry_knows_the_word_and_it_branches() -> None:
    spec = NODE_TYPES["condition"]
    assert spec.branches is True and spec.is_wait is False and spec.listens is False
    assert NODE_TYPES["wait_event"].branches is True
    assert NODE_TYPES["wait_event"].listens is True
    assert NODE_TYPES["call"].branches is False and NODE_TYPES["send"].listens is False


def test_a_missing_else_edge_is_refused() -> None:
    doc = _doc(edges=[["wait-30m", "decide"], ["decide", "rescue-call", "big"]])
    assert any("else" in p for p in validate_definition(doc))


def test_a_rule_without_an_edge_and_a_reserved_label_are_refused() -> None:
    doc = _doc()
    doc["nodes"][1]["rules"].append(
        {
            "on": "small",
            "if": [{"field": "context.requestedAmount", "op": "<", "value": 5000}],
        }
    )
    assert any("small" in p and "edge" in p for p in validate_definition(doc))
    doc = _doc()
    doc["nodes"][1]["rules"][0]["on"] = "else"
    assert any("else" in p for p in validate_definition(doc))


def test_an_unknown_op_and_a_handle_field_are_refused() -> None:
    doc = _doc()
    doc["nodes"][1]["rules"][0]["if"][0]["op"] = "gte"
    assert any("shape invalid" in p for p in validate_definition(doc))
    doc = _doc()
    doc["nodes"][1]["rules"][0]["if"][0]["field"] = "customer.phone"
    assert any("customer.phone" in p for p in validate_definition(doc))


def test_an_unlabelled_edge_out_of_a_condition_is_refused() -> None:
    doc = _doc(
        edges=[
            ["wait-30m", "decide"],
            ["decide", "rescue-call"],
            ["decide", "wait-1d", "else"],
        ]
    )
    assert any("needs an on" in p for p in validate_definition(doc))


def _run(context: Dict[str, Any]) -> EnrollmentRun:
    return EnrollmentRun(
        id=uuid4(),
        merchant_id="m1",
        workflow_id=uuid4(),
        workflow_version=1,
        customer_id=uuid4(),
        status="waiting",
        current_node="decide",
        wake_at=NOW,
        entered_at=NOW - timedelta(minutes=5),
        exited_at=None,
        exit_reason=None,
        context=context,
        enrollment_key="c-1",
        attempts=1,
        last_error=None,
    )


def _node() -> WorkflowNode:
    return WorkflowNode.model_validate(_doc()["nodes"][1])


def test_execute_writes_the_reply_and_never_reads_the_customer_unasked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: List[str] = []

    async def never(merchant_id: str, customer_id: str) -> Optional[CustomerFacts]:
        reads.append(customer_id)
        return None

    monkeypatch.setattr(condition_node, "customer_facts", never)
    definition = WorkflowDefinition.model_validate(_doc())
    out = asyncio.run(
        execute_condition(_run({"requestedAmount": "6000"}), _node(), definition)
    )
    assert out == {"reply_decide": "big"}
    out = asyncio.run(
        execute_condition(_run({"requestedAmount": "1000"}), _node(), definition)
    )
    assert out == {"reply_decide": "else"}
    out = asyncio.run(execute_condition(_run({}), _node(), definition))
    assert out == {"reply_decide": "else"}
    assert reads == []


def test_execute_reads_the_customer_once_when_a_rule_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: List[str] = []

    async def once(merchant_id: str, customer_id: str) -> Optional[CustomerFacts]:
        reads.append(customer_id)
        return _customer(tier="gold")

    monkeypatch.setattr(condition_node, "customer_facts", once)
    node = WorkflowNode.model_validate(
        {
            "id": "decide",
            "type": "condition",
            "rules": [
                {
                    "on": "vip",
                    "if": [
                        {
                            "field": "customer.attributes.tier",
                            "op": "is",
                            "value": "gold",
                        }
                    ],
                },
                {
                    "on": "known",
                    "if": [{"field": "customer.has_phone", "op": "is", "value": True}],
                },
            ],
        }
    )
    definition = WorkflowDefinition.model_validate(_doc())
    out = asyncio.run(execute_condition(_run({}), node, definition))
    assert out == {"reply_decide": "vip"} and len(reads) == 1


# --- the door still speaks the same grammar -----------------------------------


def test_entry_where_and_condition_share_one_evaluator() -> None:
    conditions = [Condition(field="payload.gateway", op="is", value="COD")]
    assert matches(conditions, lambda p: {"payload.gateway": "COD"}.get(p))
    rule = ConditionRule.model_validate(
        {"on": "cod", "if": [{"field": "context.gateway", "op": "is", "value": "COD"}]}
    )
    assert predicates.choose([rule], {"gateway": "COD"}, {}, None) == "cod"
