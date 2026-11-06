#!/usr/bin/env python3
"""CI guard for app/database/migrations/.

scripts/migrate.py applies files in lexicographic order, so two files
sharing one numeric prefix apply in an order nobody chose — and a gap
usually means a typo'd number (064 for 046). This check fails the PR
before either can merge.

Rules:
  1. filenames match NNN_snake_case.sql
  2. no two files share a numeric prefix
  3. the number sequence has no gaps

``--base <ref>`` (CI, pull_request events) additionally unions the
working tree's migration names with the CURRENT base branch's before
applying rules 2–3. Without it, two open PRs can each add the same
number, each pass CI against its own tree, and collide only after both
merge — exactly how the 052 duplicate landed on release (2026-08).
Checking against the base at CI time closes that window to the
stale-approval race, which only branch protection ("require branches to
be up to date" / a merge queue) can close completely.

Names the PR deliberately renamed AWAY from (the sanctioned renames in
``RENAMED_MIGRATIONS``, scripts/migrate.py) are excluded from the base
set — otherwise the rename that FIXES a duplicate would itself read as
one. Never rename or edit an applied migration outside that registry —
the tracking table records filenames, and a renamed applied file
re-runs its SQL everywhere.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = SCRIPTS_DIR.parent / "app" / "database" / "migrations"
MIGRATIONS_REL = "app/database/migrations"
PATTERN = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def collect_errors(names: list[str]) -> list[str]:
    """Rules 1–3 over a list of migration filenames (pure — unit-tested)."""
    errors: list[str] = []
    numbers: dict[int, list[str]] = {}

    for name in sorted(names):
        match = PATTERN.match(name)
        if not match:
            errors.append(f"bad filename (want NNN_snake_case.sql): {name}")
            continue
        numbers.setdefault(int(match.group(1)), []).append(name)

    for number, file_names in sorted(numbers.items()):
        if len(file_names) > 1:
            errors.append(
                f"duplicate migration number {number:03d}: {', '.join(file_names)}"
            )

    if numbers:
        for missing in range(min(numbers), max(numbers) + 1):
            if missing not in numbers:
                errors.append(f"gap in sequence: {missing:03d} is missing")

    return errors


def sanctioned_renames() -> dict[str, str]:
    """The sanctioned-rename registry (old → new), loaded from
    scripts/migrate.py so there is exactly one registry."""
    spec = importlib.util.spec_from_file_location(
        "_migrate_for_check", SCRIPTS_DIR / "migrate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.RENAMED_MIGRATIONS)


def sanctioned_rename_sources() -> set[str]:
    """Old names of the sanctioned renames (RENAMED_MIGRATIONS keys)."""
    return set(sanctioned_renames())


def base_migration_names(base_ref: str) -> set[str]:
    """Migration filenames on ``base_ref`` (fails loud — a union check
    that silently saw no base files would pass exactly when it matters)."""
    result = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{base_ref}:{MIGRATIONS_REL}"],
        capture_output=True,
        text=True,
        cwd=SCRIPTS_DIR.parent,
    )
    if result.returncode != 0:
        print(
            f"error: cannot list {MIGRATIONS_REL} at {base_ref!r}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(2)
    return {name for name in result.stdout.splitlines() if name.endswith(".sql")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        metavar="REF",
        help=(
            "git ref of the merge target (e.g. origin/release): rules run "
            "over the UNION of the working tree's migrations and the base's, "
            "so a number collision between two open PRs fails at PR time"
        ),
    )
    parser.add_argument(
        "--print-sanctioned",
        action="store_true",
        help=(
            "print every filename involved in a sanctioned rename (old AND "
            "new names, one per line) — the immutability guard in "
            "pr-build-check.yml derives its exemption filter from this, so "
            "the RENAMED_MIGRATIONS registry and CI can never drift"
        ),
    )
    args = parser.parse_args(argv)

    if args.print_sanctioned:
        for old, new in sorted(sanctioned_renames().items()):
            print(old)
            print(new)
        return 0

    names = {path.name for path in MIGRATIONS_DIR.glob("*.sql")}
    scope = "migrations"
    if args.base:
        names |= base_migration_names(args.base) - sanctioned_rename_sources()
        scope = f"migrations (unioned with {args.base})"

    errors = collect_errors(sorted(names))
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1

    print(f"OK: {len(names)} {scope} — no duplicates, no gaps, names clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
