#!/usr/bin/env python3
"""Clairvoyance migration runner.

Applies SQL files from app/database/migrations/ in lexicographic order, tracking
applied migrations in the `_clairvoyance_migrations` table (separate from the
nautilus `_migrations` table on the same database).

Subcommands:
    status              List applied vs pending migrations.
    up                  Apply all pending migrations (each in its own transaction).
    mark <files...>     Mark filenames as applied without running their SQL.
                        Used to bootstrap when the schema already exists.
    mark --up-to <f>    Mark every migration up to and including <f> as applied.

Connection info comes from .env (POSTGRES_USER/PASSWORD/HOST/PORT/DB). In prod,
POSTGRES_PASSWORD must already be decrypted before invoking this script.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

MIGRATIONS_DIR = (
    Path(__file__).resolve().parent.parent / "app" / "database" / "migrations"
)
TRACKING_TABLE = "_clairvoyance_migrations"

CREATE_TRACKING_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
    id          SERIAL PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Applied migrations that were later renamed on disk (pre-CI duplicate
# numbers, renumbered 2026-08-22 and 2026-08-25). The tracking table records
# applied migrations by filename, so every command reconciles these rows to
# the new names BEFORE computing pending — otherwise a renamed-but-applied
# file would look pending and its SQL would re-run. File contents are
# unchanged, so the stored checksums still match.
RENAMED_MIGRATIONS = {
    "026_link_call_execution_config_to_template.sql": (
        "046_link_call_execution_config_to_template.sql"
    ),
    "034_knowledge_base.sql": "047_knowledge_base.sql",
    # 052 duplicate (two 2026-08 merges collided; renumbered 2026-08-25).
    # The journey view merged second, so it takes the new number; the
    # drop-check migration keeps 052.
    "052_create_crm_journey_view.sql": "055_create_crm_journey_view.sql",
}


async def reconcile_renames(conn: asyncpg.Connection) -> None:
    for old, new in RENAMED_MIGRATIONS.items():
        await conn.execute(
            f"UPDATE {TRACKING_TABLE} SET filename = $2 "
            f"WHERE filename = $1 "
            f"AND NOT EXISTS (SELECT 1 FROM {TRACKING_TABLE} WHERE filename = $2)",
            old,
            new,
        )


def list_migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def get_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"],
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        min_size=1,
        max_size=2,
    )


async def fetch_applied(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch(f"SELECT filename, checksum FROM {TRACKING_TABLE};")
    return {r["filename"]: r["checksum"] for r in rows}


def _checksum_drift(
    files: list[Path], applied: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Return ``[(filename, stored_checksum, current_checksum), ...]`` for any
    already-applied migration whose on-disk file no longer matches the
    checksum recorded at apply time."""
    drift: list[tuple[str, str, str]] = []
    for f in files:
        stored = applied.get(f.name)
        if stored is None:
            continue
        current = file_checksum(f)
        if stored != current:
            drift.append((f.name, stored, current))
    return drift


async def cmd_status() -> int:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(CREATE_TRACKING_TABLE)
            await reconcile_renames(conn)
            applied = await fetch_applied(conn)
        files = list_migration_files()
        pending = [f for f in files if f.name not in applied]
        drift = _checksum_drift(files, applied)
        print(f"Applied: {len(applied)}    Pending: {len(pending)}\n")
        for f in files:
            tag = "applied" if f.name in applied else "PENDING"
            print(f"  {tag:8}  {f.name}")
        if drift:
            print(
                "\nWARNING: checksum drift detected on already-applied migrations:",
                file=sys.stderr,
            )
            for name, stored, current in drift:
                print(
                    f"  {name}: stored={stored[:12]}... disk={current[:12]}...",
                    file=sys.stderr,
                )
            print(
                "  Edit applied migration files only with extreme care; the "
                "SQL was already executed.",
                file=sys.stderr,
            )
        return 0
    finally:
        await pool.close()


async def cmd_up() -> int:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(CREATE_TRACKING_TABLE)
            await reconcile_renames(conn)
            applied = await fetch_applied(conn)
            files = list_migration_files()

            # Refuse to proceed if a previously-applied SQL file has been
            # edited since it was first run. The DB state is whatever the
            # original SQL produced; reconciling that with the new file
            # needs human attention, not a silent skip that lets envs drift.
            drift = _checksum_drift(files, applied)
            if drift:
                print(
                    "error: checksum drift on already-applied migrations:",
                    file=sys.stderr,
                )
                for name, stored, current in drift:
                    print(
                        f"  {name}: stored={stored[:12]}... disk={current[:12]}...",
                        file=sys.stderr,
                    )
                print(
                    "Revert the file or write a new migration; do not edit "
                    "applied SQL.",
                    file=sys.stderr,
                )
                return 1

            pending = [f for f in files if f.name not in applied]
            if not pending:
                print("Nothing to do.")
                return 0
            for f in pending:
                print(f"Applying {f.name}...", end=" ", flush=True)
                sql = f.read_text()
                checksum = file_checksum(f)
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        f"INSERT INTO {TRACKING_TABLE} (filename, checksum) "
                        "VALUES ($1, $2)",
                        f.name,
                        checksum,
                    )
                print("OK")
            print(f"\nApplied {len(pending)} migration(s).")
        return 0
    finally:
        await pool.close()


async def cmd_mark(filenames: list[str]) -> int:
    if not filenames:
        print("error: provide filenames or --up-to", file=sys.stderr)
        return 2
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(CREATE_TRACKING_TABLE)
            await reconcile_renames(conn)
            for name in filenames:
                path = MIGRATIONS_DIR / name
                if not path.exists():
                    print(
                        f"  skip {name} (not found in {MIGRATIONS_DIR})",
                        file=sys.stderr,
                    )
                    continue
                await conn.execute(
                    f"INSERT INTO {TRACKING_TABLE} (filename, checksum) "
                    "VALUES ($1, $2) ON CONFLICT (filename) DO NOTHING",
                    name,
                    file_checksum(path),
                )
                print(f"  marked {name}")
        return 0
    finally:
        await pool.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="List applied vs pending migrations")
    sub.add_parser("up", help="Apply all pending migrations")
    m = sub.add_parser(
        "mark",
        help="Mark migrations as applied without running their SQL",
    )
    m.add_argument("filenames", nargs="*", help="Filenames to mark as applied")
    m.add_argument(
        "--up-to",
        dest="up_to",
        help="Mark every migration up to and including this filename",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    if args.cmd == "status":
        return await cmd_status()
    if args.cmd == "up":
        return await cmd_up()
    if args.cmd == "mark":
        if args.up_to:
            target = args.up_to
            files = list_migration_files()
            names: list[str] = []
            for f in files:
                names.append(f.name)
                if f.name == target:
                    return await cmd_mark(names)
            print(
                f"error: --up-to filename not found in migrations dir: {target}",
                file=sys.stderr,
            )
            return 1
        return await cmd_mark(args.filenames)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
