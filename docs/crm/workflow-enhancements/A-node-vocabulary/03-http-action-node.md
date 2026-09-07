# A/03 — `http` action node (G5 part 2)

**Track A · step 3** · **Kind**: feat · **PR title**: `feat(crm): http action node — call a merchant endpoint from a run, results become facts (enh A/03)` · **Depends on**: A/02 (path grammar); **PR #987 merged** (`fix(security): SSRF egress guard`, murdore) — if not merged, skip to A/04 and come back; an outbound-fetch node without the egress guard is a hole, not a feature · **Notes**: §16.3 G5

## Why
Coupons ("create a 10% code and put it in the message"), lender APIs ("mark the application as nudged"), CRMs the merchant already runs. Buddy templates already have `GlobalHttpFunction` for calls; workflows need the same reach.

## Design
- Node: `{id, type: "http", method: GET|POST, url, headers?, body?, auth?: {type: "bearer", credential: <vault credential name>}, timeout_seconds: 10, expected_status: [200,201], facts_from_response: {<key>: <json path>}}`. `url`, `headers` and `body` may use `{placeholders}` resolved from `run_facts(context)` (same resolver the call payload uses); `credential` resolves through `app.database.accessor.breeze_buddy.credentials.get_credential_by_name` scoped to the merchant's reseller (the same door connectivity's `accounts.py` uses) — never inline secrets in the definition (validator refuses `Authorization` in `headers`).
- Execution: `execute_http` via `app/core/transport/http_client.create_http_client` behind the #987 egress guard; success → `facts_from_response` paths (same tiny grammar as A/02's `extract_letter_fact`) merged under `facts.<node>` (rollout 16 namespacing) and mirrored flat for templates by `run_facts` precedence. Classification: transport error / 5xx / 429 → raise (transient; the walker's retry ladder handles it); 4xx or unexpected status → `NodeParked` with status and a 200-char body excerpt masked by `mask_digit_runs`.
- Idempotency: the node is at-least-once (a lease retry after a crash re-issues the call). Give the merchant a deterministic `Idempotency-Key: <run id>:<node id>` header automatically; document that their endpoint should honour it.
- `NodeSpec(is_wait=False, branches=False)`. Validator: url is https, no private hosts (the guard decides at runtime too), placeholders name known facts or are marked optional.

## Red tests
- Placeholder resolution; the idempotency header; 5xx → raises; 4xx → NodeParked with masked excerpt; response facts land under `facts.<node>`; `Authorization` header in the definition refused; `http://` refused.

## Acceptance
- Suite green; boundary clean (outreach → app.database accessor for credentials is allowed today, same as `nodes.execute_call`). Runbook: "coupons via http".

## Decisions already made
- Egress guard is a hard dependency. Secrets by vault name only. At-least-once with an idempotency key, not exactly-once.
