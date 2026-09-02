"""Version storage + the on_publish word (rollout phase 11, ADR 0023).

Publish writes an immutable crm_workflow_version row; a plan declares
on_publish: pin (default — new entrants take vN+1, runs in flight finish
vN) or migrate (every open run is re-pinned inside the publish atom, and
only when the stranding validator passes — today's semantics as a mode).
Nothing reads the pin yet (phase 12); this phase makes the rows exist."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import pytest

import app.crm.outreach.api as outreach_api
import app.crm.outreach.plans as plans
from app.crm.outreach.db import DbTxn
from app.crm.outreach.db.queries import (
    get_definition_query,
    insert_version_query,
    repin_open_runs_query,
)
from app.crm.outreach.plans import validate_definition
from app.crm.outreach.schemas import Workflow, WorkflowDefinition
from scripts.check_crm_boundaries import TABLE_OWNERS

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

_PLAN: Dict[str, Any] = {
    "entry": {"topic": "checkout.initiated", "reenter": True, "cooldown_hours": 0},
    "nodes": [{"id": "wait-30m", "type": "wait", "minutes": 30}],
    "edges": [],
    "goal": {"topics": ["order.placed"]},
}


# --- the word ---


def test_on_publish_defaults_to_pin_and_is_closed_vocabulary() -> None:
    assert WorkflowDefinition.model_validate(_PLAN).on_publish == "pin"
    assert (
        WorkflowDefinition.model_validate({**_PLAN, "on_publish": "migrate"}).on_publish
        == "migrate"
    )
    problems = validate_definition({**_PLAN, "on_publish": "rollback"})
    assert problems and "shape invalid" in problems[0]


# --- the table's owner ---


def test_the_version_table_has_an_owner() -> None:
    assert TABLE_OWNERS["crm_workflow_version"] == "outreach"


# --- the builders ---


def test_insert_version_is_merchant_first_and_immutable_by_shape() -> None:
    sql, params = insert_version_query(
        "m1", "wf-1", 3, {"entry": {"topic": "t"}}, "pin", "ops@x"
    )
    assert "INSERT INTO crm_workflow_version" in sql
    assert (
        "(merchant_id, workflow_id, version, definition, on_publish, published_by)"
        in sql
    )
    assert (
        "$4::jsonb" in sql and "ON CONFLICT" not in sql
    )  # a duplicate version is a bug, not a merge
    assert params[0] == "m1" and params[2] == 3
    assert json.loads(params[3]) == {"entry": {"topic": "t"}}
    assert params[4:] == ["pin", "ops@x"]


def test_repin_moves_only_open_runs_of_this_plan() -> None:
    sql, params = repin_open_runs_query("m1", "wf-1", 4)
    assert "SET workflow_version = $3" in sql
    assert "merchant_id = $1 AND workflow_id = $2" in sql
    assert "status <> 'exited'" in sql and "RETURNING id" in sql
    assert params == ["m1", "wf-1", 4]


def test_get_definition_reads_one_pinned_document() -> None:
    sql, params = get_definition_query("m1", "wf-1", 2)
    assert "FROM crm_workflow_version" in sql
    assert "merchant_id = $1 AND workflow_id = $2 AND version = $3" in sql
    assert params == ["m1", "wf-1", 2]


# --- the publish atom writes the row, and re-pins only for migrate ---


def _workflow(draft: Dict[str, Any], definition: Optional[Dict[str, Any]]) -> Workflow:
    return Workflow(
        id=uuid4(),
        merchant_id="m1",
        name="plan",
        status="live" if definition else "draft",
        version=1 if definition else 0,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        definition=definition,
        draft=draft,
    )


class _PublishAccessor:
    def __init__(self, draft: Dict[str, Any], occupied: List[str]) -> None:
        self.draft = draft
        self.occupied = occupied
        self.versions: List[Tuple[Any, ...]] = []
        self.repins: List[Tuple[Any, ...]] = []

    async def workflow_for_publish(self, conn: Any, m: str, w: str) -> Workflow:
        return _workflow(self.draft, _PLAN)

    async def occupied_nodes(self, conn: Any, m: str, w: str) -> List[str]:
        return self.occupied

    async def apply_publish(self, conn: Any, m: str, w: str) -> Workflow:
        published = _workflow({}, self.draft)
        published.version = 2
        return published

    async def insert_version(self, conn: Any, *args: Any) -> None:
        self.versions.append(args)

    async def repin_open_runs(self, conn: Any, *args: Any) -> int:
        self.repins.append(args)
        return 3


async def _publish(
    monkeypatch: pytest.MonkeyPatch, draft: Dict[str, Any], occupied: List[str]
) -> _PublishAccessor:
    accessor = _PublishAccessor(draft, occupied)
    monkeypatch.setattr(plans, "accessor", accessor)

    async def no_templates(
        merchant_id: str, definition: WorkflowDefinition
    ) -> List[str]:
        return []

    monkeypatch.setattr(plans, "_template_problems", no_templates)
    await plans._publish_in_txn(cast(DbTxn, object()), "m1", "wf-1", "ops@x")
    return accessor


@pytest.mark.asyncio
async def test_a_pinned_publish_writes_the_version_row_and_touches_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = {**_PLAN, "nodes": [{"id": "wait-1h", "type": "wait", "minutes": 60}]}
    accessor = await _publish(
        monkeypatch, draft, occupied=["wait-30m"]
    )  # a removed node — fine under pin
    ((merchant, workflow_id, version, definition, on_publish, published_by),) = (
        accessor.versions
    )
    assert (merchant, workflow_id, version, on_publish, published_by) == (
        "m1",
        "wf-1",
        2,
        "pin",
        "ops@x",
    )
    assert definition == draft
    assert accessor.repins == []


@pytest.mark.asyncio
async def test_a_migrating_publish_repins_every_open_run_in_the_same_atom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = {**_PLAN, "on_publish": "migrate"}
    accessor = await _publish(monkeypatch, draft, occupied=["wait-30m"])
    assert accessor.versions[0][4] == "migrate"
    assert accessor.repins == [("m1", "wf-1", 2)]


@pytest.mark.asyncio
async def test_a_migrating_publish_that_would_strand_is_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = {
        **_PLAN,
        "on_publish": "migrate",
        "nodes": [{"id": "wait-1h", "type": "wait", "minutes": 60}],
    }
    accessor = _PublishAccessor(draft, occupied=["wait-30m"])
    monkeypatch.setattr(plans, "accessor", accessor)
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await plans._publish_in_txn(cast(DbTxn, object()), "m1", "wf-1", None)
    assert any("waiting runs standing on it" in p for p in refused.value.problems)
    assert accessor.versions == [] and accessor.repins == []


@pytest.mark.asyncio
async def test_the_publish_route_threads_the_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[Tuple[Any, ...]] = []

    async def publish_workflow(
        merchant_id: str, workflow_id: str, published_by: Optional[str] = None
    ) -> Workflow:
        seen.append((merchant_id, workflow_id, published_by))
        return _workflow({}, _PLAN)

    monkeypatch.setattr(plans, "publish_workflow", publish_workflow)
    user = cast(Any, type("U", (), {"email": "ops@x"})())
    await outreach_api.publish_workflow_route(
        "wf-1", merchant_id="m1", current_user=user
    )
    assert seen == [("m1", "wf-1", "ops@x")]
