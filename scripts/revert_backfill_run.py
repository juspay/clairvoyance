"""Revert a backfill run: delete exactly the rows a journal recorded.

Reads a journal JSONL produced by backfill_merchant_templates.py and deletes
the created rows — calling config first, then template (FK order). Name-based
resolution falls back to the shared row the moment the copy is gone.

Safety:
  - dry-run by default: shows per-merchant what would be deleted and how many
    leads already reference each copy.
  - a copy referenced by leads (merchant already took calls on it) is NEVER
    deleted — it is reported for a manual decision instead. The DB FK
    (lead_call_tracker.template_id → template.id) enforces this even if the
    check races.
  - only deletes ids listed in the journal; touches nothing else.

Usage:
  uv run --with asyncpg python3 scripts/revert_backfill_run.py \
      scripts/backfill_runs/journal-order-confirmation-wave-1-*.jsonl          # dry run
  ... --apply                                                                  # delete
  ... --merchants shop-a.myshopify.com,shop-b.myshopify.com                    # subset
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("journal", help="journal .jsonl from a backfill run")
    ap.add_argument(
        "--apply", action="store_true", help="perform deletes (default: dry run)"
    )
    ap.add_argument(
        "--merchants", default=None, help="comma-separated subset to revert"
    )
    args = ap.parse_args()

    for var in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        if not os.environ.get(var):
            print(f"error: {var} not set", file=sys.stderr)
            return 2

    entries = []
    for line in Path(args.journal).read_text().splitlines():
        rec = json.loads(line)
        if rec.get("event") == "created":
            entries.append(rec)
    if args.merchants:
        wanted = {m.strip() for m in args.merchants.split(",")}
        entries = [e for e in entries if e["merchant_id"] in wanted]
    if not entries:
        print("nothing to revert (no matching 'created' entries in journal)")
        return 0

    conn = await asyncpg.connect(
        host=os.environ["PGHOST"],
        port=int(os.environ.get("PGPORT", "5432")),
        database=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        timeout=15,
    )
    try:
        if not args.apply:
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

        deletable, blocked, gone = [], [], []
        for e in entries:
            exists = await conn.fetchval(
                "SELECT 1 FROM template WHERE id=$1::uuid", e["template_id"]
            )
            if not exists:
                gone.append(e)
                continue
            leads = await conn.fetchval(
                "SELECT COUNT(*) FROM lead_call_tracker WHERE template_id=$1::uuid",
                e["template_id"],
            )
            chats = await conn.fetchval(
                "SELECT COUNT(*) FROM chat_session WHERE template_id=$1::uuid",
                e["template_id"],
            )
            widgets = await conn.fetchval(
                "SELECT COUNT(*) FROM widget_config WHERE template_id=$1::uuid",
                e["template_id"],
            )
            if leads or chats or widgets:
                blocked.append((e, leads, chats, widgets))
            else:
                deletable.append(e)

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"── {mode} revert of {Path(args.journal).name}")
        print(f"   journal entries : {len(entries)}")
        print(f"   deletable       : {len(deletable)}")
        print(f"   already gone    : {len(gone)}")
        print(f"   BLOCKED (in use): {len(blocked)}")
        for e, leads, chats, widgets in blocked:
            print(
                f"     ✋ {e['merchant_id']}  template={e['template_id']} "
                f"leads={leads} chats={chats} widgets={widgets} — manual decision"
            )

        if not args.apply:
            for e in deletable[:8]:
                print(
                    f"     would delete {e['merchant_id']}  template={e['template_id']}"
                    + (f" config={e['config_id']}" if e.get("config_id") else "")
                )
            if len(deletable) > 8:
                print(f"     … +{len(deletable) - 8} more")
            print("   dry run — nothing deleted. Re-run with --apply to execute.")
            return 0

        reverted = failed = 0
        for e in deletable:
            try:
                async with conn.transaction():
                    if e.get("config_id"):
                        await conn.execute(
                            "DELETE FROM call_execution_config WHERE id=$1",
                            e["config_id"],
                        )
                    await conn.execute(
                        "DELETE FROM template WHERE id=$1::uuid", e["template_id"]
                    )
                reverted += 1
                print(f"   ✓ reverted {e['merchant_id']}")
            except Exception as exc:  # FK race etc. — report, keep going
                failed += 1
                print(f"   ✗ {e['merchant_id']}: {exc}", file=sys.stderr)
        print(
            f"── done: {reverted} reverted, {failed} failed, {len(blocked)} left blocked"
        )
        return 0 if failed == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
