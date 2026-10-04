#!/usr/bin/env python3
"""Seed the LOCAL clairvoyance database from PROD — reads only, ever.

Copies the config plane in full (merchants, templates, call configs, numbers,
widget configs, blacklist cap, credentials, KB metadata) plus a bounded recent
slice of traffic (leads, chat sessions + their messages/metrics) so the console
has realistic data without ever touching prod again.

Safety model:
  * The prod session is forced READ ONLY as its first statement — even with rw
    credentials no write can occur.
  * Prod connection info comes from .env.db.prod (no password inside); the
    password is taken from the PROD_PGPASSWORD env var only, never a file,
    never printed.
  * The local target is whatever .env points at — the script REFUSES to run
    if .env does not match the .env.db.local profile (belt against seeding
    prod by mistake).
  * Copied tables are truncated locally first, so the script is idempotent.

Usage:
    PROD_PGPASSWORD=... uv run python scripts/seed_local_from_prod.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

LEAD_DAYS = 30
LEAD_CAP = 25_000
CHAT_DAYS = 30
CHAT_CAP = 5_000
BLACKLIST_CAP = 5_000

# (table, bounded-select builder or None for full copy), in FK-safe load order.
FULL_TABLES = [
    "merchants",
    "template",
    "call_execution_config",
    "outbound_number",
    "widget_config",
    "credentials",
    "knowledge_base",
    "kb_document",
]


def env_profile(path: Path) -> dict[str, str]:
    vals = {k: v or "" for k, v in dotenv_values(path).items()}
    return vals


async def connect_prod() -> asyncpg.Connection:
    prof = env_profile(ROOT / ".env.db.prod")
    pw = os.environ.get("PROD_PGPASSWORD")
    if not pw:
        sys.exit("error: PROD_PGPASSWORD env var not set")
    conn = await asyncpg.connect(
        host=prof["POSTGRES_HOST"],
        port=int(prof.get("POSTGRES_PORT", "5432")),
        database=prof["POSTGRES_DB"],
        user=prof["POSTGRES_USER"],
        password=pw,
        timeout=30,
    )
    await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;")
    return conn


async def connect_local() -> asyncpg.Connection:
    env = env_profile(ROOT / ".env")
    local_prof = env_profile(ROOT / ".env.db.local")
    for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER"):
        if env.get(key) != local_prof.get(key):
            sys.exit(
                "error: .env does not match .env.db.local — run "
                "`scripts/db_env_switch.py local` first; refusing to seed a "
                "non-local target."
            )
    return await asyncpg.connect(
        host=env["POSTGRES_HOST"],
        port=int(env.get("POSTGRES_PORT", "5432")),
        database=env["POSTGRES_DB"],
        user=env["POSTGRES_USER"],
        password=env.get("POSTGRES_PASSWORD") or None,
    )


async def table_columns(conn: asyncpg.Connection, table: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position",
        table,
    )
    return [r["column_name"] for r in rows]


async def copy_rows(
    prod: asyncpg.Connection,
    local: asyncpg.Connection,
    table: str,
    select_sql: str | None = None,
    args: list | None = None,
) -> int:
    prod_cols = await table_columns(prod, table)
    local_cols = await table_columns(local, table)
    if not prod_cols:
        print(f"  {table:24} SKIP (absent in prod)")
        return 0
    cols = [c for c in prod_cols if c in local_cols]
    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = (
        select_sql.format(cols=col_list)
        if select_sql
        else f'SELECT {col_list} FROM "{table}"'
    )
    rows = await prod.fetch(sql, *(args or []))
    await local.execute(f'TRUNCATE "{table}" CASCADE')
    if rows:
        records = [tuple(r[c] for c in cols) for r in rows]
        await local.copy_records_to_table(table, records=records, columns=cols)
    print(f"  {table:24} {len(rows):>6} rows")
    return len(rows)


async def fix_sequences(local: asyncpg.Connection) -> None:
    seqs = await local.fetch("""
        SELECT c.table_name, c.column_name,
               pg_get_serial_sequence(c.table_name, c.column_name) AS seq
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
          AND c.column_default LIKE 'nextval%'
        """)
    for r in seqs:
        if r["seq"]:
            await local.execute(
                f'SELECT setval($1, COALESCE((SELECT MAX("{r["column_name"]}") '
                f'FROM "{r["table_name"]}"), 0) + 1, false)',
                r["seq"],
            )


async def main() -> int:
    prod = await connect_prod()
    local = await connect_local()
    total = 0
    try:
        # FK checks off for load order freedom (local superuser session only).
        await local.execute("SET session_replication_role = replica;")

        print("config plane (full copies):")
        for table in FULL_TABLES:
            total += await copy_rows(prod, local, table)

        print("bounded slices:")
        total += await copy_rows(
            prod,
            local,
            "blacklisted_numbers",
            "SELECT {cols} FROM blacklisted_numbers ORDER BY created_at DESC LIMIT $1",
            [BLACKLIST_CAP],
        )
        total += await copy_rows(
            prod,
            local,
            "lead_call_tracker",
            "SELECT {cols} FROM lead_call_tracker "
            f"WHERE created_at >= now() - interval '{LEAD_DAYS} days' "
            "ORDER BY created_at DESC LIMIT $1",
            [LEAD_CAP],
        )
        total += await copy_rows(
            prod,
            local,
            "chat_session",
            "SELECT {cols} FROM chat_session "
            f"WHERE created_at >= now() - interval '{CHAT_DAYS} days' "
            "ORDER BY created_at DESC LIMIT $1",
            [CHAT_CAP],
        )
        session_ids = [
            r["id"] for r in await local.fetch("SELECT id FROM chat_session")
        ]
        if session_ids:
            total += await copy_rows(
                prod,
                local,
                "chat_message",
                "SELECT {cols} FROM chat_message WHERE session_id = ANY($1)",
                [session_ids],
            )
            total += await copy_rows(
                prod,
                local,
                "chat_turn_metrics",
                "SELECT {cols} FROM chat_turn_metrics WHERE session_id = ANY($1)",
                [session_ids],
            )
        # kb_chunk intentionally NOT copied: pgvector embeddings are heavy and
        # retrieval needs the same embedding provider anyway. KB lists/docs are
        # real; the retrieval-test panel returns empty until docs are re-synced.

        await fix_sequences(local)
        await local.execute("SET session_replication_role = DEFAULT;")
        print(f"\nseeded {total} rows into the local database.")
        return 0
    finally:
        await prod.close()
        await local.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
