#!/usr/bin/env python3
"""Canon-conformance diff (A3): live database vs the sealed schema.

Canon 07-wiring: "a canon-conformance check introspects the live database
and diffs it against the sealed schema: MISSING is pending, EXTRA or
MISMATCHED fails."

  MISSING     a sealed table/column not in the DB  -> pending, exit 0
  EXTRA       a governed table/column not sealed   -> FAIL
  MISMATCHED  type or nullability disagrees        -> FAIL

Only the crm_/platform_ namespace is inspected (sealed.GOVERNED_PREFIXES);
pre-CPaaS tables are outside canon and never reported.

Usage:
    uv run python scripts/check_canon.py

Connects with the standard POSTGRES_* env vars. Exits 2 (not 1) if no
database is reachable, so "couldn't check" is never mistaken for "clean".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from app.crm.shared.sealed import (  # noqa: E402
    PENDING_CANON_IDS,
    SEALED,
    divergences,
    is_governed,
)

# Base tables only. information_schema.columns also returns VIEWS, and the
# canon has one (V01, the journey view A12 builds as crm_journey_event).
# Without this join that view lands as "EXTRA table" and fails the build —
# the gate would block a teammate for building exactly what canon asked for.
# Sealing view *shapes* is a separate question; see sealed.py.
COLUMNS_SQL = """
    SELECT c.table_name, c.column_name, c.data_type, c.is_nullable
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema
     AND t.table_name = c.table_name
    WHERE c.table_schema = 'public'
      AND t.table_type = 'BASE TABLE'
    ORDER BY c.table_name, c.ordinal_position
"""


async def _introspect() -> dict[str, dict[str, tuple[str, bool]]]:
    """{table: {column: (data_type, not_null)}} for governed tables only."""
    import asyncpg

    from app.core.config.static import (
        POSTGRES_DB,
        POSTGRES_HOST,
        POSTGRES_PASSWORD,
        POSTGRES_PORT,
        POSTGRES_USER,
    )

    conn = await asyncpg.connect(
        host=POSTGRES_HOST,
        port=int(POSTGRES_PORT or 5432),
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        database=POSTGRES_DB,
    )
    try:
        rows = await conn.fetch(COLUMNS_SQL)
    finally:
        await conn.close()

    live: dict[str, dict[str, tuple[str, bool]]] = {}
    for row in rows:
        table = row["table_name"]
        if not is_governed(table):
            continue
        live.setdefault(table, {})[row["column_name"]] = (
            row["data_type"],
            row["is_nullable"] == "NO",
        )
    return live


def diff(live: dict[str, dict[str, tuple[str, bool]]]) -> tuple[list[str], list[str]]:
    """Return (failures, pending). Failures are EXTRA/MISMATCHED."""
    failures: list[str] = []
    pending: list[str] = []

    for name, table in sorted(SEALED.items()):
        if name not in live:
            pending.append(f"MISSING table {name} ({table.canon_id})")
            continue

        live_columns = live[name]
        for column in table.columns:
            actual = live_columns.get(column.name)
            if actual is None:
                pending.append(f"MISSING column {name}.{column.name}")
                continue
            actual_type, actual_not_null = actual
            if actual_type != column.type:
                failures.append(
                    f"MISMATCHED {name}.{column.name}: sealed {column.type!r}, "
                    f"live {actual_type!r}"
                )
            if actual_not_null != column.not_null:
                failures.append(
                    f"MISMATCHED {name}.{column.name}: sealed "
                    f"not_null={column.not_null}, live not_null={actual_not_null}"
                )

        for column_name in sorted(set(live_columns) - set(table.column_map)):
            failures.append(f"EXTRA column {name}.{column_name} is not sealed")

    for name in sorted(set(live) - set(SEALED)):
        failures.append(f"EXTRA table {name} is in the canon namespace but not sealed")

    return failures, pending


async def main() -> int:
    try:
        live = await _introspect()
    except Exception as exc:  # noqa: BLE001 - any connection failure
        print(f"error: cannot reach the database to check conformance: {exc}")
        return 2

    failures, pending = diff(live)

    for line in pending:
        print(f"pending: {line}")
    for line in failures:
        print(f"error: {line}", file=sys.stderr)

    for built, canonical in divergences():
        print(f"note: built as {built}, canon specifies {canonical}")

    if failures:
        print(f"\nFAIL: {len(failures)} conformance error(s).", file=sys.stderr)
        return 1

    print(
        f"\nOK: {len(SEALED)} sealed table(s) conform; "
        f"{len(pending)} pending; {len(PENDING_CANON_IDS)} canon table(s) "
        "not yet sealed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
