#!/usr/bin/env python3
"""CI guard for the app/crm module boundaries (ADR 0001 amendment).

One DB role and no schema grants, so the boundary is enforced HERE, at
PR time — earlier than a grant would fail:

  1. TABLE OWNERSHIP — a quoted crm_*/platform_* table literal may appear
     only in its owning module's db/ package (+ migrations, tests, scripts).
  2. SQL CONFINEMENT — SQL statements inside app/crm live only in
     db/queries*.py files.
  3. DRIVER CONFINEMENT — within app/crm, `import asyncpg` is legal only
     in shared/db.py and */db/ packages.
  4. IMPORT DIRECTION — app/crm never imports app.ai; buddy (app/ai,
     app/api) imports only app.crm.<module>.contracts; the data layer
     (app/database) imports neither app.ai nor app.crm (pre-existing
     legacy exceptions allowlisted, closed to additions); cross-module
     inside app/crm goes through contracts.py (or shared/) only.
  5. HANDLE DISCIPLINE — logic files pass the DbTxn handle, they never
     call query methods on it (that is db/accessor's job).
  6. MAP COMPLETENESS — every crm_*/platform_* CREATE TABLE in the
     migrations has an owner registered below.
  7. ONE BOUNDARY DOOR — raw crm_transaction()/`async with transaction()`
     is legal only inside shared/db.py; logic enters boundaries via
     atomically(...) exclusively.
  8. ATOMIC NAMING — the function passed to atomically() is named
     *_in_txn: the suffix declares "I am a boundary's body".
  9. ATOMIC DOCSTRING — every *_in_txn body opens with "ATOMIC: <what
     shares fate> — <the law>"; grep ATOMIC: is the atom inventory.
  10. HANDLES STAY DOWN — connection()/crm_connection never appears in
     logic; accessors self-scope single statements and batch loops. A
     logic file touches a handle ONLY as an _in_txn body's txn param.
  11. ADAPTER CONFINEMENT — app.crm.connectivity.providers is imported
     only by app/crm/connectivity/send.py and inside providers/ itself;
     any other import reaches a provider around the send() door's checks.

New table? Add it to TABLE_OWNERS with its owning module. New violation
class? This script is the place — the boundary is code, so the check is
code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- the ownership map (rule 6 keeps it complete) -------------------------
TABLE_OWNERS = {
    "crm_customer": "identity",
    "platform_identity": "platform",
    "crm_event_raw": "record",
    "crm_journey_event": "record",
    "crm_message": "connectivity",
    "crm_workflow": "outreach",
    "crm_workflow_enrollment": "outreach",
    "crm_connector_installation": "connectivity",
    "crm_channel_binding": "connectivity",
}

# Pre-existing legacy inversions, allowlisted and CLOSED to additions
# (the template.py DTO->engine scar predates the CRM; fixed with the
# buddy restructure, not silently grown).
LEGACY_IMPORT_ALLOWLIST = {
    "app/database/accessor/breeze_buddy/template.py",
    "app/database/decoder/breeze_buddy/template.py",
}

SQL_STMT = re.compile(
    r"\b(SELECT\s+[\w\s,.*]+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b",
    re.IGNORECASE,
)
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(app[\w.]*)", re.MULTILINE)
# Nesting counts: a raw ``txn.transaction()`` reads like a new transaction
# but is a SAVEPOINT, and logic has its own door for that — savepoint(txn).
HANDLE_CALL = re.compile(
    r"\b(?:txn|conn)\.(?:execute|fetch|fetchrow|fetchval|transaction)\("
)
CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:crm|platform)_\w+)", re.IGNORECASE
)


def crm_module_of(rp: str) -> str | None:
    """'app/crm/identity/...' -> 'identity'; None for crm-root files."""
    parts = rp.split("/")
    if len(parts) >= 4 and parts[:2] == ["app", "crm"]:
        return parts[2]
    return None


def check(root: Path = ROOT) -> list[str]:
    """Scan the tree and return every boundary violation as a message."""
    errors: list[str] = []
    py_files = [
        p
        for p in root.glob("app/**/*.py")
        if ".venv" not in p.parts and "node_modules" not in p.parts
    ]

    for path in py_files:
        rp = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        in_crm = rp.startswith("app/crm/")
        module = crm_module_of(rp)
        in_db_pkg = in_crm and "/db/" in rp
        is_shared_db = rp == "app/crm/shared/db.py"

        # 1. table ownership — quoted literals only in the owner's db/
        for table, owner in TABLE_OWNERS.items():
            if re.search(rf"[\"']{table}[\"']", text):
                allowed = rp.startswith(f"app/crm/{owner}/db/")
                if not allowed:
                    errors.append(
                        f"{rp}: table literal '{table}' outside its owner "
                        f"(app/crm/{owner}/db/) — go through {owner}'s contracts"
                    )

        # 2. SQL confinement inside app/crm
        if in_crm and not re.search(r"/db/queries[\w]*\.py$", rp):
            if SQL_STMT.search(text):
                errors.append(
                    f"{rp}: SQL statement outside db/queries — SQL builders "
                    f"live only in the owning module's db/queries*.py"
                )

        # 3. driver confinement inside app/crm
        if in_crm and not (in_db_pkg or is_shared_db):
            if re.search(r"^\s*import asyncpg|^\s*from asyncpg", text, re.MULTILINE):
                errors.append(
                    f"{rp}: asyncpg import outside db//shared-db — logic uses "
                    f"DbTxn/UniqueViolation from its module's db door"
                )

        # 5. handle discipline: crm logic passes handles, never queries them
        if in_crm and not (in_db_pkg or is_shared_db):
            if HANDLE_CALL.search(text):
                errors.append(
                    f"{rp}: driver method called on a txn/conn handle outside "
                    f"db/ — logic opens boundaries and passes handles; only "
                    f"accessors query them, and nesting goes through "
                    f"savepoint(txn), never txn.transaction()"
                )

        # 4. import direction
        for match in IMPORT_RE.finditer(text):
            target = match.group(1)
            if in_crm and (target == "app.ai" or target.startswith("app.ai.")):
                errors.append(f"{rp}: app/crm must never import app.ai ({target})")
            if module and module != "shared" and target.startswith("app.crm."):
                t_parts = target.split(".")
                t_mod = t_parts[2] if len(t_parts) > 2 else None
                if (
                    t_mod
                    and t_mod not in (module, "shared", "auth", "api")
                    and not target.startswith(f"app.crm.{t_mod}.contracts")
                ):
                    errors.append(
                        f"{rp}: cross-module import bypasses contracts.py "
                        f"({target}) — modules import each other's contracts only"
                    )
            if rp.startswith(("app/ai/", "app/api/")) and target.startswith("app.crm."):
                if not re.fullmatch(r"app\.crm\.\w+\.contracts", target):
                    errors.append(
                        f"{rp}: buddy code may import only "
                        f"app.crm.<module>.contracts ({target})"
                    )
            if rp.startswith("app/database/") and rp not in LEGACY_IMPORT_ALLOWLIST:
                if target.startswith(("app.ai", "app.crm")):
                    errors.append(
                        f"{rp}: the data layer imports neither app.ai nor "
                        f"app.crm ({target}) — use the hook-registry pattern"
                    )
            # 11. adapter confinement — providers/ sits behind the send() door
            if target.startswith("app.crm.connectivity.providers"):
                if rp != "app/crm/connectivity/send.py" and not rp.startswith(
                    "app/crm/connectivity/providers/"
                ):
                    errors.append(
                        f"{rp}: adapter import outside send.py ({target}) — "
                        f"providers/ is reached only through the send() door"
                    )

        # 7-9. the atomic grammar
        if in_crm and not is_shared_db:
            if re.search(r"\bcrm_transaction\b|async with transaction\(", text):
                errors.append(
                    f"{rp}: raw transaction outside shared/db — logic enters "
                    f"boundaries via atomically(_x_in_txn, ...) only"
                )
            for m in re.finditer(r"await atomically\(\s*(\w+)", text):
                if not m.group(1).endswith("_in_txn"):
                    errors.append(
                        f"{rp}: atomically() callee '{m.group(1)}' must be "
                        f"named *_in_txn — the suffix declares the boundary body"
                    )
            if not in_db_pkg and re.search(
                r"\bcrm_connection\b|async with connection\(", text
            ):
                errors.append(
                    f"{rp}: connection handle in logic — accessors self-scope "
                    f"single statements; logic holds handles only inside "
                    f"_in_txn bodies"
                )
            for m in re.finditer(r"async def (\w+_in_txn)\(", text):
                tail = text[m.end() : m.end() + 400]
                if "ATOMIC:" not in tail:
                    errors.append(
                        f"{rp}: {m.group(1)} lacks an 'ATOMIC: <what> — <law>' "
                        f"docstring — every atom states what shares fate and why"
                    )

        # crm-root files hold no SQL and no table literals (surface plumbing)
        if in_crm and module is None and SQL_STMT.search(text):
            errors.append(f"{rp}: SQL in a crm-root file — root is plumbing only")

    # 6. ownership-map completeness
    for sql in sorted(root.glob("app/database/migrations/*.sql")):
        for match in CREATE_TABLE.finditer(sql.read_text(errors="replace")):
            table = match.group(1).lower()
            if table not in TABLE_OWNERS:
                errors.append(
                    f"{sql.relative_to(root).as_posix()}: table '{table}' has no owner — add it to "
                    f"TABLE_OWNERS in scripts/check_crm_boundaries.py"
                )

    return errors


def main() -> int:
    """CLI entry: print violations and exit nonzero when any exist."""
    errors = check()
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1
    print(
        "OK: crm boundaries clean "
        "(ownership, SQL, driver, imports, handles, adapters)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
