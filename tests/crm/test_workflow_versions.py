"""Version storage + the on_publish word (rollout phase 11, ADR 0023).

Publish writes an immutable crm_workflow_version row; a plan declares
on_publish: pin (default — new entrants take vN+1, runs in flight finish
vN) or migrate (every open run is re-pinned inside the publish atom, and
only when the stranding validator passes — today's semantics as a mode).
Nothing reads the pin yet (phase 12); this phase makes the rows exist."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

import pytest

import app.crm.outreach.api as outreach_api
import app.crm.outreach.plans as plans
import app.crm.outreach.versions as versions
from app.crm.outreach.db import DbTxn
from app.crm.outreach.db.queries.enrollment import (
    occupied_nodes_on_version_query,
    repin_open_runs_query,
    repin_runs_on_version_query,
    runs_referencing_template_query,
)
from app.crm.outreach.db.queries.version import (
    get_definition_query,
    insert_version_query,
    list_versions_query,
)
from app.crm.outreach.db.queries.workflow import live_plans_naming_template_query
from app.crm.outreach.plans import validate_definition, validate_migration
from app.crm.outreach.schemas import Workflow, WorkflowDefinition
from scripts.check_crm_boundaries import TABLE_OWNERS
from tests.crm.doubles import patch_accessors

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

    async def lock_templates_shared(self, conn: Any, *args: Any) -> None:
        return None  # phase 14's lock; pinned by test_workflow_plans.py

    async def insert_version(self, conn: Any, *args: Any) -> None:
        self.versions.append(args)

    async def repin_open_runs(self, conn: Any, *args: Any) -> int:
        self.repins.append(args)
        return 3


async def _publish(
    monkeypatch: pytest.MonkeyPatch, draft: Dict[str, Any], occupied: List[str]
) -> _PublishAccessor:
    accessor = _PublishAccessor(draft, occupied)
    patch_accessors(monkeypatch, plans, accessor)

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
    patch_accessors(monkeypatch, plans, accessor)
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


# --- rollout phase 14: version operations — migrate-forward, the retirement
# guard's count, the versions list ---

# v3: wait, then a call. v4 fixes the call's template id (the typo-fix
# case migrate-forward exists for); the two bad targets drop the occupied
# square, or change the entry.
_V3: Dict[str, Any] = {
    **_PLAN,
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {"id": "call", "type": "call", "template_id": "tpl-1"},
    ],
    "edges": [["wait-30m", "call"]],
}
_V4_FIXED: Dict[str, Any] = {
    **_V3,
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {"id": "call", "type": "call", "template_id": "tpl-2"},
    ],
}
_V4_DROPS_CALL = _PLAN
_V4_NEW_ENTRY: Dict[str, Any] = {
    **_V3,
    "entry": {**_V3["entry"], "topic": "checkout.started"},
}


def test_validate_migration_refuses_a_target_missing_an_occupied_square() -> None:
    problems = validate_migration(_V3, _V4_DROPS_CALL, ["call"])
    assert any("call" in p and "standing on it" in p for p in problems)


def test_validate_migration_refuses_a_target_with_a_different_entry() -> None:
    problems = validate_migration(_V3, _V4_NEW_ENTRY, ["wait-30m"])
    assert any("entry rule" in p for p in problems)
    # nothing pinned to the source: nothing in flight to re-admit — allowed
    assert validate_migration(_V3, _V4_NEW_ENTRY, []) == []


def test_validate_migration_accepts_a_target_keeping_every_square_and_the_entry() -> (
    None
):
    assert validate_migration(_V3, _V4_FIXED, ["wait-30m", "call"]) == []
    # by meaning, not spelling (B3): a target that spells out a default
    spelled = {**_V4_FIXED, "entry": {**_V4_FIXED["entry"], "on_repeat": "ignore"}}
    assert validate_migration(_V3, spelled, ["call"]) == []
    # nothing standing anywhere: only the entry matters
    assert validate_migration(_V3, _V4_DROPS_CALL, []) == []


def test_validate_migration_reports_an_unparseable_document() -> None:
    problems = validate_migration(_V3, {"entry": {}}, [])
    assert problems and "shape invalid" in problems[0]


# --- the builders ---


def test_migration_reads_and_repins_only_runs_on_the_from_version() -> None:
    sql, params = occupied_nodes_on_version_query("m1", "wf-1", 3)
    assert "merchant_id = $1 AND workflow_id = $2 AND workflow_version = $3" in sql
    assert "status <> 'exited'" in sql and params == ["m1", "wf-1", 3]
    sql, params = repin_runs_on_version_query("m1", "wf-1", 3, 4)
    assert "SET workflow_version = $4" in sql
    assert "workflow_version = $3" in sql and "status <> 'exited'" in sql
    assert "RETURNING id" in sql and params == ["m1", "wf-1", 3, 4]


def test_versions_are_never_deleted() -> None:
    """ADR 0023 §5 as amended: no sweep, no dial, no DELETE builder — an
    exited run's workflow_version must keep answering what it executed."""
    import importlib
    import pkgutil

    import app.crm.outreach.db.queries as queries_pkg

    builders = [
        name
        for _, module, _ in pkgutil.iter_modules(queries_pkg.__path__)
        for name in dir(importlib.import_module(f"{queries_pkg.__name__}.{module}"))
    ]
    assert not [name for name in builders if "sweep" in name and "version" in name]
    assert not hasattr(versions, "sweep_unreferenced_versions_tick")
    assert all(
        "DELETE FROM crm_workflow_version" not in p.read_text()
        for p in Path(queries_pkg.__file__).parent.glob("*.py")
    )


def test_runs_referencing_a_template_are_counted_by_their_pinned_document() -> None:
    sql, params = runs_referencing_template_query("m1", "whatsapp", "cart_recovery_1")
    assert "e.merchant_id = $1" in sql and "e.status <> 'exited'" in sql
    assert "v.version = e.workflow_version" in sql
    assert "jsonb_array_elements(v.definition->'nodes')" in sql
    assert "node->>'type' = 'send'" in sql
    assert "node->>'channel' = $2" in sql and "node->>'template' = $3" in sql
    assert params == ["m1", "whatsapp", "cart_recovery_1"]


def test_the_versions_list_is_merchant_first_newest_first_with_open_run_counts() -> (
    None
):
    sql, params = list_versions_query("m1", "wf-1")
    assert "FROM crm_workflow_version v" in sql
    assert "v.merchant_id = $1 AND v.workflow_id = $2" in sql
    assert "AS open_runs" in sql and "e.status <> 'exited'" in sql
    assert "ORDER BY v.version DESC" in sql
    assert params == ["m1", "wf-1"]


def test_live_and_paused_plans_naming_a_template_are_counted_by_their_latest() -> None:
    """The guard's second count: a plan with no run open right now still
    strands its next entrant — live, or paused and able to go live."""
    sql, params = live_plans_naming_template_query("m1", "whatsapp", "cart_recovery_1")
    assert "FROM crm_workflow w" in sql
    assert "w.merchant_id = $1" in sql and "w.status IN ('live', 'paused')" in sql
    assert "jsonb_array_elements(w.definition->'nodes')" in sql
    assert "node->>'channel' = $2" in sql and "node->>'template' = $3" in sql
    assert params == ["m1", "whatsapp", "cart_recovery_1"]


@pytest.mark.asyncio
async def test_template_references_answers_both_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Counts:
        async def runs_referencing_template(self, m: str, c: str, n: str) -> int:
            return 2

        async def live_plans_naming_template(self, m: str, c: str, n: str) -> int:
            return 1

    patch_accessors(monkeypatch, versions, _Counts())
    assert await versions.template_references("m1", "whatsapp", "x") == (2, 1)


# --- the migrate atom: validate, then one UPDATE, or nothing ---


class _MigrateAccessor:
    def __init__(
        self, documents: Dict[int, Dict[str, Any]], occupied: List[str], moved: int = 2
    ) -> None:
        self.documents = documents
        self.occupied = occupied
        self.moved = moved
        self.repins: List[Tuple[Any, ...]] = []
        self.locked: List[List[Tuple[str, str]]] = []
        self.order: List[str] = []

    async def lock_templates_shared(
        self, conn: Any, m: str, templates: List[Tuple[str, str]]
    ) -> None:
        self.locked.append(list(templates))
        self.order.append("lock")

    async def pinned_definition(
        self, conn: Any, m: str, w: str, version: int
    ) -> Optional[Dict[str, Any]]:
        return self.documents.get(version)

    async def occupied_nodes_on_version(
        self, conn: Any, m: str, w: str, version: int
    ) -> List[str]:
        return list(self.occupied)

    async def repin_runs_on_version(
        self, conn: Any, m: str, w: str, from_version: int, to_version: int
    ) -> int:
        self.repins.append((m, w, from_version, to_version))
        self.order.append("repin")
        return self.moved


async def _migrate(
    monkeypatch: pytest.MonkeyPatch,
    accessor: _MigrateAccessor,
    from_version: int = 3,
    to_version: int = 4,
) -> int:
    patch_accessors(monkeypatch, versions, accessor)
    return await versions._migrate_forward_in_txn(
        cast(DbTxn, object()), "m1", "wf-1", from_version, to_version
    )


@pytest.mark.asyncio
async def test_migrate_forward_moves_every_open_run_on_the_from_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _MigrateAccessor({3: _V3, 4: _V4_FIXED}, occupied=["call"])
    assert await _migrate(monkeypatch, accessor) == 2
    assert accessor.repins == [("m1", "wf-1", 3, 4)]


@pytest.mark.asyncio
async def test_migrate_forward_refuses_a_stranding_target_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _MigrateAccessor({3: _V3, 4: _V4_DROPS_CALL}, occupied=["call"])
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await _migrate(monkeypatch, accessor)
    assert any("standing on it" in p for p in refused.value.problems)
    assert accessor.repins == []


@pytest.mark.asyncio
async def test_migrate_forward_needs_both_versions_and_a_real_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(versions.VersionNotFound):
        await _migrate(monkeypatch, _MigrateAccessor({3: _V3}, []), 3, 4)
    with pytest.raises(versions.VersionNotFound):
        await _migrate(monkeypatch, _MigrateAccessor({4: _V4_FIXED}, []), 3, 4)
    with pytest.raises(plans.WorkflowValidationError):
        await _migrate(monkeypatch, _MigrateAccessor({3: _V3}, []), 3, 3)


_V4_SENDS: Dict[str, Any] = {
    **_V4_FIXED,
    "nodes": [
        {"id": "wait-30m", "type": "wait", "minutes": 30},
        {"id": "call", "type": "call", "template_id": "tpl-2"},
        {"id": "wa", "type": "send", "channel": "whatsapp", "template": "cart_1"},
    ],
    "edges": [["wait-30m", "call"], ["call", "wa"]],
    "purpose_key": "marketing.cart.recovery",
}


@pytest.mark.asyncio
async def test_migrate_forward_refuses_a_target_whose_template_is_no_longer_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target is an old version: what was approved at its publish may
    have been retired since. Publish's own check runs on the target, under
    the shared template lock, before any run is moved."""
    accessor = _MigrateAccessor({3: _V3, 4: _V4_SENDS}, occupied=["call"])

    async def retired(merchant_id: str, definition: WorkflowDefinition) -> List[str]:
        accessor.order.append("check")
        return ["send node wa: template 'cart_1' is 'deleted', not approved"]

    monkeypatch.setattr(plans, "_template_problems", retired)
    with pytest.raises(plans.WorkflowValidationError) as refused:
        await _migrate(monkeypatch, accessor)
    assert any("not approved" in p for p in refused.value.problems)
    assert accessor.repins == []
    assert accessor.locked == [[("whatsapp", "cart_1")]]
    assert accessor.order == ["lock", "check"]


@pytest.mark.asyncio
async def test_migrate_forward_holds_the_targets_templates_then_checks_then_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessor = _MigrateAccessor({3: _V3, 4: _V4_SENDS}, occupied=["call"])

    async def approved(merchant_id: str, definition: WorkflowDefinition) -> List[str]:
        accessor.order.append("check")
        return []

    monkeypatch.setattr(plans, "_template_problems", approved)
    assert await _migrate(monkeypatch, accessor) == 2
    assert accessor.order == ["lock", "check", "repin"]


# --- the template lock (shared/locks.py): one key, two sides ---


def test_the_template_lock_key_is_stable_and_bigint_sized() -> None:
    from app.crm.shared.locks import template_lock_key

    key = template_lock_key("m1", "whatsapp", "cart_1")
    assert key == template_lock_key("m1", "whatsapp", "cart_1")
    assert key != template_lock_key("m1", "whatsapp", "cart_2")
    assert key != template_lock_key("m2", "whatsapp", "cart_1")
    assert -(2**63) <= key < 2**63


def test_pinners_lock_shared_and_retirement_locks_exclusive() -> None:
    from app.crm.connectivity.db.queries.template import lock_template_exclusive_query
    from app.crm.outreach.db.queries.version import lock_template_shared_query

    sql, params = lock_template_shared_query(42)
    assert "pg_advisory_xact_lock_shared($1::bigint)" in sql and params == [42]
    sql, params = lock_template_exclusive_query(42)
    assert "pg_advisory_xact_lock($1::bigint)" in sql and "_shared" not in sql
    assert params == [42]


@pytest.mark.asyncio
async def test_the_migrate_route_threads_the_versions_and_answers_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[Tuple[Any, ...]] = []

    async def migrate_forward(
        merchant_id: str, workflow_id: str, from_version: int, to_version: int
    ) -> int:
        seen.append((merchant_id, workflow_id, from_version, to_version))
        return 5

    monkeypatch.setattr(versions, "migrate_forward", migrate_forward)
    user = cast(Any, type("U", (), {"email": "ops@x"})())
    result = await outreach_api.migrate_version_route(
        "wf-1", 3, merchant_id="m1", to=4, current_user=user
    )
    assert (result.from_version, result.to_version, result.moved) == (3, 4, 5)
    assert seen == [("m1", "wf-1", 3, 4)]


# --- versions are never deleted (ADR 0023 §5; migration 067) ------------------


def test_versions_are_undeletable_by_trigger_not_by_discipline() -> None:
    # 064 shipped the UPDATE guard and said a sweep would delete old
    # versions; the sweep was dropped and the decision became "kept for
    # the life of the plan". With one DB role, a decision kept by
    # discipline is not kept — 067 refuses every DELETE in the table.
    from pathlib import Path as _Path

    sql = _Path("app/database/migrations/067_crm_workflow_version_delete_guard.sql")
    assert sql.exists(), "the DELETE guard migration is missing"
    text = sql.read_text()
    assert "BEFORE DELETE ON crm_workflow_version" in text
    assert "RAISE EXCEPTION" in text
    assert "crm_workflow_version_delete_guard" in text
