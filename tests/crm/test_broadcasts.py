"""Broadcasts phase 1 (canon T17 + T22): the badge registry and the draft
broadcast — create, read, list.

What is proven here:
- 088 / 089 / 090 keep the canon's self-defense: the campaign key's slug and
  its window order; the broadcast's closed status enum (the same words as
  the schema's), its three tenant-pinned FKs, the pin target and the list /
  campaign indexes merchant-first; the enrollment FK added NOT VALID;
- both tables have an owner; every builder puts merchant_id at $1 and writes
  no value into the SQL text; a draft is born with no clock but created_at;
- a key is trimmed, lowercased and held to the slug; a name is trimmed and
  never blank; a window may not end before it starts; a taken key is
  CampaignKeyTaken;
- a draft names this merchant's segment, workflow and badge — every missing
  one is reported at once and nothing is stored; an archived workflow is
  refused, a draft or paused one is not;
- every /campaigns and /broadcasts route declares the tenancy door, the root
  router mounts both, and the routes answer 201 / 409 / 422 / 404 / 403;
- the docs example validates against the request models.
"""

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, get_args
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.crm.outreach.broadcasts as broadcasts
import app.crm.outreach.campaigns as campaigns
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm import api as crm_root_api
from app.crm.auth import MERCHANT_SCOPE_MARK
from app.crm.outreach import api as outreach_api
from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.db.queries.broadcast import (
    count_broadcasts_query,
    get_broadcast_query,
    insert_broadcast_query,
    list_broadcasts_query,
)
from app.crm.outreach.db.queries.campaign import (
    get_campaign_query,
    insert_campaign_query,
    list_campaigns_query,
)
from app.crm.outreach.schemas import (
    Broadcast,
    BroadcastStatus,
    Campaign,
    Segment,
)
from scripts.check_crm_boundaries import TABLE_OWNERS

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
MIGRATIONS = Path("app/database/migrations")
EXAMPLE = Path("docs/crm/broadcasts/final-nudge.json")
HOSTILE = "x'); DROP TABLE crm_broadcast; --"


def _sql(name: str) -> str:
    return (MIGRATIONS / name).read_text()


def _segment(merchant_id: str = "m1") -> Segment:
    return Segment(
        id=uuid4(),
        merchant_id=merchant_id,
        name="Offered, not granted",
        kind="dynamic",
        definition={"event": "OFFERED"},
        created_at=NOW,
        updated_at=NOW,
    )


def _campaign(key: str = "diwali-2026", merchant_id: str = "m1") -> Campaign:
    return Campaign(merchant_id=merchant_id, key=key, name="Diwali", created_at=NOW)


class World:
    """Every accessor the two logic files read, as one fake store."""

    def __init__(self) -> None:
        self.segments: Dict[str, Segment] = {}
        self.workflows: Dict[Tuple[str, str], str] = {}  # (merchant, id) -> status
        self.campaigns: List[Campaign] = []
        self.broadcasts: List[Broadcast] = []
        self.inserts: List[Dict[str, Any]] = []

    # segment accessor
    async def get_segment(self, merchant_id: str, segment_id: str) -> Optional[Segment]:
        found = self.segments.get(segment_id)
        return found if found and found.merchant_id == merchant_id else None

    # workflow accessor
    async def workflow_status(
        self, merchant_id: str, workflow_id: str
    ) -> Optional[str]:
        return self.workflows.get((merchant_id, workflow_id))

    # campaign accessor
    async def insert_campaign(
        self,
        merchant_id: str,
        key: str,
        name: str,
        starts_at: Optional[date],
        ends_at: Optional[date],
        created_by: Optional[str],
    ) -> Campaign:
        self.inserts.append({"campaign": key, "name": name, "by": created_by})
        if any(c.merchant_id == merchant_id and c.key == key for c in self.campaigns):
            raise UniqueViolation("crm_campaign_pkey")
        campaign = Campaign(
            merchant_id=merchant_id,
            key=key,
            name=name,
            starts_at=starts_at,
            ends_at=ends_at,
            created_by=created_by,
            created_at=NOW,
        )
        self.campaigns.append(campaign)
        return campaign

    async def get_campaign(self, merchant_id: str, key: str) -> Optional[Campaign]:
        return next(
            (
                c
                for c in self.campaigns
                if c.merchant_id == merchant_id and c.key == key
            ),
            None,
        )

    async def list_campaigns(self, merchant_id: str, limit: int) -> List[Campaign]:
        return [c for c in self.campaigns if c.merchant_id == merchant_id][:limit]

    # broadcast accessor
    async def insert_broadcast(
        self,
        merchant_id: str,
        name: str,
        segment_id: str,
        workflow_id: str,
        campaign_key: Optional[str],
        created_by: Optional[str],
    ) -> Broadcast:
        self.inserts.append({"broadcast": name, "campaign_key": campaign_key})
        broadcast = Broadcast(
            id=uuid4(),
            merchant_id=merchant_id,
            name=name,
            campaign_key=campaign_key,
            segment_id=UUID(segment_id),
            workflow_id=UUID(workflow_id),
            status="draft",
            created_by=created_by,
            created_at=NOW,
        )
        self.broadcasts.append(broadcast)
        return broadcast

    async def get_broadcast(
        self, merchant_id: str, broadcast_id: str
    ) -> Optional[Broadcast]:
        return next(
            (
                b
                for b in self.broadcasts
                if b.merchant_id == merchant_id and str(b.id) == broadcast_id
            ),
            None,
        )

    def _mine(self, merchant_id: str, campaign_key: Optional[str]) -> List[Broadcast]:
        return [
            b
            for b in self.broadcasts
            if b.merchant_id == merchant_id
            and (campaign_key is None or b.campaign_key == campaign_key)
        ]

    async def list_broadcasts(
        self, merchant_id: str, campaign_key: Optional[str], limit: int, offset: int
    ) -> List[Broadcast]:
        return self._mine(merchant_id, campaign_key)[offset : offset + limit]

    async def count_broadcasts(
        self, merchant_id: str, campaign_key: Optional[str]
    ) -> int:
        return len(self._mine(merchant_id, campaign_key))

    def ready(self, workflow_status: str = "live") -> Tuple[str, str]:
        """A segment and a workflow of m1's, and the diwali badge."""
        segment = _segment()
        self.segments[str(segment.id)] = segment
        workflow_id = str(uuid4())
        self.workflows[("m1", workflow_id)] = workflow_status
        self.campaigns.append(_campaign())
        return str(segment.id), workflow_id


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    fake = World()
    monkeypatch.setattr(campaigns, "campaign_accessor", fake)
    for name in (
        "broadcast_accessor",
        "campaign_accessor",
        "segment_accessor",
        "workflow_accessor",
    ):
        monkeypatch.setattr(broadcasts, name, fake)
    return fake


# --- the tables ---


def test_both_tables_have_an_owner() -> None:
    assert TABLE_OWNERS["crm_campaign"] == "outreach"
    assert TABLE_OWNERS["crm_broadcast"] == "outreach"


def test_088_keeps_the_badge_registry_honest() -> None:
    sql = _sql("088_create_crm_campaign.sql")
    assert "CREATE TABLE crm_campaign (" in sql  # not buddy's `campaign`
    assert "PRIMARY KEY (merchant_id, key)" in sql  # merchant first
    assert "key ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(key) <= 64" in sql
    assert "CHECK (name <> '' AND name !~ '^\\s|\\s$')" in sql
    assert "ends_at >= starts_at" in sql


def test_089_status_words_are_the_schemas_words() -> None:
    sql = _sql("089_create_crm_broadcast.sql")
    enum = re.search(r"CHECK \(status IN \(([^)]*)\)\)", sql)
    assert enum is not None
    words = re.findall(r"'([a-z]+)'", enum.group(1))
    assert words == list(get_args(BroadcastStatus))
    assert "status         text NOT NULL DEFAULT 'draft'" in sql


def test_089_pins_every_reference_to_the_merchant() -> None:
    sql = _sql("089_create_crm_broadcast.sql")
    assert (
        "FOREIGN KEY (merchant_id, segment_id) REFERENCES crm_segment (merchant_id, id)"
        in sql
    )
    assert (
        "FOREIGN KEY (merchant_id, workflow_id) REFERENCES crm_workflow (merchant_id, id)"
        in sql
    )
    assert (
        "FOREIGN KEY (merchant_id, campaign_key) REFERENCES crm_campaign (merchant_id, key)"
        in sql
    )
    assert (
        "segment_id     uuid NOT NULL" in sql and "workflow_id    uuid NOT NULL" in sql
    )
    assert "campaign_key   text," in sql  # optional badge
    # The pin target for the run's FK (090) and the recipients (phase 2).
    assert "CREATE UNIQUE INDEX crm_broadcast_tenant_pin_ux" in sql
    assert "ON crm_broadcast (merchant_id, id)" in sql
    for index in re.findall(r"ON crm_broadcast \(([^)]*)\)", sql):
        assert index.startswith("merchant_id"), index


def test_090_adds_the_runs_fk_without_scanning_the_hot_table() -> None:
    sql = _sql("090_add_crm_workflow_enrollment_broadcast_fk.sql")
    assert "ALTER TABLE crm_workflow_enrollment" in sql
    assert "FOREIGN KEY (merchant_id, source_broadcast_id)" in sql
    assert "REFERENCES crm_broadcast (merchant_id, id)" in sql
    assert sql.rstrip().endswith("NOT VALID;")


# --- the builders ---


@pytest.mark.parametrize(
    "built",
    [
        insert_campaign_query("m1", HOSTILE, HOSTILE, None, None, HOSTILE),
        get_campaign_query("m1", HOSTILE),
        list_campaigns_query("m1", 10),
        insert_broadcast_query("m1", HOSTILE, "s", "w", HOSTILE, HOSTILE),
        get_broadcast_query("m1", HOSTILE),
        list_broadcasts_query("m1", HOSTILE, 10, 0),
        list_broadcasts_query("m1", None, 10, 0),
        count_broadcasts_query("m1", HOSTILE),
        count_broadcasts_query("m1", None),
    ],
)
def test_every_builder_is_merchant_first_and_parameterised(
    built: Tuple[str, List[Any]],
) -> None:
    sql, params = built
    assert params[0] == "m1"
    assert re.search(r"merchant_id = \$1", sql) or "VALUES ($1" in sql
    assert HOSTILE not in sql
    placeholders = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert placeholders == set(range(1, len(params) + 1))


def test_a_draft_is_born_with_no_clock_but_created_at() -> None:
    sql, _ = insert_broadcast_query("m1", "n", "s", "w", None, None)
    columns = sql.split("(", 1)[1].split(")", 1)[0]
    for clock in (
        "status",
        "scheduled_for",
        "resolved_at",
        "dispatched_at",
        "closed_at",
    ):
        assert clock not in columns


def test_the_badge_filter_narrows_the_list_and_its_total() -> None:
    sql, params = list_broadcasts_query("m1", "diwali-2026", 20, 40)
    assert "AND campaign_key = $2" in sql and params == ["m1", "diwali-2026", 20, 40]
    assert "LIMIT $3 OFFSET $4" in sql
    plain, plain_params = list_broadcasts_query("m1", None, 20, 40)
    assert "campaign_key =" not in plain and plain_params == ["m1", 20, 40]
    counted, _ = count_broadcasts_query("m1", "diwali-2026")
    assert "AND campaign_key = $2" in counted


# --- campaigns ---


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("diwali-2026", "diwali-2026"),
        ("Diwali-2026", "diwali-2026"),
        ("  bbd-oct ", "bbd-oct"),
        ("q4", "q4"),
    ],
)
def test_a_key_is_trimmed_and_lowercased(raw: str, stored: str) -> None:
    assert campaigns.campaign_key(raw) == stored


@pytest.mark.parametrize(
    "raw",
    ["", "diwali 2026", "diwali--2026", "-diwali", "diwali-", "diwali_26", "a" * 65],
)
def test_a_key_outside_the_slug_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="lowercase letters and digits"):
        campaigns.campaign_key(raw)


async def test_create_campaign_stores_the_clean_key_name_and_author(
    world: World,
) -> None:
    created = await campaigns.create_campaign(
        "m1", " Diwali-2026 ", " Diwali 2026 ", date(2026, 10, 15), None, "ops@x"
    )
    assert (created.key, created.name, created.created_by) == (
        "diwali-2026",
        "Diwali 2026",
        "ops@x",
    )
    with pytest.raises(campaigns.CampaignKeyTaken):
        await campaigns.create_campaign("m1", "diwali-2026", "Again", None, None, None)
    # The same key under another merchant is another season.
    await campaigns.create_campaign("m2", "diwali-2026", "Theirs", None, None, None)


async def test_a_window_that_ends_before_it_starts_stores_nothing(world: World) -> None:
    with pytest.raises(ValueError, match="before starts_at"):
        await campaigns.create_campaign(
            "m1", "late", "Late", date(2026, 11, 5), date(2026, 10, 15), None
        )
    with pytest.raises(ValueError, match="needs a name"):
        await campaigns.create_campaign("m1", "blank", "   ", None, None, None)
    assert world.inserts == []


# --- broadcasts ---


async def test_a_draft_names_this_merchants_segment_workflow_and_badge(
    world: World,
) -> None:
    segment_id, workflow_id = world.ready()
    created = await broadcasts.create_broadcast(
        "m1", " Final nudge ", segment_id, workflow_id, "Diwali-2026", "ops@x"
    )
    assert created.status == "draft" and created.name == "Final nudge"
    assert created.campaign_key == "diwali-2026"
    no_badge = await broadcasts.create_broadcast(
        "m1", "Quick blast", segment_id, workflow_id, None, None
    )
    assert no_badge.campaign_key is None


async def test_every_missing_reference_is_reported_at_once(world: World) -> None:
    world.ready()
    with pytest.raises(broadcasts.BroadcastInvalid) as refused:
        await broadcasts.create_broadcast(
            "m1", "Nudge", str(uuid4()), str(uuid4()), "holi-2027", None
        )
    assert refused.value.problems == [
        "segment_id: this merchant has no such segment",
        "workflow_id: this merchant has no such workflow",
        "campaign_key: 'holi-2027' is not one of this merchant's campaigns — "
        "create it first (POST /campaigns)",
    ]
    assert world.inserts == []


async def test_another_merchants_segment_is_no_such_segment(world: World) -> None:
    segment_id, workflow_id = world.ready()
    theirs = _segment("m2")
    world.segments[str(theirs.id)] = theirs
    with pytest.raises(broadcasts.BroadcastInvalid) as refused:
        await broadcasts.create_broadcast(
            "m1", "Nudge", str(theirs.id), workflow_id, None, None
        )
    assert refused.value.problems == ["segment_id: this merchant has no such segment"]


@pytest.mark.parametrize(
    ("workflow_status", "refused"),
    [("archived", True), ("draft", False), ("paused", False), ("live", False)],
)
async def test_only_an_archived_workflow_is_refused_at_draft_time(
    world: World, workflow_status: str, refused: bool
) -> None:
    segment_id, workflow_id = world.ready(workflow_status)
    if refused:
        with pytest.raises(broadcasts.BroadcastInvalid, match="archived"):
            await broadcasts.create_broadcast(
                "m1", "Nudge", segment_id, workflow_id, None, None
            )
    else:
        await broadcasts.create_broadcast(
            "m1", "Nudge", segment_id, workflow_id, None, None
        )


async def test_a_malformed_key_or_blank_name_is_refused_before_any_read(
    world: World,
) -> None:
    segment_id, workflow_id = world.ready()
    with pytest.raises(ValueError, match="lowercase letters"):
        await broadcasts.create_broadcast(
            "m1", "Nudge", segment_id, workflow_id, "Diwali 2026", None
        )
    with pytest.raises(ValueError, match="needs a name"):
        await broadcasts.create_broadcast(
            "m1", "  ", segment_id, workflow_id, None, None
        )
    assert world.inserts == []


async def test_the_list_is_one_seasons_when_a_badge_is_named(world: World) -> None:
    segment_id, workflow_id = world.ready()
    for badge in ("diwali-2026", None, "diwali-2026"):
        await broadcasts.create_broadcast(
            "m1", "Nudge", segment_id, workflow_id, badge, None
        )
    everything = await broadcasts.list_broadcasts("m1", None, 50, 0)
    season = await broadcasts.list_broadcasts("m1", "Diwali-2026", 50, 0)
    assert everything.total == 3 and season.total == 2
    assert all(b.campaign_key == "diwali-2026" for b in season.items)


# --- the routes ---


def _client(merchants: Tuple[str, ...] = ("m1",)) -> TestClient:
    app = FastAPI()
    app.include_router(outreach_api.campaign_router, prefix="/campaigns")
    app.include_router(outreach_api.broadcast_router, prefix="/broadcasts")
    app.dependency_overrides[get_current_user_with_rbac] = lambda: SimpleNamespace(
        role="user",
        username="u",
        merchant_ids=list(merchants),
        reseller_ids=[],
        email="ops@x",
    )
    return TestClient(app)


def test_every_route_declares_the_tenancy_door() -> None:
    for router, expected in (
        (outreach_api.campaign_router, 2),
        (outreach_api.broadcast_router, 3),
    ):
        routes = [r for r in router.routes if isinstance(r, APIRoute)]
        assert len(routes) == expected, "a route was added or lost — update this walk"
        for route in routes:
            assert any(
                getattr(d.call, MERCHANT_SCOPE_MARK, False)
                for d in route.dependant.dependencies
            ), route.path


def test_the_root_router_mounts_both() -> None:
    paths = {getattr(r, "path", "") for r in crm_root_api.router.routes}
    assert {"/campaigns", "/broadcasts", "/broadcasts/{broadcast_id}"} <= paths


def test_the_campaign_routes(world: World) -> None:
    client = _client()
    body = {"merchant_id": "m1", "key": "Diwali-2026", "name": "Diwali 2026"}
    created = client.post("/campaigns", json=body)
    assert created.status_code == 201 and created.json()["key"] == "diwali-2026"
    assert client.post("/campaigns", json=body).status_code == 409
    bad = client.post("/campaigns", json={**body, "key": "diwali 2026"})
    assert bad.status_code == 422
    listed = client.get("/campaigns?merchant_id=m1")
    assert [c["key"] for c in listed.json()] == ["diwali-2026"]
    other = _client(merchants=("m1",)).post(
        "/campaigns", json={**body, "merchant_id": "m2"}
    )
    assert other.status_code == 403


def test_the_broadcast_routes(world: World) -> None:
    segment_id, workflow_id = world.ready()
    client = _client()
    body = {
        "merchant_id": "m1",
        "name": "Final nudge",
        "segment_id": segment_id,
        "workflow_id": workflow_id,
        "campaign_key": "diwali-2026",
    }
    created = client.post("/broadcasts", json=body)
    assert created.status_code == 201 and created.json()["status"] == "draft"
    broadcast_id = created.json()["id"]
    one = client.get(f"/broadcasts/{broadcast_id}?merchant_id=m1")
    assert one.status_code == 200 and one.json()["name"] == "Final nudge"
    assert client.get(f"/broadcasts/{uuid4()}?merchant_id=m1").status_code == 404
    page = client.get("/broadcasts?merchant_id=m1&campaign_key=diwali-2026").json()
    assert page["total"] == 1
    missing = client.post("/broadcasts", json={**body, "segment_id": str(uuid4())})
    assert missing.status_code == 422
    assert missing.json()["detail"] == ["segment_id: this merchant has no such segment"]
    not_a_uuid = client.post("/broadcasts", json={**body, "workflow_id": "w-1"})
    assert not_a_uuid.status_code == 422
    other = client.post("/broadcasts", json={**body, "merchant_id": "m2"})
    assert other.status_code == 403


# --- the docs example ---


def test_the_docs_example_validates_against_the_request_models() -> None:
    example = json.loads(EXAMPLE.read_text())
    campaign = outreach_api.CampaignCreate.model_validate(example["campaign"])
    broadcast = outreach_api.BroadcastCreate.model_validate(example["broadcast"])
    assert campaigns.campaign_key(campaign.key) == broadcast.campaign_key
    assert (
        campaign.starts_at
        and campaign.ends_at
        and campaign.ends_at >= campaign.starts_at
    )
