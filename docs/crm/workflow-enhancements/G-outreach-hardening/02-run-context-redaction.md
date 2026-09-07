# G/02 — Redact run context on read (P7)

**Track G · step 2** · **Kind**: fix · **PR title**: `fix(crm): run listings never return contact handles; detail is admin-only and masked (enh G/02)` · **Depends on**: nothing (rollout 16 is merged) · **Notes**: §11 P7, `shared/redact.py`

## Design
- `GET /workflows/{id}/runs` (list) returns `EnrollmentRunSummary` WITHOUT `context`. `GET /workflows/{id}/runs/{run_id}` (new, admin) returns the full run with `context` passed through `redact_context()`: values under `phone`, `customer_mobile_number`, any key matching `email`, and any string matching the E.164 regex are masked with `mask_address`/`mask_digit_runs`; `facts.<node>` recursively. Pure function in `outreach/redaction.py` built on `shared/redact.py` (no new masking rules — reuse).
- rollout 09's customer-runs read gets the same treatment.

## Red tests
- Redaction pure cases; list response schema has no `context` field; detail masks nested facts.
