"""The decision log (B4): what reaches the database."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.crm.permission import decisions
from app.crm.permission.db.queries import CRM_DECISION_LOG_TABLE, insert_decision_query
from app.crm.permission.schemas import DecisionKind

NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)
MIGRATIONS = Path(__file__).resolve().parents[3] / "app/database/migrations"
DECISION_DDL = (MIGRATIONS / "056_create_crm_decision_log.sql").read_text()


class FakeConn:
    def __init__(self) -> None:
        self.rows: List[Tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *values: Any) -> Optional[Dict[str, Any]]:
        self.rows.append(values)
        return {
            "id": 84121,
            "merchant_id": values[0],
            "customer_id": values[1],
            "decision_kind": values[2],
            "chosen": values[3],
            "decided_at": NOW,
        }


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


def test_the_insert_is_parameterized_and_returns_the_row() -> None:
    sql, params = insert_decision_query("m_123", "c_777", "send_or_hold", "{}")
    assert f"INSERT INTO {CRM_DECISION_LOG_TABLE}" in sql
    assert "RETURNING" in sql
    assert params == ["m_123", "c_777", "send_or_hold", "{}"]


def test_decided_at_is_left_to_the_database() -> None:
    """The clock is the moment the decision was reached; a caller-supplied time
    would let a row claim a moment it was not decided at."""
    sql, params = insert_decision_query("m_123", None, "send_or_hold", "{}")
    assert "decided_at" not in sql.split("VALUES")[0]
    assert len(params) == 4


async def test_the_reasoning_is_serialized_with_the_enum_value(conn: FakeConn) -> None:
    record = await decisions.log_decision(
        conn,  # type: ignore[arg-type]
        merchant_id="m_123",
        customer_id="00000000-0000-0000-0000-000000000777",
        decision_kind=DecisionKind.SEND_OR_HOLD,
        chosen={"verdict": "block", "reason": "quiet_hours", "at": NOW},
    )
    _, _, kind, chosen_json = conn.rows[0]
    assert kind == "send_or_hold"  # the value, never the Enum repr
    ink = json.loads(chosen_json)
    assert ink["reason"] == "quiet_hours"
    assert ink["at"].startswith("2026-08-23")  # non-JSON natives survive
    assert record.id == 84121


async def test_a_non_finite_number_is_refused_before_postgres_sees_it(
    conn: FakeConn,
) -> None:
    """A sent/limit ratio can produce NaN; json.dumps would emit bare NaN, which
    the ::jsonb cast rejects — inside the caller's transaction, taking the
    business write down with it."""
    with pytest.raises(ValueError):
        await decisions.log_decision(
            conn,  # type: ignore[arg-type]
            merchant_id="m_123",
            decision_kind=DecisionKind.SEND_OR_HOLD,
            chosen={"verdict": "block", "ratio": float("nan")},
        )


async def test_every_decision_kind_reaches_the_database_as_its_value(
    conn: FakeConn,
) -> None:
    for kind in DecisionKind:
        await decisions.log_decision(
            conn,  # type: ignore[arg-type]
            merchant_id="m_123",
            decision_kind=kind,
            chosen={"verdict": "recorded"},
        )
    assert {row[2] for row in conn.rows} == {k.value for k in DecisionKind}


def test_the_decision_kind_enum_matches_the_migration_check() -> None:
    for kind in DecisionKind:
        assert f"'{kind.value}'" in DECISION_DDL, kind


def test_a_row_cannot_exist_without_a_verdict() -> None:
    """Law 9: the invariant lives in the table, not in a second Python guard."""
    assert "chosen ? 'verdict'" in DECISION_DDL


def test_a_json_array_does_not_satisfy_the_verdict_check() -> None:
    """`?` on an array asks whether it CONTAINS the string, so '["verdict"]'
    passes the key test while having no verdict field — the audit read would
    find nothing where the constraint promised something."""
    assert "jsonb_typeof(chosen) = 'object'" in DECISION_DDL


def test_the_decision_log_refuses_edits_and_deletes() -> None:
    """A verdict that can be rewritten afterwards is a claim about an audit
    record, not one. Same rule as the consent ledger."""
    for trigger in ("crm_decision_log_no_update", "crm_decision_log_no_delete"):
        assert trigger in DECISION_DDL, trigger
    assert "BEFORE UPDATE ON crm_decision_log" in DECISION_DDL
    assert "BEFORE DELETE ON crm_decision_log" in DECISION_DDL
