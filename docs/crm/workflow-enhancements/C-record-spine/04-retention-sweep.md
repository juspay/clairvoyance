# C/04 — Spine retention sweep (after ADR 0023)

**Track C · step 4** · **Kind**: feat · **PR title**: `feat(crm): crm_event_raw retention sweep — processed letters age out, referenced and quarantined ones never (enh C/04)` · **Depends on**: C/03 signed off, C/01 merged · **Notes**: ADR 0023 (C/03), rollout `runs.run_retention_sweep_tick` precedent

## Design
- Static config `CRM_EVENT_RETENTION_DAYS` (default 180) beside `CRM_RUN_RETENTION_DAYS`.
- `record/workers.py` housekeeping tick on the event-worker pod (same shape as outreach's hourly sweep in `workers.claim_due_runs`): `DELETE FROM crm_event_raw WHERE id IN (SELECT id FROM crm_event_raw WHERE processed_at IS NOT NULL AND quarantine_reason IS NULL AND received_at < $1 AND NOT EXISTS (<a non-exited enrollment whose context->>'source_event_id' or any facts.*.source_event_id equals this id>) ORDER BY received_at LIMIT $2)`. The referenced-run predicate goes through an OUTREACH contract (`event_ids_referenced_by_open_runs(merchant, ids)`) or, simpler and decided: the sweep skips any row younger than the largest live plan's `exits.max_age_days` (read via an outreach contract `max_live_run_age_days()`), which is the ADR's option (1) invariant. Choose the second; it needs no join.
- Publish validator (outreach): `exits.max_age_days <= CRM_EVENT_RETENTION_DAYS` — refuse otherwise (a run could outlive the letters its goal re-check reads).
- Journey view (055) is unaffected (reads `lead_call_tracker`).

## Red tests
- Sweep SQL excludes quarantined rows and rows younger than the cutoff; the cutoff is `min(retention, retention - max_live_age)` semantics pinned; validator refuses an over-long plan.
