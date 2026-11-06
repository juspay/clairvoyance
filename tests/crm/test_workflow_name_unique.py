"""One non-archived plan per (merchant, name) — migration 086.

A provisioner adopts a merchant's plan by name, so a second create of the
same name must be refused, not stored: the database index raises, and the
create door turns it into WorkflowNameTaken (a 409 at the API)."""

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from app.crm.outreach import plans
from app.crm.outreach.db import UniqueViolation

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app/database/migrations/086_crm_workflow_name_unique.sql"
)


def _board() -> Dict[str, Any]:
    return {
        "entry": {"topic": "orders/create", "key": "id"},
        "nodes": [{"id": "hold", "type": "wait", "minutes": 1}],
        "goals": [{"topics": ["orders/cancelled"]}],
    }


@pytest.fixture
def no_catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def gather(merchant_id: str, raw: Dict[str, Any]) -> plans.Catalogs:
        return None  # shape laws only: the door's refusal is the point here

    monkeypatch.setattr(plans, "_gather_catalogs", gather)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_catalogs")
async def test_a_second_plan_of_the_same_name_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def insert_workflow(
        merchant_id: str, name: str, draft: Dict[str, Any], created_by: Optional[str]
    ) -> Any:
        raise UniqueViolation("crm_workflow_name_ux")

    monkeypatch.setattr(plans.workflow_accessor, "insert_workflow", insert_workflow)

    with pytest.raises(plans.WorkflowNameTaken) as caught:
        await plans.create_workflow(
            "shop.myshopify.com", "order-confirmation", _board(), None
        )
    assert str(caught.value) == "order-confirmation"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_catalogs")
async def test_a_validation_problem_is_still_a_validation_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name check never hides the document's own problems: an invalid
    board is refused before any insert is tried."""

    async def insert_workflow(*args: Any) -> Any:
        raise AssertionError("an invalid board must not reach the insert")

    monkeypatch.setattr(plans.workflow_accessor, "insert_workflow", insert_workflow)

    with pytest.raises(plans.WorkflowValidationError):
        await plans.create_workflow("m1", "broken", {"nodes": []}, None)


def test_the_index_is_per_merchant_case_blind_and_frees_archived_names() -> None:
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS crm_workflow_name_ux "
        "ON crm_workflow (merchant_id, lower(name)) "
        "WHERE status <> 'archived';"
    ) in sql
