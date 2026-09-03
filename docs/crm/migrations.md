# Migration conventions (046+)

`scripts/migrate.py up` applies `app/database/migrations/*.sql` in
lexicographic order, each file in its own transaction, tracked by
filename + sha256 in `_clairvoyance_migrations`.

## The rules

CI-enforced by `scripts/check_migrations.py` and the immutability guard in
`.github/workflows/pr-build-check.yml`:

1. **Sequential `NNN_snake_case.sql`, no duplicates, no gaps.** Take the
   next free number. If someone merges your number first, renumber your
   file before merge — CI blocks the duplicate.
2. **Never edit, rename, or delete a merged migration.** The SQL already
   ran; the tracker knows it by filename and checksum. Corrections are a
   new migration with the next number. `RENAMED_MIGRATIONS` in
   `scripts/migrate.py` exists for exactly ONE situation: an
   already-merged **duplicate number** that escaped CI (a content
   correction is NEVER a rename). Entries so far: the pre-CI 026/034
   pair, renumbered to 046/047 on 2026-08-22; and 052 (journey view →
   055) on 2026-08-25, after two open PRs each passed HEAD-only CI with
   the same number. That escape class is now closed —
   `check_migrations.py --base` unions the PR's migrations with the
   CURRENT target branch, and the immutability guard derives its rename
   exemptions from `RENAMED_MIGRATIONS` (`--print-sanctioned`), so the
   registry and CI cannot drift. Adding an entry requires all three in
   one commit: the registry entry, this doc's list above, and the red
   test in `tests/crm/test_check_migrations.py` pinning the new name.
3. **048+ is the CPaaS era.** One table owner per migration — the task
   that owns a table ships its migration (vertical slices).
4. Migrations run as `POSTGRES_USER` via
   `uv run python scripts/migrate.py up`; check state with
   `uv run python scripts/migrate.py status`.

## CRM table template (048+)

CRM tables live in the default schema with a `crm_` name prefix
(`platform_identity` for the cross-merchant identity/suppression table) —
no separate Postgres schemas and no per-module DB roles. This IS the
canon position: ADR 0001 was amended 2026-08-23 to move boundary
enforcement into code (prefixes + CI SQL-ownership lint + import-linter +
table-level CHECKs/triggers). Cross-module access goes through each
module's `contracts.py`, never another module's tables.

```sql
-- NNN: <module> — crm_<table> (T##, canon/0X-<file>.md)
CREATE TABLE crm_<table> (
    id          uuid PRIMARY KEY,
    merchant_id text NOT NULL,
    -- ... columns exactly as the canon table spec ...
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- merchant_id is the FIRST column of every unique index (tenancy law).
CREATE UNIQUE INDEX crm_<table>_merchant_<handle>_uq
    ON crm_<table> (merchant_id, <handle>) WHERE status = 'active';
```

Rules the template encodes:

- **One owning module per table** — the module that owns the contract owns
  the writes (identity → crm_customer; platform → platform_identity, Permission
  squad owns it; permission →
  crm_consent_*, crm_decision_log; connectivity → installations, bindings,
  templates, crm_message; record → crm_event_raw; outreach →
  segments/workflows/broadcasts — note the P2 outreach table must be
  `crm_campaign` to avoid colliding with buddy's existing `campaign`;
  `crm_workflow_version` (064, ADR 0023) is outreach's too — the
  immutable per-publish document runs are pinned to, rows only, never
  edits and never deletes (ADR 0023 §5 as amended: no retention sweep;
  064's comment saying otherwise is superseded); its `on_publish` is a
  closed enum in a CHECK).
- Append-only tables (crm_consent_event) additionally REVOKE UPDATE,
  DELETE and add the refusal trigger in the same migration.
- Partitioned tables (crm_event_raw, crm_message, crm_decision_log)
  declare RANGE partitioning in their CREATE.
- No CHECK constraints on channel/connector vocabularies (the
  migration-027 scar) — vocabulary lives in code dicts; a new channel is
  a deploy, never a migration.
- Accessors reach these tables only through
  `app.crm.shared.db.crm_transaction()`.
- On `crm_event_raw` the envelope columns (`processed_at`,
  `quarantine_reason`, `customer_id`, `attempts`) are the only mutable
  ones — the 051 immutability trigger (amended in 062) refuses every
  ingestion field, and `attempts` is spent by the claim so a poison row
  quarantines after `CRM_EVENT_MAX_ATTEMPTS` instead of looping forever.
- `crm_workflow_enrollment.exit_reason` is a closed enum in a CHECK
  (`goal_met`, `timed_out`, `withdrawn`, `ejected`, `completed`,
  `converted_elsewhere` — the last added by 063 so a goal tier keyed to
  the run's own cart can say "recovered" while any other order still ends
  the run, distinguishably). A new reason is a migration that re-creates
  the constraint under its explicit name, never an edit to 058/063.
