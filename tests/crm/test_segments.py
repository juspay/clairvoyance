"""Segments phase 1 (canon T15, canon/05-audiences.md): the segment row,
created and read.

What is proven here:
- the table has its owner, and 085 carries the canon's self-defense: the
  name unique per merchant with merchant_id first, a blank name refused, a
  definition only ever a JSON object, and the tenant pin a broadcast will FK;
- every builder puts merchant_id at $1 and parameterizes every value; a new
  list is born with an exact count (0, stamped now) and no definition;
- the name is trimmed before it reaches the unique index, a blank one never
  reaches the database, and the index's refusal becomes SegmentNameTaken;
- the kind is derived from definition on read;
- every /segments route declares the tenancy door, and the routes answer
  201 / 409 / 422 / 404 / 403 as documented.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.crm.outreach.segments as segments
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm import api as crm_root_api
from app.crm.auth import MERCHANT_SCOPE_MARK
from app.crm.outreach import api as outreach_api
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.db.decoders.segment import decode_segment
from app.crm.outreach.db.queries.segment import (
    count_segments_query,
    get_segment_query,
    insert_static_list_query,
    list_segments_query,
)
from app.crm.outreach.schemas import Segment, SegmentPage
from scripts.check_crm_boundaries import TABLE_OWNERS

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
MIGRATION = Path("app/database/migrations/085_create_crm_segment.sql")


def _segment(name: str = "Diwali VIPs", **overrides: Any) -> Segment:
    row: Dict[str, Any] = {
        "id": uuid4(),
        "merchant_id": "m1",
        "name": name,
        "kind": "static",
        "definition": None,
        "member_count": 0,
        "computed_at": NOW,
        "created_by": "ops@x",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return Segment(**row)


class FakeSegmentAccessor:
    """Records every insert by field name; answers reads from a dict."""

    def __init__(self, existing: Optional[List[Segment]] = None) -> None:
        self.rows: List[Segment] = list(existing or [])
        self.inserts: List[Dict[str, Any]] = []

    async def insert_static_list(
        self, merchant_id: str, name: str, created_by: Optional[str]
    ) -> Segment:
        self.inserts.append(
            {"merchant_id": merchant_id, "name": name, "created_by": created_by}
        )
        if any(r.merchant_id == merchant_id and r.name == name for r in self.rows):
            raise UniqueViolation("crm_segment_merchant_name_uq")
        segment = _segment(name, merchant_id=merchant_id, created_by=created_by)
        self.rows.append(segment)
        return segment

    async def get_segment(self, merchant_id: str, segment_id: str) -> Optional[Segment]:
        return next(
            (
                r
                for r in self.rows
                if r.merchant_id == merchant_id and str(r.id) == segment_id
            ),
            None,
        )

    async def list_segments(
        self, merchant_id: str, limit: int, offset: int
    ) -> List[Segment]:
        mine = [r for r in self.rows if r.merchant_id == merchant_id]
        return mine[offset : offset + limit]

    async def count_segments(self, merchant_id: str) -> int:
        return sum(1 for r in self.rows if r.merchant_id == merchant_id)


@pytest.fixture
def accessor(monkeypatch: pytest.MonkeyPatch) -> FakeSegmentAccessor:
    fake = FakeSegmentAccessor()
    monkeypatch.setattr(segments, "segment_accessor", fake)
    return fake


# --- the table ---


def test_the_segment_table_has_an_owner() -> None:
    assert TABLE_OWNERS["crm_segment"] == "outreach"


def test_085_keeps_the_canon_self_defense() -> None:
    sql = MIGRATION.read_text()
    # T15 col 3: unique per merchant, merchant_id first (tenancy law).
    assert "ON crm_segment (merchant_id, name)" in sql
    assert "CREATE UNIQUE INDEX crm_segment_merchant_name_uq" in sql
    # Blank or untrimmed names are refused by the table itself, so the plain
    # unique index is whitespace-insensitive for every writer.
    assert "CHECK (name <> '' AND name !~ '^\\s|\\s$')" in sql
    # A definition is always an object when set; NULL means a static list.
    assert "jsonb_typeof(definition) = 'object'" in sql
    assert (
        "definition    jsonb CHECK" in sql and "definition    jsonb NOT NULL" not in sql
    )
    # The pin target crm_broadcast (T17) will reference.
    assert "ON crm_segment (merchant_id, id)" in sql
    # The kind is derived, never stored (law 10).
    assert "kind" not in sql.split("CREATE TABLE crm_segment (")[1].split(");")[0]


# --- the builders ---


@pytest.mark.parametrize(
    ("built", "merchant_first"),
    [
        (insert_static_list_query("m1", "Diwali VIPs", "ops@x"), True),
        (get_segment_query("m1", "seg-1"), True),
        (list_segments_query("m1", 50, 0), True),
        (count_segments_query("m1"), True),
    ],
)
def test_every_builder_puts_the_merchant_first_and_parameterizes(
    built: Tuple[str, List[Any]], merchant_first: bool
) -> None:
    query, params = built
    assert params[0] == "m1"
    assert "merchant_id = $1" in query or "VALUES ($1" in query
    for value in params:
        if isinstance(value, str):
            assert value not in query, f"{value!r} reached the SQL text"


def test_a_new_list_is_born_with_an_exact_count_and_no_definition() -> None:
    query, params = insert_static_list_query("m1", "Diwali VIPs", "ops@x")
    assert "VALUES ($1, $2, 0, now(), $3)" in query
    assert "definition" not in query.split("RETURNING")[0]
    assert params == ["m1", "Diwali VIPs", "ops@x"]


def test_the_list_read_is_newest_first_and_paged() -> None:
    query, params = list_segments_query("m1", 25, 50)
    assert "ORDER BY created_at DESC, id DESC" in query
    assert "LIMIT $2 OFFSET $3" in query
    assert params == ["m1", 25, 50]


# --- the name ---


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("Diwali VIPs", "Diwali VIPs"),
        ("  Diwali VIPs ", "Diwali VIPs"),
        ("Diwali  VIPs", "Diwali  VIPs"),  # inner spacing is the author's
    ],
)
def test_the_name_is_trimmed_before_it_reaches_the_index(raw: str, stored: str) -> None:
    assert segments.segment_name(raw) == stored


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_a_blank_name_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        segments.segment_name(raw)


# --- create and read ---


async def test_create_stores_the_trimmed_name_and_the_author(
    accessor: FakeSegmentAccessor,
) -> None:
    segment = await segments.create_static_list("m1", " Diwali VIPs ", "ops@x")
    assert accessor.inserts == [
        {"merchant_id": "m1", "name": "Diwali VIPs", "created_by": "ops@x"}
    ]
    assert segment.kind == "static" and segment.member_count == 0


async def test_a_blank_name_never_reaches_the_database(
    accessor: FakeSegmentAccessor,
) -> None:
    with pytest.raises(ValueError):
        await segments.create_static_list("m1", "   ", "ops@x")
    assert accessor.inserts == []


async def test_a_second_list_with_the_same_name_is_refused(
    accessor: FakeSegmentAccessor,
) -> None:
    await segments.create_static_list("m1", "Diwali VIPs", "ops@x")
    with pytest.raises(segments.SegmentNameTaken):
        await segments.create_static_list("m1", "Diwali VIPs ", "ops@y")


async def test_two_merchants_may_use_the_same_name(
    accessor: FakeSegmentAccessor,
) -> None:
    await segments.create_static_list("m1", "Diwali VIPs", "ops@x")
    other = await segments.create_static_list("m2", "Diwali VIPs", "ops@y")
    assert other.merchant_id == "m2"


async def test_another_merchants_segment_reads_as_none(
    accessor: FakeSegmentAccessor,
) -> None:
    mine = await segments.create_static_list("m1", "Diwali VIPs", "ops@x")
    assert await segments.get_segment("m2", str(mine.id)) is None
    assert await segments.get_segment("m1", str(mine.id)) == mine


async def test_the_list_carries_its_total_in_the_body(
    accessor: FakeSegmentAccessor,
) -> None:
    for name in ("A", "B", "C"):
        await segments.create_static_list("m1", name, None)
    page = await segments.list_segments("m1", limit=2, offset=0)
    assert isinstance(page, SegmentPage)
    assert len(page.items) == 2 and page.total == 3


# --- the decoder ---


def _row(definition: Any) -> Dict[str, Any]:
    return {
        "id": uuid4(),
        "merchant_id": "m1",
        "name": "s",
        "definition": definition,
        "member_count": None,
        "computed_at": None,
        "created_by": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


@pytest.mark.parametrize(
    ("definition", "kind"),
    [
        (None, "static"),
        ({"all": []}, "dynamic"),
        ('{"all": []}', "dynamic"),  # jsonb arriving as text decodes too
    ],
)
def test_the_kind_is_derived_from_the_definition(definition: Any, kind: str) -> None:
    assert decode_segment(_row(definition)).kind == kind


# --- the routes ---


def _client(merchants: Tuple[str, ...] = ("m1",)) -> TestClient:
    app = FastAPI()
    app.include_router(outreach_api.segment_router, prefix="/segments")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user",
        username="u",
        merchant_ids=list(merchants),
        reseller_ids=[],
        email="ops@x",
    )
    return TestClient(app)


def test_every_segment_route_declares_the_tenancy_door() -> None:
    routes = [r for r in outreach_api.segment_router.routes if isinstance(r, APIRoute)]
    assert len(routes) == 3, "the router lost or gained routes — update this walk"
    missing = [
        r.path
        for r in routes
        if not any(
            getattr(d.call, MERCHANT_SCOPE_MARK, False)
            for d in r.dependant.dependencies
        )
    ]
    assert missing == []


def test_the_root_router_mounts_segments() -> None:
    paths = {getattr(r, "path", "") for r in crm_root_api.router.routes}
    assert {"/segments", "/segments/{segment_id}"} <= paths


def test_create_answers_201_with_the_new_list(accessor: FakeSegmentAccessor) -> None:
    response = _client().post(
        "/segments", json={"merchant_id": "m1", "name": " Diwali VIPs "}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Diwali VIPs" and body["kind"] == "static"
    assert body["member_count"] == 0 and body["created_by"] == "ops@x"


def test_create_answers_409_on_a_taken_name(accessor: FakeSegmentAccessor) -> None:
    client = _client()
    client.post("/segments", json={"merchant_id": "m1", "name": "Diwali VIPs"})
    again = client.post("/segments", json={"merchant_id": "m1", "name": "Diwali VIPs"})
    assert again.status_code == 409


def test_create_answers_422_on_a_name_blank_once_trimmed(
    accessor: FakeSegmentAccessor,
) -> None:
    response = _client().post("/segments", json={"merchant_id": "m1", "name": "   "})
    assert response.status_code == 422
    assert accessor.inserts == []


def test_create_refuses_a_merchant_the_caller_cannot_reach(
    accessor: FakeSegmentAccessor,
) -> None:
    response = _client(merchants=("m1",)).post(
        "/segments", json={"merchant_id": "m2", "name": "Diwali VIPs"}
    )
    assert response.status_code == 403
    assert accessor.inserts == []


def test_get_answers_404_for_an_unknown_or_foreign_segment(
    accessor: FakeSegmentAccessor,
) -> None:
    client = _client(merchants=("m1", "m2"))
    created = client.post(
        "/segments", json={"merchant_id": "m2", "name": "Diwali VIPs"}
    ).json()
    assert client.get(f"/segments/{uuid4()}?merchant_id=m1").status_code == 404
    assert client.get(f"/segments/{created['id']}?merchant_id=m1").status_code == 404
    mine = client.get(f"/segments/{created['id']}?merchant_id=m2")
    assert mine.status_code == 200 and UUID(mine.json()["id"])


def test_get_answers_422_for_an_id_that_is_not_a_uuid(
    accessor: FakeSegmentAccessor,
) -> None:
    assert _client().get("/segments/not-a-uuid?merchant_id=m1").status_code == 422


def test_the_list_route_pages_and_totals(accessor: FakeSegmentAccessor) -> None:
    client = _client()
    for name in ("A", "B", "C"):
        client.post("/segments", json={"merchant_id": "m1", "name": name})
    page = client.get("/segments?merchant_id=m1&limit=2").json()
    assert len(page["items"]) == 2 and page["total"] == 3
