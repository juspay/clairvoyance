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
   new migration with the next number. (The one historical exception: the
   pre-CI duplicate numbers 026/034 were renumbered to 046/047 on
   2026-08-22, reconciled by `RENAMED_MIGRATIONS` in `scripts/migrate.py`
   so no environment re-runs them. Do not add to that list.)
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
  `crm_campaign` to avoid colliding with buddy's existing `campaign`).
- Append-only tables (crm_consent_event) additionally REVOKE UPDATE,
  DELETE and add the refusal trigger in the same migration.
- Partitioned tables (crm_event_raw, crm_message, crm_decision_log)
  declare RANGE partitioning in their CREATE.
- No CHECK constraints on channel/connector vocabularies (the
  migration-027 scar) — vocabulary lives in code dicts; a new channel is
  a deploy, never a migration.
- Accessors reach these tables only through
  `app.crm.shared.db.crm_transaction()`.
