"""Migrations 084-086: per-table autovacuum triggers.

lead_call_tracker and crm_workflow_enrollment vacuum at 2% dead rows
(autovacuum_vacuum_scale_factor = 0.02). crm_event_raw vacuums every 30 k
dead rows (scale_factor 0, threshold 30000): its claim walks a partial index
that rows leave, so a fixed trigger keeps the claim flat as the table grows
(measured: docs/bbd/bench/sawtooth/).

Each file must be exactly two statements: SET LOCAL lock_timeout (so a
vacuum holding the table fails the file in 10 s instead of hanging the
run), then one ALTER TABLE ... SET of its storage parameters on its one
table (one table owner per file). Each must carry its own rollback
statement. The DB-backed test runs the shipped SQL verbatim, and then the
documented rollback, against temp tables that shadow the real names for the
session (the 069 precedent), inside a transaction it rolls back.

  CRM_WEBHOOK_TEST_DSN=postgresql:///crm_webhook_test uv run pytest \\
      tests/database/test_autovacuum_scale_factor_migrations.py

Unset, the DB-backed test skips."""

import asyncio
import re
from pathlib import Path

import asyncpg
import pytest

from tests.crm.conftest import CRM_WEBHOOK_TEST_DSN as DSN

MIGRATIONS = Path(__file__).resolve().parents[2] / "app" / "database" / "migrations"

FILES = {
    "lead_call_tracker": "084_lead_call_tracker_autovacuum_scale_factor.sql",
    "crm_event_raw": "085_crm_event_raw_autovacuum_scale_factor.sql",
    "crm_workflow_enrollment": "086_crm_workflow_enrollment_autovacuum_scale_factor.sql",
}

# table -> (the SET list, the RESET list, reloptions after applying)
SETTINGS = {
    "lead_call_tracker": (
        "autovacuum_vacuum_scale_factor = 0.02",
        "autovacuum_vacuum_scale_factor",
        ["autovacuum_vacuum_scale_factor=0.02"],
    ),
    "crm_event_raw": (
        "autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 30000",
        "autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold",
        ["autovacuum_vacuum_scale_factor=0", "autovacuum_vacuum_threshold=30000"],
    ),
    "crm_workflow_enrollment": (
        "autovacuum_vacuum_scale_factor = 0.02",
        "autovacuum_vacuum_scale_factor",
        ["autovacuum_vacuum_scale_factor=0.02"],
    ),
}


def _statements(sql: str) -> list[str]:
    """Non-comment SQL, one entry per statement, whitespace-collapsed."""
    code = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    return [" ".join(s.split()) for s in code.split(";") if s.strip()]


def _rollback(sql: str) -> str:
    match = re.search(r"^--\s+(ALTER TABLE \w+ RESET \([\w, ]+\));$", sql, re.MULTILINE)
    assert match, "the header must carry the exact rollback statement"
    return match.group(1)


@pytest.mark.parametrize("table", sorted(FILES))
def test_file_sets_only_its_vacuum_trigger_on_its_own_table(table: str) -> None:
    sql = (MIGRATIONS / FILES[table]).read_text()
    set_list, reset_list, _ = SETTINGS[table]
    assert _statements(sql) == [
        "SET LOCAL lock_timeout = '10s'",
        f"ALTER TABLE {table} SET ({set_list})",
    ]
    assert _rollback(sql) == f"ALTER TABLE {table} RESET ({reset_list})"


async def _apply_and_roll_back() -> dict[str, tuple[object, object]]:
    conn = await asyncpg.connect(DSN)
    seen: dict[str, tuple[object, object]] = {}
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            for table, name in FILES.items():
                await conn.execute(f"CREATE TEMP TABLE {table} (id int)")
                sql = (MIGRATIONS / name).read_text()
                await conn.execute(sql)
                after = await conn.fetchval(
                    "SELECT reloptions FROM pg_class WHERE oid = $1::regclass",
                    table,
                )
                await conn.execute(_rollback(sql))
                reset = await conn.fetchval(
                    "SELECT reloptions FROM pg_class WHERE oid = $1::regclass",
                    table,
                )
                seen[table] = (after, reset)
        finally:
            await tx.rollback()
    finally:
        await conn.close()
    return seen


@pytest.mark.skipif(
    not DSN, reason="set CRM_WEBHOOK_TEST_DSN to run the DB-backed migration test"
)
def test_shipped_sql_sets_the_options_and_the_rollback_clears_them() -> None:
    seen = asyncio.run(_apply_and_roll_back())
    for table in FILES:
        after, reset = seen[table]
        assert sorted(after or []) == sorted(SETTINGS[table][2]), table
        assert reset is None, table
