#!/usr/bin/env python3
"""Tenancy CI (A3): enforce canon's tenancy laws on the crm namespace.

Canon 07-wiring states them as:

  1. "any row -> merchant -> reseller is one total join; no crm table
     stores a reseller."
  2. "merchant_id is globally unique (registry PK); merchants.reseller_id
     NOT NULL"
  3. "root-table query without a merchant_id predicate fails the build"
  4. "child tables must be entered through a scoped parent"

Laws 1, 2 and the schema half of 3 are checked against the live database;
the query half of 3 is a static read of app/crm/**/queries.py. Law 4 needs
a declared parent/child graph, which no canon table has yet (crm_customer
is a root) — it is wired as a no-op until the first child table lands,
rather than silently omitted.

Usage:
    uv run python scripts/check_tenancy.py

Exits 2 if the database is unreachable, so "couldn't check" never reads as
"clean".
"""

from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
load_dotenv()

from app.crm.shared.sealed import SEALED, is_governed  # noqa: E402

CRM_PACKAGE = REPO_ROOT / "app" / "crm"

UNIQUE_INDEX_SQL = """
    SELECT
        t.relname  AS table_name,
        i.relname  AS index_name,
        a.attname  AS column_name,
        k.ord      AS position
    FROM pg_class t
    JOIN pg_index ix       ON t.oid = ix.indrelid
    JOIN pg_class i        ON i.oid = ix.indexrelid
    JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a    ON a.attrelid = t.oid AND a.attnum = k.attnum
    WHERE ix.indisunique AND t.relkind = 'r'
    ORDER BY t.relname, i.relname, k.ord
"""

COLUMNS_SQL = """
    SELECT table_name, column_name, is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public'
"""


async def _fetch_schema() -> (
    tuple[dict[str, dict[str, bool]], dict[tuple[str, str], list[str]]]
):
    """Introspect governed tables: ({table: {column: not_null}}, unique indexes)."""
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
        column_rows = await conn.fetch(COLUMNS_SQL)
        index_rows = await conn.fetch(UNIQUE_INDEX_SQL)
    finally:
        await conn.close()

    columns: dict[str, dict[str, bool]] = {}
    for row in column_rows:
        if is_governed(row["table_name"]):
            columns.setdefault(row["table_name"], {})[row["column_name"]] = (
                row["is_nullable"] == "NO"
            )

    index_columns: dict[tuple[str, str], list[str]] = {}
    for row in index_rows:
        if is_governed(row["table_name"]):
            index_columns.setdefault((row["table_name"], row["index_name"]), []).append(
                row["column_name"]
            )

    return columns, index_columns


def evaluate_schema_laws(
    columns: dict[str, dict[str, bool]],
    index_columns: dict[tuple[str, str], list[str]],
) -> list[str]:
    """Laws 1, 2 and the schema half of 3, over introspected shape.

    Pure so the rules can be tested without a database.
    """
    violations: list[str] = []

    for table_name, table_columns in sorted(columns.items()):
        sealed = SEALED.get(table_name)

        # Law 1 — no crm table stores a reseller.
        for column_name in table_columns:
            if "reseller" in column_name:
                violations.append(
                    f"law 1: {table_name}.{column_name} stores a reseller; "
                    "the merchant -> reseller join is the only path"
                )

        # Law 2 — a tenant-scoped table carries merchant_id NOT NULL.
        # platform_* tables are deliberately cross-merchant and exempt.
        if sealed is not None and sealed.tenant_scoped:
            if not table_columns.get("merchant_id", False):
                violations.append(
                    f"law 2: {table_name} is tenant-scoped but has no "
                    "merchant_id NOT NULL"
                )

    # Law 3 (schema half) — merchant_id leads every unique index on a
    # tenant-scoped table, so a uniqueness collision can never cross tenants.
    for (table_name, index_name), cols in sorted(index_columns.items()):
        sealed = SEALED.get(table_name)
        if sealed is None or not sealed.tenant_scoped:
            continue
        if cols == ["id"]:  # the primary key is exempt by construction
            continue
        if cols[0] != "merchant_id":
            violations.append(
                f"law 3: unique index {index_name} on {table_name} leads with "
                f"{cols[0]!r}, not merchant_id — uniqueness would span tenants"
            )

    return violations


def _display_path(path: Path) -> Path:
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:  # a tmp dir in tests
        return path


_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line and /* */ block comments.

    A predicate inside a comment is not a predicate: without this,
    ``SELECT ... FROM crm_customer -- merchant_id = $2`` passes law 3.
    """
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """``NAME = "literal"`` string assignments, file-wide.

    Two kinds matter. Module constants name the table
    (``CRM_CUSTOMER_TABLE = "crm_customer"``) — resolving them is what lets
    an f-string body mention a real table at all. Function-local literals
    carry predicates (``where = "merchant_id = $1 AND status = 'active'"``,
    then interpolated as ``WHERE {where}``); without them a scoped query
    reads as unscoped.

    A name assigned more than once with differing text is dropped rather
    than guessed at — substituting the wrong one could mask a real
    violation, and a heuristic checker should not invent certainty.
    """
    seen: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                seen.setdefault(target.id, set()).add(node.value.value)
    return {name: next(iter(v)) for name, v in seen.items() if len(v) == 1}


def _render_fstring(node: ast.JoinedStr, constants: dict[str, str]) -> str:
    """Flatten an f-string, substituting known module-level constants.

    Interpolations that cannot be resolved become a placeholder rather than
    vanishing, so ``FROM {some_var}`` never accidentally reads as ``FROM``
    followed by whatever text comes next.
    """
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            inner = value.value
            if isinstance(inner, ast.Name) and inner.id in constants:
                parts.append(constants[inner.id])
            else:
                parts.append("{?}")
    return "".join(parts)


def _sql_literals(tree: ast.Module) -> list[tuple[int, str]]:
    """Every (lineno, sql) candidate in a module: plain strings AND f-strings.

    Without the f-string half this check is inert on the real codebase —
    every builder writes ``FROM {CRM_CUSTOMER_TABLE}``, so the literal text
    of a root table never appears in an ``ast.Constant`` and nothing is ever
    examined.
    """
    constants = _module_string_constants(tree)

    joined: list[tuple[int, str]] = []
    consumed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            joined.append((node.lineno, _render_fstring(node, constants)))
            # The fragments of an f-string are Constants; don't re-read them
            # as standalone literals.
            for child in ast.walk(node):
                consumed.add(id(child))

    plain = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in consumed
    ]
    return plain + joined


def _root_table_names() -> set[str]:
    return {table.name for table in SEALED.values() if table.tenant_scoped}


def check_query_predicates(package: Path = CRM_PACKAGE) -> list[str]:
    """Law 3 (query half) — a read of a root table must filter merchant_id.

    Reads string literals in app/crm/**/*.py, so it sees the SQL the
    builders actually assemble rather than trusting a naming convention.

    A literal only counts if it *starts* with a SQL verb. Without that
    anchor the check matches prose: resolve.py's docstring says "no other
    INSERT INTO crm_customer may exist anywhere", which names a root table
    and carries no predicate, and would be reported as a violation.

    Comments are stripped before anything is matched. Without that, a
    trailing ``-- merchant_id = $2`` satisfies the predicate check and the
    law is bypassed by a comment.

    Deliberately conservative in two more ways: INSERT is skipped (it
    carries merchant_id as a column, not a predicate), and one merchant_id
    predicate satisfies the whole statement. That second point is a known
    limitation — a join across two root tables that scopes only one of them
    passes. Closing it needs alias-aware SQL parsing rather than a regex;
    until then this catches the unscoped-read case it is aimed at.
    """
    violations: list[str] = []
    roots = _root_table_names()
    if not roots:
        return violations

    # Anchored at the start so only real statements are considered.
    sql_start = re.compile(r"^\s*(SELECT|UPDATE|DELETE|WITH)\b", re.IGNORECASE)
    table_pattern = re.compile(
        r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(" + "|".join(sorted(roots)) + r")\b",
        re.IGNORECASE,
    )
    predicate_pattern = re.compile(r"merchant_id\s*=", re.IGNORECASE)

    for path in sorted(package.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - malformed source
            violations.append(f"could not parse {path}: {exc}")
            continue

        for lineno, raw in _sql_literals(tree):
            sql = _strip_sql_comments(raw)
            if not sql_start.match(sql) or not table_pattern.search(sql):
                continue
            if not predicate_pattern.search(sql):
                rel = _display_path(path)
                violations.append(
                    f"law 3: {rel}:{lineno} reads a root table without "
                    "a merchant_id predicate"
                )
    return violations


def check_child_tables() -> list[str]:
    """Law 4 — child tables must be entered through a scoped parent.

    No canon child table is sealed yet (crm_customer is a root), so there is
    nothing to walk. Wired in now so law 4 is enforced the day the first
    child lands rather than remembered later.
    """
    return []


async def main() -> int:
    violations = check_query_predicates() + check_child_tables()

    try:
        columns, index_columns = await _fetch_schema()
        violations += evaluate_schema_laws(columns, index_columns)
    except Exception as exc:  # noqa: BLE001 - any connection failure
        print(f"error: cannot reach the database to check tenancy: {exc}")
        return 2

    for violation in violations:
        print(f"error: {violation}", file=sys.stderr)

    if violations:
        print(f"\nFAIL: {len(violations)} tenancy violation(s).", file=sys.stderr)
        return 1

    print(f"OK: tenancy laws hold across {len(SEALED)} sealed table(s).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
