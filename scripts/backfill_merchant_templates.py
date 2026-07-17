"""Backfill: one template per merchant (remove shared-template reliance).

For a (reseller, family) pair, snapshots the reseller-level shared template
(merchant_id IS NULL) and creates a byte-identical, merchant-scoped copy for
every active merchant that doesn't own one, plus a merchant-scoped calling
config copied from the shared config when the merchant lacks one.

Copy semantics (deliberate):
  - same ``name``          → calling-config resolution and name-keyed voice
                             analytics stay continuous per merchant
  - ``_lineage`` key added to the flow JSON: {family, base_template_id,
    base_hash, run} → future fleet rollouts (three-way merges) are mechanical
  - ``configurations.enable_inbound`` forced ``false`` → inbound routing
    stays exactly where it is (the shared rows are false today too)
  - same ``outbound_number_id`` → outbound caller id keeps working
  - ``is_active`` copied (true) → the copy must be live-ready: on current
    prod code, NAME-based pushers (Nautilus & some merchants) flip to the
    copy on their next push — creation IS the cutover for that cohort.
    Rollback for a merchant = DELETE the copy; name resolution falls back
    to the shared row again.

Modes:
  dry-run (default)  read-only session; prints the full per-merchant plan and
                     writes it to scripts/backfill_runs/plan-*.json
  --apply            performs the inserts, one transaction per merchant,
                     journaling every created row to
                     scripts/backfill_runs/journal-<run>.jsonl (the rollback
                     list). Idempotent: merchants that already own the family
                     are skipped; the DB unique index is the backstop.

Connection via standard libpq env vars (PGHOST/PGPORT/PGDATABASE/PGUSER/
PGPASSWORD) — export them yourself; never store credentials in files.

Usage:
  uv run --with asyncpg python3 scripts/backfill_merchant_templates.py \
      --reseller BB_SHOPIFY --family order-confirmation            # dry run
  ... --apply --canary shop-a.myshopify.com,shop-b.myshopify.com   # canary
  ... --apply --limit 100 --run-label wave-1                        # wave
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

RUNS_DIR = Path(__file__).resolve().parent / "backfill_runs"

TEMPLATE_COPY_SQL = """
INSERT INTO template (reseller_id, merchant_id, name, flow,
                      expected_payload_schema, expected_callback_response_schema,
                      is_active, configurations, secrets, outbound_number_id,
                      supported_channels)
SELECT reseller_id, $2, name,
       flow || $3::jsonb,
       expected_payload_schema, expected_callback_response_schema,
       is_active,
       jsonb_set(COALESCE(configurations, '{}'::jsonb),
                 '{enable_inbound}', 'false'::jsonb),
       secrets, outbound_number_id, supported_channels
FROM template
WHERE id = $1::uuid AND merchant_id IS NULL
RETURNING id
"""

CONFIG_COPY_SQL = """
INSERT INTO call_execution_config (
    id, initial_offset, retry_offset, call_start_time, call_end_time,
    max_retry, calling_provider, template, enable_international_call,
    enable_calling, template_id, pre_checks, telephony_config, reseller_id,
    merchant_id, enable_inbound, inbound_call_start_time,
    inbound_call_end_time, inbound_call_timezone, inbound_block_action,
    inbound_redirect_number, inbound_block_message, enforce_blacklist,
    rate_limit_enabled, rate_limit_max_calls, rate_limit_window_seconds,
    rate_limit_whitelist)
SELECT $2, initial_offset, retry_offset, call_start_time, call_end_time,
       max_retry, calling_provider, template, enable_international_call,
       enable_calling, $3::uuid, pre_checks, telephony_config, reseller_id,
       $4, enable_inbound, inbound_call_start_time,
       inbound_call_end_time, inbound_call_timezone, inbound_block_action,
       inbound_redirect_number, inbound_block_message, enforce_blacklist,
       rate_limit_enabled, rate_limit_max_calls, rate_limit_window_seconds,
       rate_limit_whitelist
FROM call_execution_config
WHERE id = $1 AND merchant_id IS NULL
RETURNING id
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reseller", required=True)
    ap.add_argument("--family", required=True, help="shared template name to replicate")
    ap.add_argument(
        "--apply", action="store_true", help="perform writes (default: dry run)"
    )
    ap.add_argument("--limit", type=int, default=None, help="max merchants this run")
    ap.add_argument(
        "--canary",
        default=None,
        help="comma-separated merchant_ids; process ONLY these",
    )
    ap.add_argument(
        "--run-label", default=None, help="label for the journal file (apply mode)"
    )
    args = ap.parse_args()

    for var in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        if not os.environ.get(var):
            print(f"error: {var} not set (see file header)", file=sys.stderr)
            return 2

    now = datetime.now(timezone.utc)
    run_id = f"{args.run_label or 'run'}-{now.strftime('%Y%m%dT%H%M%SZ')}"

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
            # belt & braces: a dry run cannot write even by accident
            await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

        # ── snapshot the shared row ────────────────────────────────────────
        shared = await conn.fetchrow(
            """SELECT id, name, is_active, outbound_number_id,
                      flow::text AS flow_text, configurations::text AS cfg_text,
                      COALESCE(secrets::text, '') AS sec_text, updated_at
               FROM template
               WHERE reseller_id=$1 AND merchant_id IS NULL AND name=$2""",
            args.reseller,
            args.family,
        )
        if not shared:
            print(
                f"error: no shared template '{args.family}' for {args.reseller}",
                file=sys.stderr,
            )
            return 2
        base_hash = hashlib.sha256(
            (
                shared["flow_text"]
                + "|"
                + (shared["cfg_text"] or "")
                + "|"
                + shared["sec_text"]
            ).encode()
        ).hexdigest()[:16]

        shared_cfg = await conn.fetchrow(
            """SELECT id FROM call_execution_config
               WHERE reseller_id=$1 AND merchant_id IS NULL AND template=$2""",
            args.reseller,
            args.family,
        )

        # ── build the work-list ────────────────────────────────────────────
        merchants = [
            r["merchant_id"]
            for r in await conn.fetch(
                "SELECT merchant_id FROM merchants WHERE reseller_id=$1 AND is_active ORDER BY created_at",
                args.reseller,
            )
        ]
        have_tpl = {
            r["merchant_id"]
            for r in await conn.fetch(
                "SELECT merchant_id FROM template WHERE reseller_id=$1 AND name=$2 AND merchant_id IS NOT NULL",
                args.reseller,
                args.family,
            )
        }
        have_cfg = {
            r["merchant_id"]
            for r in await conn.fetch(
                "SELECT merchant_id FROM call_execution_config WHERE reseller_id=$1 AND template=$2 AND merchant_id IS NOT NULL",
                args.reseller,
                args.family,
            )
        }

        todo = [m for m in merchants if m not in have_tpl]
        if args.canary:
            wanted = {m.strip() for m in args.canary.split(",") if m.strip()}
            unknown = wanted - set(merchants)
            if unknown:
                print(
                    f"error: canary merchants not in universe: {sorted(unknown)}",
                    file=sys.stderr,
                )
                return 2
            already = wanted & have_tpl
            if already:
                print(
                    f"note: canary merchants already own the family (skipped): {sorted(already)}"
                )
            todo = [m for m in todo if m in wanted]
        if args.limit is not None:
            todo = todo[: args.limit]

        plan = {
            "run_id": run_id,
            "mode": "apply" if args.apply else "dry-run",
            "reseller": args.reseller,
            "family": args.family,
            "base_template_id": str(shared["id"]),
            "base_hash": base_hash,
            "base_updated_at": shared["updated_at"].isoformat(),
            "shared_config_id": shared_cfg["id"] if shared_cfg else None,
            "universe_active_merchants": len(merchants),
            "already_have_template": len(have_tpl),
            "to_create_templates": len(todo),
            "to_create_configs": len(
                [m for m in todo if m not in have_cfg and shared_cfg]
            ),
            "merchants": [
                {
                    "merchant_id": m,
                    "create_template": True,
                    "create_config": bool(shared_cfg) and m not in have_cfg,
                }
                for m in todo
            ],
        }

        RUNS_DIR.mkdir(exist_ok=True)
        plan_path = RUNS_DIR / f"plan-{args.family}-{run_id}.json"
        plan_path.write_text(json.dumps(plan, indent=1))

        print(f"── {plan['mode'].upper()} {args.reseller} / {args.family}")
        print(
            f"   base template  : {shared['id']}  (hash {base_hash}, updated {shared['updated_at']:%Y-%m-%d})"
        )
        print(
            f"   shared config  : {shared_cfg['id'] if shared_cfg else '— none (no config copies will be made)'}"
        )
        print(f"   universe       : {len(merchants)} active merchants")
        print(f"   already owners : {len(have_tpl)}")
        print(
            f"   THIS RUN       : {len(todo)} template copies, "
            f"{plan['to_create_configs']} config copies"
        )
        print(f"   plan file      : {plan_path}")

        if not args.apply:
            preview = ", ".join(m["merchant_id"] for m in plan["merchants"][:8])
            more = len(todo) - min(8, len(todo))
            print(
                f"   first merchants: {preview}{f' … +{more} more' if more > 0 else ''}"
            )
            print("   dry run — nothing written. Re-run with --apply to execute.")
            return 0

        # ── apply ──────────────────────────────────────────────────────────
        journal_path = RUNS_DIR / f"journal-{args.family}-{run_id}.jsonl"
        created_t = created_c = failed = 0
        lineage = json.dumps(
            {
                "_lineage": {
                    "family": args.family,
                    "base_template_id": str(shared["id"]),
                    "base_hash": base_hash,
                    "run": run_id,
                }
            }
        )
        with journal_path.open("a") as journal:
            journal.write(
                json.dumps(
                    {
                        "event": "run_start",
                        **{k: v for k, v in plan.items() if k != "merchants"},
                    }
                )
                + "\n"
            )
            for m in todo:
                try:
                    async with conn.transaction():
                        new_tpl = await conn.fetchval(
                            TEMPLATE_COPY_SQL, shared["id"], m, lineage
                        )
                        new_cfg = None
                        if shared_cfg and m not in have_cfg:
                            new_cfg = await conn.fetchval(
                                CONFIG_COPY_SQL,
                                shared_cfg["id"],
                                str(uuid.uuid4()),
                                new_tpl,
                                m,
                            )
                    created_t += 1
                    created_c += 1 if new_cfg else 0
                    journal.write(
                        json.dumps(
                            {
                                "event": "created",
                                "merchant_id": m,
                                "template_id": str(new_tpl),
                                "config_id": new_cfg,
                                "at": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                        + "\n"
                    )
                    journal.flush()
                    print(
                        f"   ✓ {m}  template={new_tpl}"
                        + (f" config={new_cfg}" if new_cfg else "")
                    )
                except Exception as e:  # unique-index race, etc. — log and continue
                    failed += 1
                    journal.write(
                        json.dumps(
                            {"event": "error", "merchant_id": m, "error": str(e)}
                        )
                        + "\n"
                    )
                    journal.flush()
                    print(f"   ✗ {m}: {e}", file=sys.stderr)
            journal.write(
                json.dumps(
                    {
                        "event": "run_end",
                        "created_templates": created_t,
                        "created_configs": created_c,
                        "failed": failed,
                    }
                )
                + "\n"
            )
        print(
            f"── done: {created_t} templates, {created_c} configs created, {failed} failed"
        )
        print(f"   journal (rollback list): {journal_path}")
        return 0 if failed == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
