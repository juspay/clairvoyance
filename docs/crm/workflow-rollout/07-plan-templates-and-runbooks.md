# Phase 07 — Plan templates + runbooks (cart board; loan funnel as clocks)

**Kind**: docs + tests · **PR title**: `docs(crm): cart-recovery and loan-dropoff plan templates, validated in CI` · **Depends on**: 00, 01, 02, 06 · **Notes**: §12, §13 Option A, §16.1, §15.1 Phase 1

## Why
The two target flows become concrete, versioned JSON documents that a test validates on every CI run, plus a runbook per flow so ops can publish them without reading code. This is also how the loan funnel ships NOW (per-stage clocks) before versioning exists.

## Deliverables
1. `docs/crm/plans/cart-recovery.json` — §16.1 exactly (entry `checkouts/update`, `reenter: true`, `cooldown_hours: 24`, `on_repeat: refresh_latest`, `debounce_minutes: 30`; `goals` two tiers keyed on `cart_token`; `wait 30 → send whatsapp cart_recovery_1 → wait 30 → call <template> → wait 1440`; `purpose_key: marketing.cart.recovery`; `exits.max_age_days: 7`). Placeholders (`<template_id>`) as `"TEMPLATE_ID_PLACEHOLDER"` strings so the document validates.
2. `docs/crm/plans/loan-dropoff/` — one JSON per stage (`01-profile.json` … `05-agreement.json`): entry = stage topic, `reenter: true`, `cooldown_hours: 1`, `on_repeat: refresh_latest`, `debounce_minutes: 30`; nodes `wait 30 → call <stage template>`; `goals` = `[{topics: <every downstream stage + loan.disbursed>, exit_reason: goal_met}, {topics: [loan.rejected, loan.withdrawn], exit_reason: withdrawn}]`; `key: application_id`. A `README.md` in the folder explains the clock pattern (§13 A) and that phase 17 replaces it with one board.
3. `tests/crm/test_plan_templates.py`: loads every `docs/crm/plans/**/*.json` and asserts `validate_definition(doc) == []`; asserts the loan stage plans' goal lists are exactly "all downstream stages" (compute from an ordered list in the test so a missing topic fails CI).
4. `docs/crm/runbooks/cart-recovery.md` and `docs/crm/runbooks/loan-dropoff.md`: prerequisites (connector onboarded, template approved, s2s token, relay topic names), the `POST /crm/workflows` → `PUT draft` → `POST publish` → `POST status` sequence with curl, how to read `GET /runs?status=parked`, how `resume` works, what each exit reason means, the settings to change per merchant.

## Acceptance
- CI validates the documents. No app code changes except possibly a `tests/` helper.
- Both runbooks reviewed against `app/crm/outreach/api.py` route signatures (merchant_id is a query param on every route; admin JWT).

## Decisions already made
- Goal key for cart is `cart_token`; if the relay's checkout payload lacks it, switch to `token` and note it.
- Loan clocks trigger on stage EVENTS (not a stage attribute) because the source system is an API integrator sending events today; §15.1 mentions the attribute variant as a later option.

## Out of scope
- Any code change to the walker or validator. Reporting (phase 09).
