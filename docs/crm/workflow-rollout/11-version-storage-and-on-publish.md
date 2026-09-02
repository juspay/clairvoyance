# Phase 11 — Version storage + `on_publish` word

**Kind**: feat + migration · **PR title**: `feat(crm): crm_workflow_version — publish writes an immutable version row; on_publish pin|migrate` · **Depends on**: 10 · **Notes**: §15.1 Phase 2, §15.3

## Design
### Migration `NNN_create_crm_workflow_version.sql`
```sql
CREATE TABLE crm_workflow_version (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id  text NOT NULL,
    workflow_id  uuid NOT NULL,
    version      integer NOT NULL,
    definition   jsonb NOT NULL,
    on_publish   text NOT NULL DEFAULT 'pin' CHECK (on_publish IN ('pin','migrate')),
    published_by text,
    published_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (merchant_id, workflow_id) REFERENCES crm_workflow (merchant_id, id)
);
CREATE UNIQUE INDEX crm_workflow_version_uq ON crm_workflow_version (merchant_id, workflow_id, version);
```
- Immutability trigger: refuse UPDATE of `definition`/`version` (copy the 051 pattern). No DELETE grant changes (one role) — the retention sweep (phase 14) deletes only unreferenced versions.
- Backfill in the same migration: `INSERT … SELECT merchant_id, id, version, definition FROM crm_workflow WHERE definition IS NOT NULL` so every existing live plan has its current version row.
- `TABLE_OWNERS["crm_workflow_version"] = "outreach"`; `docs/crm/migrations.md` entry.
### Vocabulary
- `WorkflowDefinition.on_publish: Literal["pin","migrate"] = "pin"` (schemas.py). Validator: when `migrate`, the existing stranding checks apply (they already run when `occupied_nodes`/`live_entry` are passed); when `pin`, **skip** the occupied-node and entry-change refusals (the new version cannot strand anyone).
### Publish atom (`plans.py::_publish_in_txn`)
- After validation: `apply_publish` (copies draft→definition, bumps version — unchanged) then `accessor.insert_version(txn, merchant, workflow_id, published.version, published.definition, on_publish, created_by)`; if `migrate`, `accessor.repin_open_runs(txn, merchant, workflow_id, published.version)` → `UPDATE crm_workflow_enrollment SET workflow_version=$3 WHERE merchant_id=$1 AND workflow_id=$2 AND status<>'exited'`. All in the one atom (docstring `ATOMIC: version row + repin share the publish's fate`).
- `published_by`: thread `current_user.email` from `api.py` into `publish_workflow(merchant, id, published_by)`.
### Reads
- `accessor.get_definition(merchant, workflow_id, version) -> Optional[Dict]` (single statement, self-scoped) — used by phase 12. Not wired to the walker yet.

## Red tests
- Migration numbering guard; `TABLE_OWNERS` completeness (rule 6 test exists: `tests/crm/test_check_boundaries.py`).
- Queries: `insert_version_query` merchant-first; `repin_open_runs_query` has `status <> 'exited'`.
- Plans: `pin` + occupied node removed → publishes; `migrate` + occupied node removed → refused; monkeypatched accessor asserts `repin_open_runs` called only for `migrate`.

## Acceptance
- Suite green; boundary clean; migration applies; existing behaviour for `migrate` plans identical to today except the version row.

## Out of scope
- Walker reading versions (12). Consumer (13). Ops (14).
