# Phase 09 — Runs reporting (G9)

**Kind**: feat · **PR title**: `feat(crm): workflow run summary and journey read — counts by exit reason, time per node` · **Depends on**: 06; **coordinate with PR #1053** (`feat(crm): per-workflow run counts, trigger and updated_by column`, sharifajahanshaik) — if merged, extend its counts rather than duplicating; if open, build on `release` and note the overlap in the PR. · **Notes**: §13 verdict (A + reporting view), §16.3 G9

## Why
With the loan funnel running as per-stage clocks, "where do customers drop" is a join across plans. Boards (later) answer it from one run. Either way the read belongs to outreach and is missing.

## Design
- `GET /crm/workflows/{id}/summary?merchant_id&since&until` → `{runs: n, by_exit_reason: {...}, open: {waiting: n, parked: n}, median_minutes_to_exit}`. One query builder (`workflow_summary_query`) with `GROUP BY exit_reason`; merchant-first; window on `entered_at`.
- `GET /crm/customers/{customer_id}/runs?merchant_id` → the customer's runs across ALL plans ordered by `entered_at` (`customer_runs_query`; index `crm_workflow_enrollment_customer_ix` exists). This is the loan funnel's journey view under Option A.
- Recovered revenue: at goal-cancel time (phase 06 `cancel_open_runs`), stash `context.goal = {topic, event_id, amount}` where `amount` is `payload.total_price` / `payload.amount` if scalar — done in `entry.py` before calling cancel (pass a `context_patch` to `cancel_open_runs_query`: `context = context || $8::jsonb`). Summary adds `recovered_amount = sum((context->'goal'->>'amount')::numeric)` for `goal_met` rows.
- Schemas: `WorkflowSummary` name is taken (list shape) — call it `WorkflowRunSummary`; add `CustomerRun` (a thin `EnrollmentRun` with `workflow_name`).

## Red tests
- Query builders: merchant-first, `$1` params, window predicates present, GROUP BY exit_reason.
- `entry.py`: goal cancel passes a `goal` patch with the amount when present, none when absent (monkeypatched accessor).

## Acceptance
- Routes admin-only (`crm_admin_user`), `set_log_context` on each. Suite green; boundary clean.
- Runbooks (phase 07) gain a "how to read the summary" section.

## Out of scope
- Console UI. Cross-merchant/platform totals.
