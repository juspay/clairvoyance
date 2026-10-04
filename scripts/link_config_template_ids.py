"""Link legacy call_execution_config rows to their owning template by id.

Mirrors migration 026 exactly, but runs it as an announced, journaled,
revertible prod operation BEFORE the code deploys. After the id-only config
resolution ships, template_id is the ONLY runtime linkage — any config left
unlinked stops resolving, so this reports everything it cannot link and why.

Analysis (always, read-only):
  - config population: linked / unlinked, by scope (merchant vs reseller-level)
  - unambiguous matches it would link
  - ambiguous names, orphan configs (no template in scope), second-config
    conflicts, in-flight-lead conflicts (deferred, rerun after drain)
  - post-link coverage: voice templates that would STILL have no config at
    deploy (these lose the old name/scope fallback → outbound 404, inbound
    allow-by-default)

Apply mode journals every UPDATE (config id + old/new template_id) to
scripts/backfill_runs/journal-config-link-<label>.jsonl.
Revert = SET template_id = NULL for journaled config ids (script prints it).

Usage:
  uv run --with asyncpg python3 scripts/link_config_template_ids.py            # dry run
  uv run --with asyncpg python3 scripts/link_config_template_ids.py --apply
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

# Rows this run would link: exact scope + name, unambiguous, no second
# config on the template, no in-flight leads pinned elsewhere.
CANDIDATES_SQL = """
SELECT c.id AS config_id, c.reseller_id, c.merchant_id, c.template AS name,
       t.id AS template_id
FROM call_execution_config c
JOIN template t
  ON t.reseller_id = c.reseller_id
 AND t.merchant_id IS NOT DISTINCT FROM c.merchant_id
 AND t.name = c.template
WHERE c.template_id IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM template t2
        WHERE t2.reseller_id = c.reseller_id
          AND t2.merchant_id IS NOT DISTINCT FROM c.merchant_id
          AND t2.name = c.template
          AND t2.id <> t.id)
  AND NOT EXISTS (
        SELECT 1 FROM call_execution_config c2
        WHERE c2.template_id = t.id AND c2.id <> c.id)
  AND NOT EXISTS (
        SELECT 1 FROM lead_call_tracker l
        WHERE l.merchant_id = c.merchant_id
          AND l.template = c.template
          AND l.status IN ('BACKLOG', 'RETRY', 'PROCESSING')
          AND l.template_id IS DISTINCT FROM t.id)
ORDER BY c.reseller_id, c.merchant_id NULLS FIRST, c.template
"""

SKIPPED_SQL = """
SELECT c.id AS config_id, c.reseller_id, c.merchant_id, c.template AS name,
  CASE
    WHEN NOT EXISTS (
        SELECT 1 FROM template t WHERE t.reseller_id = c.reseller_id
          AND t.merchant_id IS NOT DISTINCT FROM c.merchant_id
          AND t.name = c.template)
      THEN 'orphan (no template in scope)'
    WHEN (SELECT COUNT(*) FROM template t WHERE t.reseller_id = c.reseller_id
          AND t.merchant_id IS NOT DISTINCT FROM c.merchant_id
          AND t.name = c.template) > 1
      THEN 'ambiguous (multiple templates share the name in scope)'
    WHEN EXISTS (
        SELECT 1 FROM template t
        JOIN call_execution_config c2 ON c2.template_id = t.id AND c2.id <> c.id
        WHERE t.reseller_id = c.reseller_id
          AND t.merchant_id IS NOT DISTINCT FROM c.merchant_id
          AND t.name = c.template)
      THEN 'template already linked to another config'
    ELSE 'in-flight leads pinned to a different template (rerun after drain)'
  END AS reason
FROM call_execution_config c
WHERE c.template_id IS NULL
  AND c.id NOT IN (SELECT config_id FROM ({candidates}) x)
ORDER BY reason, c.reseller_id, c.merchant_id NULLS FIRST
""".format(candidates=CANDIDATES_SQL)

POPULATION_SQL = """
SELECT
  COUNT(*)                                            AS total,
  COUNT(*) FILTER (WHERE template_id IS NOT NULL)     AS linked,
  COUNT(*) FILTER (WHERE template_id IS NULL)         AS unlinked,
  COUNT(*) FILTER (WHERE template_id IS NULL
                     AND merchant_id IS NOT NULL)     AS unlinked_merchant,
  COUNT(*) FILTER (WHERE template_id IS NULL
                     AND merchant_id IS NULL)         AS unlinked_shared
FROM call_execution_config
"""

DUP_LINK_SQL = """
SELECT template_id, COUNT(*) AS n
FROM call_execution_config
WHERE template_id IS NOT NULL
GROUP BY template_id HAVING COUNT(*) > 1
"""

# Voice templates with no config AFTER this run's linking — at deploy these
# stop inheriting any reseller-level fallback (strict id-only resolution).
UNCOVERED_SQL = """
SELECT t.reseller_id, t.merchant_id, t.name, t.id,
       (SELECT COUNT(*) FROM lead_call_tracker l
         WHERE l.template_id = t.id
           AND l.created_at > NOW() - INTERVAL '30 days') AS leads_30d
FROM template t
WHERE t.is_active
  AND (t.supported_channels IS NULL OR 'voice' = ANY(t.supported_channels)
       OR 'telephony' = ANY(t.supported_channels))
  AND NOT EXISTS (SELECT 1 FROM call_execution_config c WHERE c.template_id = t.id)
  AND t.id NOT IN (SELECT template_id FROM ({candidates}) y)
ORDER BY leads_30d DESC, t.reseller_id, t.merchant_id NULLS FIRST
""".format(candidates=CANDIDATES_SQL)

UPDATE_SQL = """
UPDATE call_execution_config
SET template_id = $2::uuid, updated_at = NOW()
WHERE id = $1 AND template_id IS NULL
RETURNING id
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="perform writes (default: dry run)"
    )
    ap.add_argument("--run-label", default=None, help="journal label (apply mode)")
    args = ap.parse_args()

    for var in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        if not os.environ.get(var):
            print(f"error: {var} not set", file=sys.stderr)
            return 2

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

        pop = await conn.fetchrow(POPULATION_SQL)
        dups = await conn.fetch(DUP_LINK_SQL)
        candidates = await conn.fetch(CANDIDATES_SQL)
        skipped = await conn.fetch(SKIPPED_SQL)
        uncovered = await conn.fetch(UNCOVERED_SQL)

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"── {mode} config→template linking")
        print(
            f"   configs: {pop['total']} total, {pop['linked']} linked, "
            f"{pop['unlinked']} unlinked ({pop['unlinked_merchant']} merchant-scoped, "
            f"{pop['unlinked_shared']} reseller-level)"
        )
        print(f"   would link now : {len(candidates)}")
        print(f"   skipped        : {len(skipped)}")
        for r in skipped:
            print(
                f"     ✋ {r['reason']}: config={r['config_id']} "
                f"reseller={r['reseller_id']} merchant={r['merchant_id']} name={r['name']}"
            )
        if dups:
            print(f"   ⚠ existing duplicate links (must dedup before unique index):")
            for d in dups:
                print(f"     template {d['template_id']} has {d['n']} configs")
        else:
            print("   existing links: no duplicates (unique index will apply cleanly)")
        print(
            f"   voice templates with NO config after this run: {len(uncovered)} "
            f"(lose fallback at deploy — create configs or accept)"
        )
        for r in uncovered[:15]:
            print(
                f"     ∅ {r['reseller_id']} / {r['merchant_id'] or '(reseller-level)'} / "
                f"{r['name']}  leads_30d={r['leads_30d']}  id={r['id']}"
            )
        if len(uncovered) > 15:
            print(f"     … +{len(uncovered) - 15} more")

        if not args.apply:
            by_scope = {}
            for c in candidates:
                key = (
                    c["reseller_id"],
                    "shared" if c["merchant_id"] is None else "merchant",
                )
                by_scope[key] = by_scope.get(key, 0) + 1
            for (rid, scope), n in sorted(by_scope.items()):
                print(f"     → would link {n:4d}  {rid} [{scope}]")
            print("   dry run — nothing written. Re-run with --apply to execute.")
            return 0

        label = args.run_label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        jpath = Path("scripts/backfill_runs") / f"journal-config-link-{label}.jsonl"
        jpath.parent.mkdir(exist_ok=True)
        linked = failed = 0
        with jpath.open("a") as journal:
            for c in candidates:
                try:
                    got = await conn.fetchval(
                        UPDATE_SQL, c["config_id"], str(c["template_id"])
                    )
                    if got:
                        linked += 1
                        journal.write(
                            json.dumps(
                                {
                                    "event": "linked",
                                    "config_id": c["config_id"],
                                    "template_id": str(c["template_id"]),
                                    "reseller_id": c["reseller_id"],
                                    "merchant_id": c["merchant_id"],
                                    "name": c["name"],
                                    "at": datetime.now(timezone.utc).isoformat(),
                                }
                            )
                            + "\n"
                        )
                        journal.flush()
                    else:
                        failed += 1
                        print(f"   ✗ {c['config_id']}: row changed underneath, skipped")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(f"   ✗ {c['config_id']}: {exc}", file=sys.stderr)
        print(f"── done: {linked} linked, {failed} failed. journal: {jpath}")
        print(
            "   revert: UPDATE call_execution_config SET template_id = NULL "
            "WHERE id IN (<config_ids from journal>);"
        )
        return 0 if failed == 0 else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
