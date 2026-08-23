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

Never rename or edit an applied migration to satisfy this check — the
tracking table records filenames, and a renamed applied file re-runs its
SQL everywhere. (The one historical renumbering of the pre-CI 026/034
duplicates is reconciled by RENAMED_MIGRATIONS in migrate.py.)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MIGRATIONS_DIR = (
    Path(__file__).resolve().parent.parent / "app" / "database" / "migrations"
)
PATTERN = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def main() -> int:
    errors: list[str] = []
    numbers: dict[int, list[str]] = {}

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = PATTERN.match(path.name)
        if not match:
            errors.append(f"bad filename (want NNN_snake_case.sql): {path.name}")
            continue
        numbers.setdefault(int(match.group(1)), []).append(path.name)

    for number, names in sorted(numbers.items()):
        if len(names) > 1:
            errors.append(
                f"duplicate migration number {number:03d}: {', '.join(names)}"
            )

    if numbers:
        for missing in range(min(numbers), max(numbers) + 1):
            if missing not in numbers:
                errors.append(f"gap in sequence: {missing:03d} is missing")

    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1

    total = sum(len(names) for names in numbers.values())
    print(f"OK: {total} migrations — no duplicates, no gaps, names clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
