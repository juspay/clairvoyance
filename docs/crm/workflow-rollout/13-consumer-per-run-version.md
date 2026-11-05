# Phase 13 — Entry consumer evaluates per-run versions

**Kind**: feat · **PR title**: `feat(crm): entries match the latest version; goals and listening match each open run's version` · **Depends on**: 12 · **Notes**: §14.7 (entry consumer redesign), §15.3

## Why
`entry.py::consume_attributed_event` loads the merchant's LIVE plans once and evaluates entry, goal and listening from the same (latest) document. With pinning, a run on v3 must be cancelled by v3's goals and woken by v3's `wait_event` topics, even after v5 changed them.

## Design
- New accessor `open_runs_for_customer(merchant, customer) -> List[EnrollmentRun]` (index `crm_workflow_enrollment_customer_ix`; `status <> 'exited'`).
- Consumer shape becomes two passes:
  1. **Per open run** (goals + listening): for each open run, `definition = get_definition(run.workflow_id, run.workflow_version)` (reuse the walker's cache — move the cache into a small `outreach/definitions.py` concern used by both walker and entry; it is logic, not db); evaluate goal tiers (phase 06) and `wait_event` listening against THAT definition; `cancel_open_runs`/`resume_run_on_event` are already scoped by `(merchant, workflow, customer[, node])` — add `AND id = $n` variants or pass the run id so a v3 goal never touches a sibling run on v5 (`cancel_run_query(run_id, reason, occurred_at, key)`; `resume_run_query` by run id).
  2. **Entries**: latest live plans as today (`live_workflows`), unchanged; keyed by the latest `entry`.
- Ordering law kept: goals first, then replies, then entries (docstring explains why).
- `apply_repeat` (#1041) also needs the run's version's `entry` words, not the latest: pass the run's definition.

## Red tests
- `tests/crm/test_workflow_entry.py`: two open runs on v3 and v5 with different goal topics; an event matching only v3's goal cancels only the v3 run (monkeypatched accessors record ids); listening likewise; entries still use v5.
- Query builders: `cancel_run_query`/`resume_run_by_id_query` carry `id = $` and merchant-first.

## Acceptance
- Suite green; boundary clean (`definitions.py` imports only the module's db door).
- `context/reading-notes.md` §15.3 sentence "one consumer with two reads" now true in code — cite the file.

## Out of scope
- Drain tooling (14).
