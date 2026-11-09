"""Segments phase 3 (canon T15 col 6): a dynamic segment's rule — its format,
the laws it must pass before it is saved, and creating one.

What is proven here:
- the shape: all / any / not / event conditions nest; every malformed
  shape is refused with a problem located by path (not an object, an empty
  group, two words in one group, an unknown key, a missing event, a bad
  within_days / at_least, a bad where condition, too deep, too many events);
- the catalog laws, against the merchant's own catalog: an event the
  merchant does not send, an undeclared field, an op that does not fit the
  field's type, a choice value that does not exist, a derived field — each
  refused; a topic two sources declare is refused at the gather;
- the gather costs at most one lookup per event the shape walk reached:
  an oversized rule (more than 50 events) is refused with no lookup and no
  insert, and an event past the depth limit is never read;
- the shape lives in shared/segment_format.py (record answers the same rule
  and may not import outreach) and imports nothing outside shared/;
  check_shape hands each event condition on with its path and its where
  lines at their written index; rule_events finds every nested event; the
  docs example validates against the real Shopify code catalog;
- create_dynamic_segment checks before it stores, and the route answers 201 /
  422 with every problem / 409, while a body without a definition still
  creates a static list.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.crm.outreach.segment_rules as segment_rules
import app.crm.outreach.segments as segments
import app.crm.shared.segment_format as segment_format
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.outreach import api as outreach_api
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.db.queries.segment import insert_dynamic_segment_query
from app.crm.outreach.schemas import Segment
from app.crm.record.catalog import code_entries, with_ops
from app.crm.record.contracts import AmbiguousTopic, CatalogField

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
EXAMPLE = Path("docs/crm/segments/abandoned-checkout-not-ordered.json")


def _field(path: str, type_: str, **extra: Any) -> CatalogField:
    return with_ops(CatalogField(path=path, type=type_, label=path, **extra))  # type: ignore[arg-type]


# A Flipkart-shaped registered catalog: what a merchant's schemas declare.
FLIPKART: Dict[str, Optional[Dict[str, CatalogField]]] = {
    "OFFERED": {
        "payload.requested_amount": _field("payload.requested_amount", "number"),
        "payload.loan_state": _field(
            "payload.loan_state", "choice", values=["OFFERED", "KYC_COMPLETED"]
        ),
        "payload.loan_applications.lender_name": _field(
            "payload.loan_applications.lender_name", "list"
        ),
        "items_count": _field("items_count", "number", derived=True),
    },
    "GRANTED": {},
    "KYC_COMPLETED": {},
}


# --- the shape ---


@pytest.mark.parametrize(
    "rule",
    [
        {"event": "OFFERED"},
        {"event": "OFFERED", "within_days": 7, "at_least": 2},
        {"not": {"event": "GRANTED"}},
        {
            "all": [
                {
                    "event": "OFFERED",
                    "within_days": 7,
                    "where": [
                        {
                            "field": "payload.loan_applications.lender_name",
                            "op": "includes",
                            "value": "FINNABLE",
                        },
                        {
                            "field": "payload.requested_amount",
                            "op": ">",
                            "value": 50000,
                        },
                    ],
                },
                {"not": {"event": "GRANTED"}},
            ]
        },
        {
            "any": [
                {"event": "OFFERED", "within_days": 7},
                {
                    "all": [
                        {"event": "KYC_COMPLETED", "within_days": 3},
                        {"not": {"event": "GRANTED"}},
                    ]
                },
            ]
        },
    ],
)
def test_a_well_formed_rule_has_no_problems(rule: Dict[str, Any]) -> None:
    assert segment_rules.validate_rule(rule, FLIPKART) == []


@pytest.mark.parametrize(
    ("rule", "problem"),
    [
        ([{"event": "OFFERED"}], "rule: a rule is an object"),
        ({}, "rule: expected a group"),
        ({"all": []}, "rule.all: needs a non-empty list of rules"),
        ({"any": {"event": "OFFERED"}}, "rule.any: needs a non-empty list"),
        (
            {"all": [{"event": "OFFERED"}], "not": {"event": "GRANTED"}},
            "exactly one word",
        ),
        ({"event": "OFFERED", "within": 7}, "unknown key(s) within"),
        ({"event": ""}, "rule.event: names the event"),
        ({"event": "OFFERED", "within_days": 0}, "rule.within_days: a whole number"),
        ({"event": "OFFERED", "within_days": "7"}, "rule.within_days: a whole number"),
        ({"event": "OFFERED", "within_days": True}, "rule.within_days: a whole number"),
        ({"event": "OFFERED", "at_least": 0}, "rule.at_least: a whole number"),
        ({"event": "OFFERED", "where": {"field": "x"}}, "rule.where: a list"),
        (
            {
                "event": "OFFERED",
                "where": [{"field": "payload.loan_state", "op": "in", "value": "x"}],
            },
            "rule.where[0]: 'in' needs a non-empty list value",
        ),
        (
            {
                "event": "OFFERED",
                "where": [{"field": "payload.loan_state", "op": "like", "value": "x"}],
            },
            "rule.where[0]: op:",
        ),
    ],
)
def test_a_malformed_rule_is_refused_by_path(rule: Any, problem: str) -> None:
    problems = segment_rules.validate_rule(rule, FLIPKART)
    assert any(problem in p for p in problems), problems


def test_nesting_deeper_than_the_limit_is_refused() -> None:
    rule: Dict[str, Any] = {"event": "OFFERED"}
    for _ in range(segment_format.MAX_RULE_DEPTH):
        rule = {"not": rule}
    problems = segment_rules.validate_rule(rule, FLIPKART)
    assert any("nested deeper than" in p for p in problems), problems


def test_more_event_conditions_than_the_limit_are_refused() -> None:
    rule = {"any": [{"event": "OFFERED"}] * (segment_format.MAX_RULE_EVENTS + 1)}
    problems = segment_rules.validate_rule(rule, FLIPKART)
    assert any("event conditions — at most" in p for p in problems), problems


# --- the catalog laws ---


@pytest.mark.parametrize(
    ("condition", "problem"),
    [
        (
            {"field": "payload.lendr", "op": "is", "value": "X"},
            "'payload.lendr' is not a declared field",
        ),
        (
            {"field": "payload.requested_amount", "op": "is", "value": "5"},
            "'is' is not an op for 'payload.requested_amount'",
        ),
        (
            {"field": "payload.loan_state", "op": "is", "value": "GRANTED"},
            "has no value 'GRANTED'",
        ),
        (
            {"field": "payload.requested_amount", "op": ">", "value": "lots"},
            "is a number",
        ),
        (
            {"field": "items_count", "op": ">", "value": 2},
            "is computed in code, not stored on the event",
        ),
    ],
)
def test_a_where_condition_answers_to_the_merchants_catalog(
    condition: Dict[str, Any], problem: str
) -> None:
    rule = {"all": [{"event": "OFFERED", "where": [condition]}]}
    problems = segment_rules.validate_rule(rule, FLIPKART)
    assert any(
        p.startswith("rule.all[0].where[0]:") and problem in p for p in problems
    ), problems


def test_an_event_the_merchant_does_not_send_is_refused() -> None:
    rule = {"any": [{"event": "OFFERRED"}, {"event": "OFFERED"}]}
    problems = segment_rules.validate_rule(rule, {**FLIPKART, "OFFERRED": None})
    assert problems == [
        "rule.any[0].event: 'OFFERRED' is not an event this merchant sends — "
        "register its schema (or declare it in code) before a rule names it"
    ]


def test_without_a_catalog_only_the_shape_is_judged() -> None:
    rule = {
        "event": "ANYTHING",
        "where": [{"field": "payload.x", "op": "is", "value": "y"}],
    }
    assert segment_rules.validate_rule(rule, None) == []


def test_rule_events_finds_every_nested_event() -> None:
    rule = {
        "any": [
            {"event": "OFFERED"},
            {"all": [{"event": "KYC_COMPLETED"}, {"not": {"event": "GRANTED"}}]},
        ]
    }
    assert segment_format.rule_events(rule) == ["OFFERED", "KYC_COMPLETED", "GRANTED"]
    assert segment_format.rule_events("not a rule") == []


def test_check_shape_hands_on_each_event_with_its_path_and_where() -> None:
    problems, events = segment_format.check_shape(
        {
            "all": [
                {
                    "event": "OFFERED",
                    "within_days": 7,
                    "where": [
                        {"field": "payload.requested_amount", "op": ">", "value": 5}
                    ],
                },
                {"not": {"event": "GRANTED"}},
            ]
        }
    )
    assert problems == []
    assert [(e.path, e.topic) for e in events] == [
        ("rule.all[0]", "OFFERED"),
        ("rule.all[1].not", "GRANTED"),
    ]
    ((index, condition),) = events[0].where
    assert (index, condition.field, condition.op) == (
        0,
        "payload.requested_amount",
        ">",
    )


def test_a_catalog_problem_points_at_the_line_written_not_its_survivor_rank() -> None:
    # where[0] is malformed and dropped; where[1]'s field is undeclared. The
    # second problem must say where[1] — the line the person wrote.
    rule = {
        "event": "OFFERED",
        "where": [
            {"field": "payload.loan_state", "op": "like", "value": "x"},
            {"field": "payload.lendr", "op": "is", "value": "X"},
        ],
    }
    problems = segment_rules.validate_rule(rule, FLIPKART)
    assert any(p.startswith("rule.where[0]: op:") for p in problems), problems
    assert any(
        p.startswith("rule.where[1]:") and "'payload.lendr'" in p for p in problems
    ), problems


def test_the_format_imports_nothing_outside_shared() -> None:
    # record answers this rule in phase 4 and may import only identity +
    # shared (boundary rule 12) — the format must stay a shared leaf.
    source = Path("app/crm/shared/segment_format.py").read_text()
    internal = re.findall(r"^(?:from|import) (app\.[\w.]+)", source, re.MULTILINE)
    assert internal and all(m.startswith("app.crm.shared.") for m in internal)


async def test_the_gather_reads_each_event_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[Tuple[str, str]] = []

    async def catalog_fields(merchant_id: str, topic: str):
        asked.append((merchant_id, topic))
        return FLIPKART.get(topic)

    monkeypatch.setattr(segment_rules, "catalog_fields", catalog_fields)
    rule = {
        "any": [
            {"event": "OFFERED"},
            {"not": {"event": "OFFERED"}},
            {"event": "GRANTED"},
        ]
    }
    catalogs = await segment_rules.gather_rule_catalogs("m1", rule)
    assert asked == [("m1", "GRANTED"), ("m1", "OFFERED")]
    assert set(catalogs or {}) == {"GRANTED", "OFFERED"}


async def test_a_topic_two_sources_declare_is_refused_at_the_gather(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def catalog_fields(merchant_id: str, topic: str):
        raise AmbiguousTopic(topic, ["shopify", "lead-api"])

    monkeypatch.setattr(segment_rules, "catalog_fields", catalog_fields)
    with pytest.raises(segment_rules.SegmentRuleInvalid) as refused:
        await segment_rules.gather_rule_catalogs("m1", {"event": "orders/create"})
    assert (
        "declared by more than one source (shopify, lead-api)"
        in refused.value.problems[0]
    )


async def test_an_oversized_rule_is_refused_without_a_single_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One lookup per topic: a rule naming thousands of events must be
    # refused for its size before it costs thousands of queries.
    asked: List[str] = []

    async def catalog_fields(merchant_id: str, topic: str):
        asked.append(topic)
        return {}

    monkeypatch.setattr(segment_rules, "catalog_fields", catalog_fields)
    wide = {"any": [{"event": f"T{i}"} for i in range(5000)]}
    catalogs = await segment_rules.gather_rule_catalogs("m1", wide)
    assert catalogs is None and asked == []
    problems = segment_rules.validate_rule(wide, catalogs)
    assert problems == ["rule: 5000 event conditions — at most 50"]


async def test_the_gather_reads_only_what_the_shape_walk_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[str] = []

    async def catalog_fields(merchant_id: str, topic: str):
        asked.append(topic)
        return {}

    monkeypatch.setattr(segment_rules, "catalog_fields", catalog_fields)
    deep: Dict[str, Any] = {"event": "BURIED"}
    for _ in range(900):
        deep = {"not": deep}
    rule = {"all": [{"event": "OFFERED"}, deep]}
    await segment_rules.gather_rule_catalogs("m1", rule)
    assert asked == ["OFFERED"]  # the event past the depth limit is never read


async def test_create_refuses_an_oversized_rule_before_any_lookup_or_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[str] = []

    async def catalog_fields(merchant_id: str, topic: str):
        asked.append(topic)
        return {}

    async def never(*args: Any) -> Any:
        raise AssertionError("an oversized rule reached the insert")

    monkeypatch.setattr(segment_rules, "catalog_fields", catalog_fields)
    monkeypatch.setattr(segments.segment_accessor, "insert_dynamic_segment", never)
    wide = {"any": [{"event": f"T{i}"} for i in range(5000)]}
    with pytest.raises(segments.SegmentRuleInvalid) as refused:
        await segments.create_dynamic_segment("m1", "Too big", wide, "ops@x")
    assert refused.value.problems == ["rule: 5000 event conditions — at most 50"]
    assert asked == []


def _code_catalog(topic: str) -> Optional[Dict[str, CatalogField]]:
    fields = {f.path: f for e in code_entries() if e.topic == topic for f in e.fields}
    return fields or None


def test_the_docs_example_validates_against_the_shopify_code_catalog() -> None:
    rule = json.loads(EXAMPLE.read_text())
    catalogs = {
        topic: _code_catalog(topic) for topic in segment_format.rule_events(rule)
    }
    assert all(catalogs.values()), "the example names an event the code catalog lacks"
    assert segment_rules.validate_rule(rule, catalogs) == []


# --- creating a dynamic segment ---


def test_the_insert_stores_the_rule_and_leaves_the_count_unstamped() -> None:
    query, params = insert_dynamic_segment_query(
        "m1", "Offered", {"event": "OFFERED"}, "ops@x"
    )
    assert "VALUES ($1, $2, $3::jsonb, $4)" in query
    assert "member_count" not in query.split("RETURNING")[0]
    assert params == ["m1", "Offered", '{"event": "OFFERED"}', "ops@x"]


def _dynamic(name: str, definition: Dict[str, Any]) -> Segment:
    return Segment(
        id=uuid4(),
        merchant_id="m1",
        name=name,
        kind="dynamic",
        definition=definition,
        member_count=None,
        computed_at=None,
        created_by="ops@x",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeSegmentAccessor:
    def __init__(self, taken: Tuple[str, ...] = ()) -> None:
        self.taken = set(taken)
        self.inserts: List[Dict[str, Any]] = []

    async def insert_dynamic_segment(self, merchant_id, name, definition, created_by):
        self.inserts.append({"name": name, "definition": definition})
        if name in self.taken:
            raise UniqueViolation("crm_segment_merchant_name_uq")
        return _dynamic(name, definition)

    async def insert_static_list(self, merchant_id, name, created_by):
        segment = _dynamic(name, {"event": "x"})
        return segment.model_copy(update={"kind": "static", "definition": None})


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    def _install(taken: Tuple[str, ...] = ()):
        fake = FakeSegmentAccessor(taken)

        async def catalog_fields(merchant_id: str, topic: str):
            return FLIPKART.get(topic)

        monkeypatch.setattr(segments, "segment_accessor", fake)
        monkeypatch.setattr(segment_rules, "catalog_fields", catalog_fields)
        return fake

    return _install


async def test_a_valid_rule_is_stored_as_written(world) -> None:
    fake = world()
    rule = {
        "all": [{"event": "OFFERED", "within_days": 7}, {"not": {"event": "GRANTED"}}]
    }
    segment = await segments.create_dynamic_segment(
        "m1", " Offered, not granted ", rule, "ops@x"
    )
    assert fake.inserts == [{"name": "Offered, not granted", "definition": rule}]
    assert segment.kind == "dynamic" and segment.member_count is None


async def test_a_broken_rule_is_never_stored(world) -> None:
    fake = world()
    with pytest.raises(segment_rules.SegmentRuleInvalid) as refused:
        await segments.create_dynamic_segment(
            "m1", "Bad", {"all": [{"event": "OFFERRED"}, {"any": []}]}, "ops@x"
        )
    assert len(refused.value.problems) == 2
    assert fake.inserts == []


async def test_a_taken_name_is_refused_for_a_dynamic_segment_too(world) -> None:
    world(taken=("Offered",))
    with pytest.raises(segments.SegmentNameTaken):
        await segments.create_dynamic_segment(
            "m1", "Offered", {"event": "OFFERED"}, None
        )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(outreach_api.segment_router, prefix="/segments")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user", username="u", merchant_ids=["m1"], reseller_ids=[], email="ops@x"
    )
    return TestClient(app)


def test_the_route_creates_a_dynamic_segment(world) -> None:
    world()
    response = _client().post(
        "/segments",
        json={
            "merchant_id": "m1",
            "name": "Offered",
            "definition": {"event": "OFFERED"},
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "dynamic" and body["definition"] == {"event": "OFFERED"}
    assert body["member_count"] is None and body["computed_at"] is None


def test_the_route_answers_422_with_every_problem(world) -> None:
    world()
    response = _client().post(
        "/segments",
        json={
            "merchant_id": "m1",
            "name": "Bad",
            "definition": {
                "all": [
                    {"event": "OFFERRED"},
                    {
                        "event": "OFFERED",
                        "where": [{"field": "payload.lendr", "op": "is", "value": "X"}],
                    },
                ]
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == [
        "rule.all[0].event: 'OFFERRED' is not an event this merchant sends — "
        "register its schema (or declare it in code) before a rule names it",
        "rule.all[1].where[0]: 'payload.lendr' is not a declared field",
    ]


def test_the_route_answers_409_on_a_taken_name(world) -> None:
    world(taken=("Offered",))
    response = _client().post(
        "/segments",
        json={
            "merchant_id": "m1",
            "name": "Offered",
            "definition": {"event": "OFFERED"},
        },
    )
    assert response.status_code == 409


def test_a_body_without_a_definition_still_creates_a_static_list(world) -> None:
    fake = world()
    response = _client().post("/segments", json={"merchant_id": "m1", "name": "VIPs"})
    assert response.status_code == 201 and response.json()["kind"] == "static"
    assert fake.inserts == []
