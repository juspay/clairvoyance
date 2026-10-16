"""Segments phase 4: record answers a dynamic segment's rule — the compiler
(record/db/queries_segments.py) and its logic (record/segments.py) — and
outreach asks it (outreach/segments.py, the member and count routes).

What is proven here (no database — tests/crm/test_segment_parity.py runs the
same SQL against Postgres beside the Python evaluator):
- holds_without_events decides the universe: a rule that needs an event
  reads only its own topics; one that holds with none ("never ordered")
  reads every event of the merchant;
- answer_problems re-checks what the SQL needs (shape, stored payload paths,
  numbers for = and the ordering ops) and the contract refuses before any
  read;
- the SQL: merchant first, every value a parameter (a hostile value never
  reaches the text), every parameter referenced (Postgres refuses an
  unreferenced one), one count per event condition, all / any / not as
  AND / OR / NOT, the topic narrowing, the keyset page, the count wrapper;
- the reads go to the replica and decode ids and the count;
- the save-time law: a segment does not compare date-times yet;
- outreach asks record with the stored rule: a dynamic segment's members
  page through record (no added_at — nobody added them), its count is
  stamped with the moment it was asked and only moves forward, a static
  list's exact count is returned untouched, and a rule record refuses is a
  422 with every reason.
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

import app.crm.outreach.segments as outreach_segments
import app.crm.record.contracts as record_contracts
import app.crm.record.db.accessor as accessor
import app.crm.record.segments as segments
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.outreach import api as outreach_api
from app.crm.outreach.db.queries.segment import stamp_dynamic_count_query
from app.crm.outreach.schemas import Segment
from app.crm.outreach.segment_rules import validate_rule
from app.crm.record.catalog import with_ops
from app.crm.record.contracts import CatalogField
from app.crm.record.db.queries_segments import (
    segment_count_query,
    segment_members_query,
)

OFFERED_NOT_GRANTED = {
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
                {"field": "payload.requested_amount", "op": ">", "value": 50000},
            ],
        },
        {"not": {"event": "GRANTED"}},
    ]
}

# One condition per op the closed set holds.
EVERY_OP = [
    {"field": "payload.s", "op": "exists"},
    {"field": "payload.s", "op": "not_exists"},
    {"field": "payload.s", "op": "is", "value": "7"},
    {"field": "payload.s", "op": "is_not", "value": "7"},
    {"field": "payload.s", "op": "in", "value": ["7", 7]},
    {"field": "payload.tags", "op": "includes", "value": "vip"},
    {"field": "payload.n", "op": "=", "value": "1850.00"},
    {"field": "payload.n", "op": ">", "value": 5},
    {"field": "payload.n", "op": ">=", "value": 5},
    {"field": "payload.n", "op": "<", "value": 5},
    {"field": "payload.n", "op": "<=", "value": 5.5},
]


def _placeholders(sql: str) -> set:
    return {int(n) for n in re.findall(r"\$(\d+)", sql)}


# --- the universe ---


@pytest.mark.parametrize(
    ("rule", "holds"),
    [
        ({"event": "OFFERED"}, False),
        ({"not": {"event": "GRANTED"}}, True),
        (OFFERED_NOT_GRANTED, False),
        ({"any": [{"event": "OFFERED"}, {"not": {"event": "GRANTED"}}]}, True),
        ({"not": {"all": [{"not": {"event": "A"}}, {"not": {"event": "B"}}]}}, False),
        ({"not": {"not": {"event": "A"}}}, False),
    ],
)
def test_holds_without_events_decides_the_universe(
    rule: Dict[str, Any], holds: bool
) -> None:
    assert segments.holds_without_events(rule) is holds
    topics = segments._topics(rule)
    if holds:
        assert topics is None  # every event of the merchant is read
    else:
        assert topics == sorted(set(segments.rule_events(rule)))


# --- what the SQL needs ---


@pytest.mark.parametrize(
    ("rule", "problem"),
    [
        ({"all": []}, "rule.all: needs a non-empty list of rules"),
        (
            {
                "event": "orders/create",
                "where": [{"field": "items_count", "op": ">", "value": 2}],
            },
            "rule.where[0]: 'items_count' is not a stored payload field",
        ),
        (
            {
                "event": "OFFERED",
                "where": [{"field": "payload.at", "op": ">", "value": "2026-10-13"}],
            },
            "rule.where[0]: '>' compares numbers in a segment",
        ),
        (
            {
                "event": "OFFERED",
                "where": [{"field": "payload.n", "op": "=", "value": "lots"}],
            },
            "rule.where[0]: '=' compares numbers in a segment",
        ),
    ],
)
def test_answer_problems_refuses_what_sql_cannot_answer(
    rule: Dict[str, Any], problem: str
) -> None:
    problems = segments.answer_problems(rule)
    assert any(p.startswith(problem) for p in problems), problems


def test_a_numeric_text_value_and_every_op_are_answerable() -> None:
    rule = {"event": "T", "where": EVERY_OP}
    assert segments.answer_problems(rule) == []
    assert segments.answer_problems(OFFERED_NOT_GRANTED) == []


async def test_the_contract_refuses_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*args: Any) -> Any:
        raise AssertionError("an unanswerable rule reached the database")

    monkeypatch.setattr(accessor, "customers_matching", never)
    monkeypatch.setattr(accessor, "count_matching", never)
    bad = {"event": "T", "where": [{"field": "payload.at", "op": "<", "value": "x"}]}
    with pytest.raises(segments.SegmentRuleUnanswerable):
        await segments.customers_matching("m1", bad)
    with pytest.raises(segments.SegmentRuleUnanswerable):
        await segments.count_matching("m1", bad)
    for limit in (0, segments.MAX_MATCHING_PAGE + 1):
        with pytest.raises(ValueError):
            await segments.customers_matching("m1", {"event": "T"}, limit=limit)


# --- the SQL ---


def test_the_flipkart_rule_compiles_to_one_grouped_query() -> None:
    sql, params = segment_members_query(
        "flipkart", OFFERED_NOT_GRANTED, ["GRANTED", "OFFERED"], None, 1000
    )
    assert params[0] == "flipkart"
    assert "WHERE merchant_id = $1" in sql
    assert "AND customer_id IS NOT NULL" in sql
    assert "AND topic = ANY($2::text[])" in sql and params[1] == ["GRANTED", "OFFERED"]
    assert "GROUP BY customer_id" in sql
    assert sql.count("count(*) FILTER") == 2  # one per event condition
    assert "(NOT (count(*) FILTER (WHERE topic = $" in sql
    assert ") AND (NOT" in sql  # all -> AND
    assert "make_interval(days => $4::int)" in sql and params[3] == 7
    assert 'lax $."loan_applications"."lender_name"[*]' in params
    assert sql.rstrip().endswith(f"LIMIT ${len(params)}") and params[-1] == 1000
    assert _placeholders(sql) == set(range(1, len(params) + 1))


def test_any_is_or_and_at_least_defaults_to_one() -> None:
    rule = {"any": [{"event": "A", "at_least": 3}, {"event": "B"}]}
    sql, params = segment_members_query("m1", rule, ["A", "B"], None, 10)
    assert ") OR (" in sql
    at_least = re.findall(r">= \$(\d+)::int", sql)
    assert [params[int(n) - 1] for n in at_least] == [3, 1]


@pytest.mark.parametrize("condition", EVERY_OP, ids=lambda c: c["op"])
def test_every_op_references_every_parameter_and_writes_no_value(
    condition: Dict[str, Any],
) -> None:
    hostile = "x'); DROP TABLE crm_event_raw; --"
    if "value" in condition and isinstance(condition["value"], str):
        if condition["op"] not in ("=",):
            condition = {**condition, "value": hostile}
    rule = {"event": hostile, "where": [condition]}
    for sql, params in (
        segment_members_query("m1", rule, [hostile], "c-0", 5),
        segment_count_query("m1", rule, None),
    ):
        assert hostile not in sql
        assert _placeholders(sql) == set(range(1, len(params) + 1)), sql


def test_without_narrowing_every_event_is_read() -> None:
    rule = {"not": {"event": "orders/create"}}
    sql, _ = segment_members_query("m1", rule, None, None, 10)
    assert "topic = ANY" not in sql


def test_the_page_is_keyset_on_the_group_key() -> None:
    sql, params = segment_members_query("m1", {"event": "A"}, ["A"], "c-9", 200)
    assert "AND customer_id > $2::uuid" in sql and params[1] == "c-9"
    assert "ORDER BY customer_id" in sql
    first, _ = segment_members_query("m1", {"event": "A"}, ["A"], None, 200)
    assert "customer_id >" not in first


def test_the_count_wraps_the_same_grouped_query() -> None:
    sql, params = segment_count_query("m1", OFFERED_NOT_GRANTED, ["GRANTED", "OFFERED"])
    assert sql.strip().startswith("SELECT count(*) AS n FROM (")
    assert "GROUP BY customer_id" in sql and "LIMIT" not in sql
    assert _placeholders(sql) == set(range(1, len(params) + 1))


# --- the reads ---


async def test_the_reads_go_to_the_replica_and_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[str] = []

    async def replica(query: str, values: List[Any]) -> List[Dict[str, Any]]:
        seen.append(query)
        if "count(*) AS n" in query:
            return [{"n": 2}]
        return [{"customer_id": "c-1"}, {"customer_id": "c-2"}]

    monkeypatch.setattr(accessor, "crm_replica_read", replica)
    assert await record_contracts.customers_matching("m1", {"event": "A"}) == [
        "c-1",
        "c-2",
    ]
    assert await record_contracts.count_matching("m1", {"event": "A"}) == 2
    assert len(seen) == 2


# --- the save-time law ---


def _field(path: str, type_: str) -> CatalogField:
    return with_ops(CatalogField(path=path, type=type_, label=path))  # type: ignore[arg-type]


def test_a_segment_does_not_compare_date_times_yet() -> None:
    catalogs: Dict[str, Optional[Dict[str, CatalogField]]] = {
        "OFFERED": {"payload.expires_at": _field("payload.expires_at", "datetime")}
    }
    compared = {
        "event": "OFFERED",
        "where": [{"field": "payload.expires_at", "op": "<", "value": "2026-10-20"}],
    }
    problems = validate_rule(compared, catalogs)
    assert problems == [
        "rule.where[0]: a segment does not compare date-times yet — ask whether "
        "'payload.expires_at' exists, or how recent the event is (within_days)"
    ]
    present = {
        "event": "OFFERED",
        "where": [{"field": "payload.expires_at", "op": "exists"}],
    }
    assert validate_rule(present, catalogs) == []


def test_the_docs_example_is_answerable() -> None:
    rule = json.loads(
        Path("docs/crm/segments/abandoned-checkout-not-ordered.json").read_text()
    )
    assert segments.answer_problems(rule) == []
    assert segments._topics(rule) == ["checkouts/create", "orders/create"]


# --- outreach asks record ---

ASKED_BEFORE = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


def _segment(kind: str = "dynamic", **extra: Any) -> Segment:
    return Segment(
        id=uuid4(),
        merchant_id="m1",
        name="Offered, not granted",
        kind=kind,  # type: ignore[arg-type]
        definition=OFFERED_NOT_GRANTED if kind == "dynamic" else None,
        member_count=extra.get("member_count"),
        computed_at=extra.get("computed_at"),
        created_by="ops@x",
        created_at=ASKED_BEFORE,
        updated_at=ASKED_BEFORE,
    )


class FakeSegmentRows:
    """outreach's segment accessor: one row, and the count stamps it took."""

    def __init__(self, segment: Optional[Segment], newer_stands: bool = False):
        self.segment = segment
        self.newer_stands = newer_stands
        self.stamps: List[Tuple[str, str, int, datetime]] = []

    async def get_segment(self, merchant_id: str, segment_id: str) -> Optional[Segment]:
        return self.segment

    async def stamp_dynamic_count(
        self, merchant_id: str, segment_id: str, n: int, at: datetime
    ) -> Optional[Segment]:
        self.stamps.append((merchant_id, segment_id, n, at))
        if self.newer_stands or self.segment is None:
            return None
        self.segment = self.segment.model_copy(
            update={"member_count": n, "computed_at": at}
        )
        return self.segment


class FakeRecord:
    """record's two contracts, as outreach sees them."""

    def __init__(self, ids: List[str], n: int = 0, refuses: bool = False):
        self.ids, self.n, self.refuses = ids, n, refuses
        self.asked: List[Tuple[Any, ...]] = []

    async def customers_matching(
        self, merchant_id: str, rule: Any, after: Optional[str], limit: int
    ) -> List[str]:
        self.asked.append(("members", merchant_id, rule, after, limit))
        if self.refuses:
            raise record_contracts.SegmentRuleUnanswerable(["rule.where[0]: no"])
        return self.ids[:limit]

    async def count_matching(self, merchant_id: str, rule: Any) -> int:
        self.asked.append(("count", merchant_id, rule))
        if self.refuses:
            raise record_contracts.SegmentRuleUnanswerable(["rule.where[0]: no"])
        return self.n


@pytest.fixture
def asks(monkeypatch: pytest.MonkeyPatch):
    def _install(segment: Optional[Segment], **record: Any) -> SimpleNamespace:
        rows = FakeSegmentRows(segment, record.pop("newer_stands", False))
        fake = FakeRecord(**record)
        monkeypatch.setattr(outreach_segments, "segment_accessor", rows)
        monkeypatch.setattr(
            outreach_segments, "customers_matching", fake.customers_matching
        )
        monkeypatch.setattr(outreach_segments, "count_matching", fake.count_matching)
        return SimpleNamespace(rows=rows, record=fake)

    return _install


async def test_a_dynamic_segments_members_page_through_record(asks) -> None:
    ids = [str(uuid4()) for _ in range(3)]
    w = asks(_segment(), ids=ids)
    page = await outreach_segments.segment_members("m1", "seg-1", "c-0", 3)
    assert w.record.asked == [("members", "m1", OFFERED_NOT_GRANTED, "c-0", 3)]
    assert [str(m.customer_id) for m in page.items] == ids
    assert all(m.added_at is None and m.source is None for m in page.items)
    assert str(page.next_after) == ids[-1]
    short = await outreach_segments.segment_members("m1", "seg-1", None, 5)
    assert short.next_after is None


async def test_a_dynamic_count_is_stamped_with_the_moment_it_was_asked(asks) -> None:
    w = asks(_segment(), ids=[], n=18240)
    before = datetime.now(timezone.utc)
    counted = await outreach_segments.count_segment("m1", "seg-1")
    after = datetime.now(timezone.utc)
    assert w.record.asked == [("count", "m1", OFFERED_NOT_GRANTED)]
    ((merchant, segment_id, n, at),) = w.rows.stamps
    assert (merchant, segment_id, n) == ("m1", "seg-1", 18240)
    assert before <= at <= after
    assert counted.member_count == 18240 and counted.computed_at == at


async def test_a_newer_count_already_standing_is_returned_not_overwritten(asks) -> None:
    standing = _segment(member_count=900, computed_at=datetime.now(timezone.utc))
    w = asks(standing, ids=[], n=5, newer_stands=True)
    counted = await outreach_segments.count_segment("m1", "seg-1")
    assert len(w.rows.stamps) == 1  # it tried; the stamp refused to go back
    assert counted.member_count == 900


async def test_a_static_lists_exact_count_is_returned_untouched(asks) -> None:
    w = asks(_segment("static", member_count=42, computed_at=ASKED_BEFORE), ids=[])
    counted = await outreach_segments.count_segment("m1", "seg-1")
    assert counted.member_count == 42 and w.record.asked == [] and w.rows.stamps == []


async def test_a_rule_record_refuses_is_invalid_with_its_reasons(asks) -> None:
    asks(_segment(), ids=[], refuses=True)
    with pytest.raises(outreach_segments.SegmentRuleInvalid) as refused:
        await outreach_segments.segment_members("m1", "seg-1", None, 10)
    assert refused.value.problems == ["rule.where[0]: no"]
    with pytest.raises(outreach_segments.SegmentRuleInvalid):
        await outreach_segments.count_segment("m1", "seg-1")


async def test_a_missing_segment_is_not_found_and_asks_nobody(asks) -> None:
    w = asks(None, ids=[])
    with pytest.raises(outreach_segments.SegmentNotFound):
        await outreach_segments.count_segment("m1", "seg-x")
    assert w.record.asked == []


def test_the_count_stamp_is_merchant_first_dynamic_only_and_monotonic() -> None:
    sql, params = stamp_dynamic_count_query("m1", "seg-1", 7, ASKED_BEFORE)
    assert "WHERE merchant_id = $1 AND id = $2" in sql
    assert "AND definition IS NOT NULL" in sql
    assert "AND (computed_at IS NULL OR computed_at < $4)" in sql
    assert params == ["m1", "seg-1", 7, ASKED_BEFORE]


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(outreach_api.segment_router, prefix="/segments")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user", username="u", merchant_ids=["m1"], reseller_ids=[], email="ops@x"
    )
    return TestClient(app)


def test_the_routes_answer_a_dynamic_segment(asks) -> None:
    ids = [str(uuid4()) for _ in range(2)]
    asks(_segment(), ids=ids, n=2)
    members = _client().get(f"/segments/{uuid4()}/members?merchant_id=m1&limit=2")
    assert members.status_code == 200
    assert [m["customer_id"] for m in members.json()["items"]] == ids
    assert members.json()["items"][0]["added_at"] is None
    counted = _client().post(f"/segments/{uuid4()}/count?merchant_id=m1")
    assert counted.status_code == 200 and counted.json()["member_count"] == 2


@pytest.mark.parametrize(
    ("segment", "record", "code"),
    [(None, {}, 404), (_segment(), {"refuses": True}, 422)],
)
def test_the_routes_map_a_missing_segment_and_a_refused_rule(
    asks, segment: Optional[Segment], record: Dict[str, Any], code: int
) -> None:
    asks(segment, ids=[], **record)
    client = _client()
    assert client.get(f"/segments/{uuid4()}/members?merchant_id=m1").status_code == code
    response = client.post(f"/segments/{uuid4()}/count?merchant_id=m1")
    assert response.status_code == code
    if code == 422:
        assert response.json()["detail"] == ["rule.where[0]: no"]
