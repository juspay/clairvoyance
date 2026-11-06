"""Segments phase 2 (canon T21, canon/05-audiences.md:32-51): a static list's
members.

What is proven here:
- the table has its owner, and 087 keeps the canon's shape (composite PK, no
  merchant_id) plus the two invariants its trigger enforces (static lists
  only; the customer belongs to the segment's merchant);
- the builders: one INSERT for every customer with ON CONFLICT DO NOTHING
  and RETURNING (so "added" is counted by the database), the parent locked
  FOR UPDATE under its merchant, the count recounted from the rows, the
  member page entered through the parent and keyset-paged;
- the plan: duplicates in one upload are counted, never added twice;
- add_members: every person goes through resolve() with imported evidence;
  every person sent is accounted for exactly once (added / already a member
  / duplicate / skipped with its line and reason); a wrong or dynamic
  segment is refused before anyone is resolved; the atom re-checks under the
  lock; sending the same people twice adds nobody;
- the member page's cursor; the routes' answers.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.crm.outreach.segments as segments
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.outreach import api as outreach_api
from app.crm.outreach.db.queries.segment import (
    lock_segment_query,
    stamp_member_count_query,
)
from app.crm.outreach.db.queries.segment_member import (
    insert_members_query,
    member_page_query,
)
from app.crm.outreach.schemas import Segment, SegmentMember
from scripts.check_crm_boundaries import TABLE_OWNERS

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
MIGRATION = Path("app/database/migrations/087_create_crm_segment_member.sql")
NAMESPACE = UUID("00000000-0000-0000-0000-00000000c0de")


def _customer_id(handle: str) -> UUID:
    """The fake resolve()'s answer: one stable customer per handle."""
    return uuid5(NAMESPACE, handle)


def _segment(kind: str = "static") -> Segment:
    return Segment(
        id=uuid4(),
        merchant_id="m1",
        name="Diwali VIPs",
        kind=kind,  # type: ignore[arg-type]
        definition=None if kind == "static" else {"all": []},
        member_count=0,
        computed_at=NOW,
        created_by="ops@x",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeSegments:
    """The segment accessor: one segment, its kind, and the stamped count."""

    def __init__(self, segment: Optional[Segment], locked: Optional[bool] = None):
        self.segment = segment
        # What lock_segment answers inside the atom; defaults to the gather's.
        self.locked = (
            locked
            if locked is not None or segment is None
            else segment.kind == "static"
        )
        self.members: Set[str] = set()

    async def get_segment(self, merchant_id: str, segment_id: str) -> Optional[Segment]:
        return self.segment

    async def lock_segment(self, conn: Any, merchant_id: str, segment_id: str):
        return self.locked

    async def stamp_member_count(
        self, conn: Any, merchant_id: str, segment_id: str
    ) -> Tuple[int, datetime]:
        return len(self.members), NOW


class FakeMembers:
    """The member accessor, sharing the segment fake's member set."""

    def __init__(
        self, segments_fake: FakeSegments, page: Optional[List[SegmentMember]] = None
    ):
        self.segments = segments_fake
        self.inserts: List[Dict[str, Any]] = []
        self.page = page or []

    async def insert_members(
        self, conn: Any, segment_id: str, customer_ids: List[str], source: Optional[str]
    ) -> int:
        self.inserts.append(
            {
                "segment_id": segment_id,
                "customer_ids": list(customer_ids),
                "source": source,
            }
        )
        new = [c for c in customer_ids if c not in self.segments.members]
        self.segments.members.update(new)
        return len(new)

    async def member_page(
        self, merchant_id: str, segment_id: str, after: Optional[str], limit: int
    ) -> List[SegmentMember]:
        return self.page[:limit]


class Resolver:
    """Stands in for identity's resolve(): records every call; a person with
    no usable handle raises ValueError, exactly as the real one does."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def __call__(
        self, merchant_id: str, handles: Dict[str, str], *, evidence: str, source: str
    ) -> UUID:
        self.calls.append(
            {
                "merchant_id": merchant_id,
                "handles": handles,
                "evidence": evidence,
                "source": source,
            }
        )
        usable = [v for v in handles.values() if v and v.strip() and v != "abc"]
        if not usable:
            raise ValueError("resolve() requires at least one usable handle")
        return _customer_id(usable[0])


async def _atomically(fn: Any, *args: Any) -> Any:
    """Test double: run the atom body against a None handle."""
    return await fn(None, *args)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    def _install(segment: Optional[Segment], locked: Optional[bool] = None, page=None):
        segs = FakeSegments(segment, locked)
        members = FakeMembers(segs, page)
        resolver = Resolver()
        monkeypatch.setattr(segments, "segment_accessor", segs)
        monkeypatch.setattr(segments, "segment_member_accessor", members)
        monkeypatch.setattr(segments, "resolve", resolver)
        monkeypatch.setattr(segments, "atomically", _atomically)
        return SimpleNamespace(segments=segs, members=members, resolver=resolver)

    return _install


# --- the table ---


def test_the_member_table_has_an_owner() -> None:
    assert TABLE_OWNERS["crm_segment_member"] == "outreach"


def test_087_keeps_the_canon_shape_and_its_two_invariants() -> None:
    sql = MIGRATION.read_text()
    table = sql.split("CREATE TABLE crm_segment_member (")[1].split(");")[0]
    # T21: the composite PK is the set; a pure child row has no merchant_id.
    assert "PRIMARY KEY (segment_id, customer_id)" in table
    assert "merchant_id" not in table
    assert "REFERENCES crm_segment (id) ON DELETE CASCADE" in table
    assert "REFERENCES crm_customer (id)" in table
    # The trigger: static lists only, and the customer is the segment's
    # merchant's — on insert AND update.
    assert "definition IS NOT NULL" in sql
    assert "merchant_id = segment_merchant" in sql
    assert "BEFORE INSERT OR UPDATE ON crm_segment_member" in sql


# --- the builders ---


def test_every_member_goes_in_one_insert_that_counts_what_it_added() -> None:
    ids = [str(uuid4()), str(uuid4())]
    query, params = insert_members_query("seg-1", ids, "import diwali.csv")
    assert "unnest($2::uuid[])" in query
    assert "ON CONFLICT (segment_id, customer_id) DO NOTHING" in query
    assert "RETURNING customer_id" in query
    assert params == ["seg-1", ids, "import diwali.csv"]


def test_the_member_page_enters_through_the_parent_and_pages_by_key() -> None:
    query, params = member_page_query("m1", "seg-1", None, 200)
    assert "JOIN crm_segment s ON s.id = m.segment_id" in query
    assert "s.merchant_id = $1" in query
    assert "m.customer_id > $3::uuid" in query
    assert "ORDER BY m.customer_id" in query and "LIMIT $4" in query
    assert params == ["m1", "seg-1", None, 200]


def test_the_lock_is_merchant_first_and_for_update() -> None:
    query, params = lock_segment_query("m1", "seg-1")
    assert "WHERE merchant_id = $1 AND id = $2" in query
    assert "FOR UPDATE" in query
    assert params == ["m1", "seg-1"]


def test_the_count_is_recounted_from_the_rows_and_stamped_now() -> None:
    query, params = stamp_member_count_query("m1", "seg-1")
    assert "SELECT count(*)::int" in query and "FROM crm_segment_member" in query
    assert "computed_at = now()" in query
    assert "WHERE merchant_id = $1 AND id = $2" in query
    assert params == ["m1", "seg-1"]


# --- the plan ---


@pytest.mark.parametrize(
    ("resolved", "unique", "duplicates"),
    [
        ([], [], 0),
        (["a", "b", "c"], ["a", "b", "c"], 0),
        (["a", "b", "a", "a"], ["a", "b"], 2),
        (["c", "a", "c"], ["c", "a"], 1),  # first-seen order is kept
    ],
)
def test_duplicates_in_one_upload_are_counted_not_added_twice(
    resolved: List[str], unique: List[str], duplicates: int
) -> None:
    plan = segments.plan_members(resolved)
    assert plan.customer_ids == unique and plan.duplicates == duplicates


# --- adding members ---


async def test_every_person_sent_is_accounted_for_exactly_once(world) -> None:
    w = world(_segment())
    w.segments.members.add(str(_customer_id("+919000000003")))  # already on it
    people = [
        {"phone": "+919000000001"},
        {"phone": "+919000000002"},
        {"phone": "+919000000001"},  # same person again
        {"phone": "abc"},  # nothing usable
        {"phone": "+919000000003"},  # already a member
        {},  # nothing at all
    ]
    report = await segments.add_members("m1", "seg-1", people, "import diwali.csv")
    assert report.received == 6
    assert report.added == 2
    assert report.already_member == 1
    assert report.duplicates_in_upload == 1
    assert [(s.line, s.reason) for s in report.skipped] == [
        (4, segments.SKIP_NO_USABLE_HANDLE),
        (6, segments.SKIP_NO_USABLE_HANDLE),
    ]
    assert (
        report.added
        + report.already_member
        + report.duplicates_in_upload
        + len(report.skipped)
        == report.received
    )
    assert report.member_count == 3 and report.computed_at == NOW
    assert w.members.inserts[0]["source"] == "import diwali.csv"


async def test_every_person_goes_through_resolve_as_imported(world) -> None:
    w = world(_segment())
    await segments.add_members("m1", "seg-1", [{"email": "priya@x.com"}], None)
    assert w.resolver.calls == [
        {
            "merchant_id": "m1",
            "handles": {"email": "priya@x.com"},
            "evidence": "imported",
            "source": segments.MEMBER_SOURCE_LABEL,
        }
    ]


async def test_sending_the_same_people_twice_adds_nobody(world) -> None:
    world(_segment())
    people = [{"phone": "+919000000001"}, {"phone": "+919000000002"}]
    first = await segments.add_members("m1", "seg-1", people, None)
    again = await segments.add_members("m1", "seg-1", people, None)
    assert (first.added, first.already_member) == (2, 0)
    assert (again.added, again.already_member) == (0, 2)
    assert again.member_count == 2


async def test_an_unknown_segment_resolves_nobody(world) -> None:
    w = world(None)
    with pytest.raises(segments.SegmentNotFound):
        await segments.add_members("m1", "seg-x", [{"phone": "+919000000001"}], None)
    assert w.resolver.calls == [] and w.members.inserts == []


async def test_a_dynamic_segment_resolves_nobody(world) -> None:
    w = world(_segment("dynamic"))
    with pytest.raises(segments.SegmentIsDynamic):
        await segments.add_members("m1", "seg-1", [{"phone": "+919000000001"}], None)
    assert w.resolver.calls == [] and w.members.inserts == []


@pytest.mark.parametrize("count", [0, segments.MAX_MEMBERS_PER_ADD + 1])
async def test_an_empty_or_oversized_add_is_refused_before_any_work(
    world, count: int
) -> None:
    w = world(_segment())
    people = [{"phone": f"+9190000{i:05d}"} for i in range(count)]
    with pytest.raises(ValueError):
        await segments.add_members("m1", "seg-1", people, None)
    assert w.resolver.calls == []


async def test_the_atom_rechecks_the_segment_under_its_lock(world) -> None:
    """Static at the gather, gone by the lock: refused inside the atom, so no
    member row is written."""
    w = world(_segment(), locked=None)
    w.segments.locked = None
    with pytest.raises(segments.SegmentNotFound):
        await segments.add_members("m1", "seg-1", [{"phone": "+919000000001"}], None)
    assert w.members.inserts == []


async def test_a_list_turned_dynamic_under_the_lock_is_refused(world) -> None:
    w = world(_segment(), locked=False)
    with pytest.raises(segments.SegmentIsDynamic):
        await segments.add_members("m1", "seg-1", [{"phone": "+919000000001"}], None)
    assert w.members.inserts == []


async def test_an_upload_with_nobody_usable_still_stamps_the_count(world) -> None:
    w = world(_segment())
    report = await segments.add_members("m1", "seg-1", [{"phone": "abc"}], None)
    assert report.added == 0 and len(report.skipped) == 1
    assert w.members.inserts == [
        {"segment_id": "seg-1", "customer_ids": [], "source": None}
    ]
    assert report.member_count == 0 and report.computed_at == NOW


# --- reading members ---


def _member() -> SegmentMember:
    return SegmentMember(customer_id=uuid4(), added_at=NOW, source="import a.csv")


async def test_a_full_page_carries_the_cursor_and_a_short_one_ends(world) -> None:
    rows = [_member() for _ in range(3)]
    world(_segment(), page=rows)
    full = await segments.segment_members("m1", "seg-1", None, 3)
    assert full.next_after == rows[-1].customer_id
    short = await segments.segment_members("m1", "seg-1", None, 5)
    assert len(short.items) == 3 and short.next_after is None


async def test_members_of_a_missing_or_dynamic_segment_are_refused(world) -> None:
    world(None)
    with pytest.raises(segments.SegmentNotFound):
        await segments.segment_members("m1", "seg-x", None, 10)
    world(_segment("dynamic"))
    with pytest.raises(segments.SegmentIsDynamic):
        await segments.segment_members("m1", "seg-1", None, 10)


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


def test_the_add_route_answers_with_the_report(world) -> None:
    w = world(_segment())
    response = _client().post(
        f"/segments/{uuid4()}/members",
        json={
            "merchant_id": "m1",
            "members": [{"phone": "+919000000001"}, {"phone": "abc"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 1 and body["skipped"] == [
        {"line": 2, "reason": "no_usable_handle"}
    ]
    # No source sent: the caller is recorded.
    assert w.members.inserts[0]["source"] == "api: ops@x"


@pytest.mark.parametrize(
    "members",
    [
        [],
        [{"mobile": "+919000000001"}],  # unknown key — a typo is loud
        [{"phone": "+919000000001"}] * (segments.MAX_MEMBERS_PER_ADD + 1),
    ],
)
def test_the_add_route_refuses_a_malformed_body(world, members: List[Any]) -> None:
    w = world(_segment())
    response = _client().post(
        f"/segments/{uuid4()}/members", json={"merchant_id": "m1", "members": members}
    )
    assert response.status_code == 422
    assert w.resolver.calls == []


@pytest.mark.parametrize(
    ("segment", "code"),
    [(None, 404), (_segment("dynamic"), 409)],
)
def test_the_add_route_maps_a_missing_or_dynamic_segment(
    world, segment: Optional[Segment], code: int
) -> None:
    world(segment)
    response = _client().post(
        f"/segments/{uuid4()}/members",
        json={"merchant_id": "m1", "members": [{"phone": "+919000000001"}]},
    )
    assert response.status_code == code


def test_the_add_route_refuses_a_merchant_the_caller_cannot_reach(world) -> None:
    w = world(_segment())
    response = _client(merchants=("m1",)).post(
        f"/segments/{uuid4()}/members",
        json={"merchant_id": "m2", "members": [{"phone": "+919000000001"}]},
    )
    assert response.status_code == 403
    assert w.resolver.calls == []


def test_the_members_route_pages(world) -> None:
    rows = [_member() for _ in range(2)]
    world(_segment(), page=rows)
    response = _client().get(f"/segments/{uuid4()}/members?merchant_id=m1&limit=2")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2 and body["next_after"] == str(rows[-1].customer_id)
    assert (
        _client()
        .get(f"/segments/{uuid4()}/members?merchant_id=m1&after=not-a-uuid")
        .status_code
        == 422
    )
