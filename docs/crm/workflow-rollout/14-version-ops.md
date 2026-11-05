# Phase 14 — Version operations: migrate-forward, template guard

**Kind**: feat · **PR title**: `feat(crm): version drain — migrate-forward endpoint, template retirement guard` · **Depends on**: 13 · **Notes**: §14.7, §15.1 Phase 2 tooling

## Design
1. **Migrate-forward**: `POST /crm/workflows/{id}/versions/{from}/migrate?merchant_id&to=<version>` (admin). Atom `_migrate_forward_in_txn`: load both definitions; refuse unless every node occupied by runs on `from` exists in `to` AND `to.entry` equals `from.entry` (model_dump compare; phase 01) — the stranding validator reused as a pure function `validate_migration(from_doc, to_doc, occupied_nodes) -> problems`; then `UPDATE … SET workflow_version=$to WHERE workflow_id AND workflow_version=$from AND status<>'exited' RETURNING id`. Returns the count. This is how a typo fix reaches pinned runs.
2. **Retention** — **dropped 2026-09-03, before merge** (ADR 0023 §5 as amended): versions are never deleted. No sweep, no `CRM_VERSION_RETENTION_DAYS`. The sweep as originally specified here (an hourly batched DELETE of versions below the plan's current one, older than a retention window, with no open run pinned to them) was built and removed: it saved nothing measurable and made an exited run's `workflow_version` point at nothing.
3. **Template retirement guard**: connectivity `templates.retire(...)` must refuse when a non-exited run's pinned definition names the template on a send node. Boundary: connectivity may not read outreach tables; add outreach contract `runs_referencing_template(merchant, channel, name) -> int` (query joins enrollment → version → `definition->'nodes'` with `jsonb_path_exists`; index later if hot) and call it from `templates.retire` via `app.crm.outreach.contracts`. **Cycle check**: outreach already imports connectivity contracts (`queue_message`); connectivity importing outreach contracts closes a cycle at import time. Resolution (decision): register the guard through a small hook in connectivity (`templates.register_retire_guard(fn)`) filled by `app/crm/worker_main.py`/`app/main.py` composition root — the same inversion as `record/consumers.py` (rule 12 precedent). The same for buddy call templates is out of scope (buddy's template deletion path is legacy). *Amended before merge:* the guard also counts live or paused plans whose latest document names the template (their next entrant is stranded the same way), the contract is `template_references -> (open_runs, plans)`, and retire withdraws locally first — check and write in one atom — with the provider call after. The atom holds a transaction-scoped advisory lock keyed by the template (`app/crm/shared/locks.py`, EXCLUSIVE); the enrol, publish and migrate atoms hold the same key SHARED for every template their document sends, which closes the check-then-write interleaving without a cross-module transaction (proven on Postgres in the PR). Migrate-forward also runs publish's template-approval check on the target.
4. `GET /crm/workflows/{id}/versions?merchant_id` → list `{version, on_publish, published_by, published_at, open_runs}` (coordinate with #1053's counts).

## Red tests
- `validate_migration` pure cases (node missing → refused; entry changed → refused; ok → []).
- No DELETE builder for `crm_workflow_version` exists, and no sweep tick (versions are kept).
- Retire guard: hook registered and count ≥1 → retire refused with the count; hook registered and count 0 → retire proceeds; **no hook registered → retire refuses and logs an error** (fail closed: a missing registration is a wiring bug, not permission to delete). `app/main.py` and `worker_main.py` must register it. Test all three.

## Acceptance
- Suite green; boundary clean (hook registration, no direct cross import); config documented in `app/core/config/static.py` beside `CRM_RUN_RETENTION_DAYS`.

## Out of scope
- UI for versions. Buddy template deletion guard (backlog).
