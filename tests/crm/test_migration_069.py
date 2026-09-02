"""Migration 069 over BOTH entry shapes: the single door object and the
list of doors (rollout phase 15). Runs the migration's own SQL, verbatim,
against a temp table that shadows crm_workflow for the session — so the
statements under test are the statements that ship, not a copy.

  CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
      tests/crm/test_migration_069.py

Unset, the DB-backed tests skip (tests/crm/conftest.py)."""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import asyncpg
import pytest

from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed migration test"
)

SQL = Path("app/database/migrations/069_migrate_workflow_where_to_conditions.sql")

OBJECT_SHAPE = {
    "entry": {"topic": "orders/create", "where": {"gateway": "COD"}},
    "nodes": [],
}
LIST_SHAPE = {
    "entry": [
        {"topic": "orders/create", "start": "a", "where": {"gateway": "COD"}},
        {"topic": "orders/paid", "start": "b"},
        {"topic": "checkouts/update", "start": "c", "where": {"currency": "INR"}},
    ],
    "nodes": [],
}
ALREADY_TYPED = {
    "entry": [
        {
            "topic": "orders/create",
            "start": "a",
            "where": [{"field": "payload.gateway", "op": "is", "value": "UPI"}],
        }
    ],
    "nodes": [],
}
COD = [{"field": "payload.gateway", "op": "is", "value": "COD"}]
INR = [{"field": "payload.currency", "op": "is", "value": "INR"}]


async def _migrate(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "CREATE TEMP TABLE crm_workflow (n int, definition jsonb, draft jsonb)"
        )
        for i, row in enumerate(rows):
            await conn.execute(
                "INSERT INTO crm_workflow VALUES ($1, $2::jsonb, $3::jsonb)",
                i,
                json.dumps(row["definition"]) if row.get("definition") else None,
                json.dumps(row["draft"]) if row.get("draft") else None,
            )
        await conn.execute(SQL.read_text())
        await conn.execute(SQL.read_text())  # idempotent: a re-run is a no-op
        out = await conn.fetch("SELECT definition, draft FROM crm_workflow ORDER BY n")
        return [
            {
                "definition": json.loads(r["definition"]) if r["definition"] else None,
                "draft": json.loads(r["draft"]) if r["draft"] else None,
            }
            for r in out
        ]
    finally:
        await conn.close()


def test_069_rewrites_the_object_shape_and_every_door_of_the_list_shape() -> None:
    rows = asyncio.run(
        _migrate(
            [
                {"definition": OBJECT_SHAPE, "draft": OBJECT_SHAPE},
                {"definition": LIST_SHAPE, "draft": LIST_SHAPE},
                {"definition": ALREADY_TYPED, "draft": None},
            ]
        )
    )
    for column in ("definition", "draft"):
        assert rows[0][column]["entry"]["where"] == COD
        doors = rows[1][column]["entry"]
        assert [d["topic"] for d in doors] == [
            "orders/create",
            "orders/paid",
            "checkouts/update",
        ], "door order is kept"
        assert doors[0]["where"] == COD
        assert "where" not in doors[1], "a door with no map is untouched"
        assert doors[2]["where"] == INR
        assert doors[0]["start"] == "a", "the door's other words survive"
    assert rows[2]["definition"] == ALREADY_TYPED, "typed already: not touched"
    assert rows[2]["draft"] is None
