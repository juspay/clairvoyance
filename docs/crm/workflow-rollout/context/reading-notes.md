> **Status note (2026-09-02)**: these notes are the discussion record behind `docs/crm/workflow-rollout/`. One item discussed here was later DROPPED by decision and is not in the queue: the in-call WhatsApp `send_whatsapp` builtin (mentioned in §12, §14.6, §16.1). Treat every mention of it as historical.

> Reading notes for app/crm (all six modules) + the workflow layer (app/crm/outreach), migrations 048–061, the CI boundary guard and the workflow tests. Sections: 0 plumbing · 1 platform · 2 identity · 3 record · 4 outreach/workflow (with W-1..W-11 findings) · 5 migrations · 6 connectivity · 7 CI guard · 8 tests · 9 verification · 10 one-page model.

# CRM + workflow reading notes (branch claude/crm-workflow-review-k2gko2, HEAD 719d88f)

## 0. Layout / plumbing
- `app/crm/` modules: shared, platform, identity, record, connectivity, outreach (== the workflow layer; mounted at `/crm/workflows`).
- `app/crm/api.py` root router: identity→/customers, record.journey→/customers, record.ingest→/ingest, outreach→/workflows, connectivity→/connectors. Auth per-router, never blanket (webhook ingress needs no bearer).
- `app/crm/auth.py`: 4 doors. `crm_admin_user` (RBAC JWT + admin), `assert_merchant_access(user, merchant_id, op)` (admin passes; "*" wildcard; merchant_id from REQUEST not token; fail closed 403), `verify_s2s_merchant` (constant-time compare vs merchants.s2s_token then JWT verify; 404 on unknown merchant so non-enumerable), `verify_s2s_caller` (branch on what the token CLAIMS: wildcard/admin → check merchant exists (404), else defer to per-merchant token compare). Load-bearing: never "try relay, fall back", since rotated per-merchant tokens still have valid sigs.
- `app/crm/worker_main.py`: closed ROLES dict: event-worker (record.run_pass), dispatcher (connectivity claim_sends/dispatch_send, own batch dial CRM_DISPATCH_BATCH because batch×send-timeout < lease), walker (outreach claim_due_runs/walk_run). Registers `consume_attributed_event` as record consumer here (composition root; subscriber→record import direction). Refuses to start with pool ceiling < 2 (claim txn holds conn #1 for whole batch; contracts open conn #2).
- `shared/db.py`: `DbTxn = asyncpg.Connection`, `UniqueViolation`; `crm_connection()` (no txn, single statements), `crm_transaction()`, `atomically(fn,*a)` (ParamSpec-typed; the ONLY logic entry), `savepoint(txn)` for per-row isolation in batch atoms.
- `shared/decode.py`: TOTAL decoders `jsonb_object`, `jsonb_list`, `uuid_or_none` (never raise — a raising decoder strands a claimed batch forever). Note: driver returns jsonb as string today.
- `shared/normalize.py`: `normalize_phone` (E.164; bare 10-digit → +91 default; 0-prefixed 11 → +91; returns None if unparseable), `normalize_email` (lower/strip; needs "@").
- `shared/redact.py`: `mask_address(address, channel)` — channel picks the rule, unknown channel masks everything; `mask_digit_runs` for foreign text (≥7 digits).
- `shared/worker.py`: `run_drain_loop(claim, handle, interval, batch, stop_event, name)` — claim commits already; handle is per-row post-commit hook; per-row error isolation (log+skip); jittered exp backoff capped 5s; heartbeat; full batch → no sleep.

## 1. platform (platform_identity, T02) — the cross-merchant handles book
- contracts: `ensure_identities(handles)` (best-effort upsert, never raises), `is_suppressed(handles) -> bool` (FAIL CLOSED: error → True; no channel arg — probe is `bool_or(is_suppressed)` across (kind,value) pairs, so ANY live suppression on any channel blocks), `record_suppression(kind, value, channel, reason, source, evidence_ref, until)`.
- HANDLE_KINDS = phone/email only (igsid, shopify ids never enter the book). Normalizes before touching table; 048 CHECKs are backstop.
- `record_suppression` atom: ensure_identity → SELECT ... FOR UPDATE → pure `_merge_entry` (suppressions[channel] = entry; log.append) → UPDATE suppressions+log. `is_suppressed` column is derived by the 048 liveness trigger, never written here.
- `entry_is_live()` mirrors the trigger (until None/unparseable → live). NOTE: `_load_dict/_load_list` here are NOT total (raise on garbage) unlike shared/decode — acceptable since inside a single-row atom, not a batch.
- queries: `ensure_identity_query` ON CONFLICT (kind,value) DO UPDATE last_seen_at at most once/day.
- OBSERVATION: the resolved map is keyed by channel, but the gate probe ignores channel (is_suppressed is a single bool). A whatsapp-only STOP also blocks email/voice by construction (conservative — fine for now, flag for when channels multiply).

## 2. identity (crm_customer, T05)
- contracts: `resolve(merchant_id, handles, *, evidence="observed", source)` → customer_id; `assert_facts(merchant_id, customer_id, facts, evidence, source, confidence)`.
- HANDLE_COLUMNS probe order (the law): phone, email, igsid, shopify_customer_id, external_ref. Partial uniques `WHERE status='active'` per (merchant_id, handle).
- resolve: normalize (drop unparseable phone/email with len-only log; strip others) → `atomically(_resolve_in_txn)`: GATHER owners by probing each handle → DECIDE `plan_resolution` (0 owners → create; 1 → `plan_handle_writes` ladder; N → staple: survivor = oldest first_seen_at, tie lower uuid; losers → merged_away) → APPLY. UniqueViolation → retry once (index is the referee). Then `ensure_identities` best-effort.
- Evidence: RESOLVE_EVIDENCE = declared/observed/imported; `inferred` refused. Overwrite of occupied slot only for declared/observed; imported keeps existing; 049 trigger keeps replaced value in history.
- OBSERVATION (merge semantics): on a staple, only the INCOMING handles are written onto the survivor. A loser's other handles (e.g. its igsid) stay on the merged_away row and are no longer probe-able (probe filters status='active'). A later payload with just that igsid mints a NEW customer. The docstring says "their freed handles attach to the survivor" — code only attaches those present in the payload. Worth checking against ADR 0021 / canon.
- OBSERVATION: `_apply_resolution` logs the loser/survivor ids (uuids, not PII) — fine.
- facts.py: ladder declared(1.0) > observed(0.9) > imported(0.7) > inferred(0.4, cap 0.5). Claims appended to `attributes[attr]` list `{v,e,k,src,at}` only on drift vs LATEST claim. Winner = max(rank, at); materialized to display_name/primary_locale/timezone unless winner is inferred. Atom: SELECT attributes FOR UPDATE → append → UPDATE. Customer not found → logs error and returns (silent no-op, not raise).
- api: GET /crm/customers?merchant_id&q&limit&offset (admin), GET /crm/customers/{id}?merchant_id. `q` normalized to stored form for exact match (phone/email) + ILIKE on display_name (seq scan; pg_trgm later).
- db: only `apply_handles_query` writes handle columns (ADR 0021 lock #3). Column names come from HANDLE_COLUMNS only (asserted) so f-string is safe. `merge_customer_query` has `AND status='active'` so racing staplers converge.

## 3. record (crm_event_raw T13 + crm_journey_event view V01) — the spine
- contracts: `RawEvent`, `record_event` (buddy mirrors; fire-and-forget, returns None on failure), `get_customer_journey`, `customer_has_event(merchant, customer, topics, since)`.
- NOT exported: `workers.run_pass/observe_processed_event` (would form cycle with outreach) — worker_main takes them directly. `consumers.py` = registry LIST filled by worker_main (`register_consumer`, idempotent).
- api: `GET /crm/customers/{id}/journey?merchant_id&limit&before_started_at&before_id` (admin, keyset cursor on (started_at,id)); `POST /crm/ingest/events` (s2s via `verify_s2s_caller`, 1MB cap on Content-Length only, 503 on store failure = fail closed, 200 `duplicate=true` on dedupe).
- `EventIn`: extra=forbid (smuggled customer_id → 422), str_strip_whitespace, occurred_at must be tz-aware. Dedupe UNIQUE (merchant_id, source, external_id). occurred_at clamped LEAST($7, now()).
- Worker pass (`_pass_in_txn`): claim FOR UPDATE SKIP LOCKED (ORDER BY received_at) → per row `savepoint` → extractor (EXTRACTORS: lead-api/telephony → flat, shopify → shopify, default flat) → resolve(evidence=observed) → assert_facts (failure logged, never fails row) → consumers (entry rules) → stamp. Quarantine reasons: extractor_error, no_handle, unresolvable. resolve/assert_facts/consumers each commit on their own connection (why pool ceiling ≥ 2).
- OBSERVATION (poison row): crm_event_raw has NO attempt counter. A consumer that raises deterministically for one row leaves it pending forever; it is re-claimed every poll (it sits at the head, ORDER BY received_at) and re-runs resolve/assert_facts each time. "Costs one row per poll" is true but it never quarantines. Compare with enrollment.attempts. Candidate improvement: attempts column or quarantine after N.
- OBSERVATION: size guard checks only the Content-Length header; body is already parsed by FastAPI before the dep runs (EventIn is a dependency of verified_caller). Chunked bodies bypass. Minor.
- shopify extractor: phone from customer.phone → default_address.phone → payload.phone → shipping/billing.phone; email; shopify_customer_id; name never defaulted. Normalizes itself (resolve re-normalizes — harmless).
- `customer_has_event_query`: `COALESCE(occurred_at, received_at) > $4` on topic ANY($3) — index crm_event_raw_customer_ix (merchant, customer, occurred_at).
- Journey view 055 reads lead_call_tracker (call arm only), direction lowercased, WHERE customer_id IS NOT NULL. Registered in TABLE_OWNERS as record.
- Migration 051: deliberate NO partitioning (dedupe unique needs it); immutability trigger allows only processed_at/quarantine_reason/customer_id to change.

## 4. outreach == THE WORKFLOW LAYER (crm_workflow T19, crm_workflow_enrollment T20; /crm/workflows)
### Files
- `schemas.py`: `WorkflowDefinition{entry{topic, where{}, reenter=False, cooldown_hours=24, key?}, nodes[≥1], edges[[from,to] | [from,to,on]], goal{topics[≥1]}, exits{max_age_days=7 >0}, purpose_key?}`. Node types (Literal): wait(minutes) · send(channel, template) · call(template_id) · wait_event(topics, key, minutes). nodes[0] is the start. `outgoing()` = adjacency in doc order.
- `nodes.py`: NODE_TYPES registry {validate, execute, is_wait}. wait/wait_event: execute None, is_wait True. send → `queue_message` (connectivity contract; dedupe_key=`run:node`, source_kind="workflow", source_id=run.id, purpose_key from plan, variables=send_variables(context)). call → `create_lead_call_tracker` with deterministic lead id uuid5(`crm-workflow-lead:{run}:{node}`), status BACKLOG, TELEPHONY, meta {workflow_id, enrollment_id}, request_id = context.order_id|request_id|`wf-<run>`, then `update_lead_enrollment_id`. NodeParked = deterministic failure (park); anything else = transient (retry on lease). Context bookkeeping keys: source_event_id, phone, customer_mobile_number, lead_*/message_*/reply_* prefixes; `reporting_webhook_url` rides to lead only.
- `plans.py`: `validate_definition(raw, occupied_nodes, live_entry)` PURE: shape (pydantic) → dup node ids → per-type validate → edge endpoints exist → wait_event edges all labelled & unique labels; other nodes ≤1 unlabelled edge → entry unchanged while runs open → no occupied node removed. Lifecycle: create (draft; validated) → update_draft (validated) → publish (`_publish_in_txn`: read, occupied_nodes, validate, `apply_publish` = definition:=draft, draft:=NULL, version+1, draft→live) → set_status(live|paused|archived; archived terminal).
- `enrol.py`: ONLY creator of runs. `enrol(merchant, workflow, customer, context, enrollment_key)`: skip if not live; atom: source_event_used? → admission_facts (count runs, max entered_at per customer+workflow) → `_admission` PURE (runs>0 && !reenter → refuse; cooldown) → insert with current_node=nodes[0].id, wake_at=`_first_wake` (wait → now+minutes else now). UniqueViolation on open-run unique → None (normal).
- `entry.py`: the CONSUMER `consume_attributed_event(event, customer_id, handles)`: load live flows for merchant; classify: entry match (topic + where equality), goal match, listening wait_event nodes (topic in node.topics). Order: goal-cancel (time-aware: entered_at < occurred_at; NULL occurred_at ends all) → replies (`resume_run_on_event` writes context[reply_<node>] and wake_at=now for runs standing on that node) → enrol (context = scalar payload keys ≤256 chars + source_event_id + phone from extractor handles or payload fallback; enrollment_key from entry.key payload field, refused if missing).
- `walker.py`: `claim_due_runs` = one UPDATE (wake_at += lease, attempts+1, status waiting & due & workflow not paused, SKIP LOCKED). `walk_run`: archived/missing → exit ejected; paused/no def → snooze; `_advance`: max_age → timed_out; goal re-check via `customer_has_event(since=entered_at)` → goal_met; loop ≤10 steps: execute action node (context.update), `pick_next` (wait_event: edge `on == reply` else "timeout"; plain: first edge), None → exit completed (writes context); next is wait → `advance_run` (wake=now+minutes, attempts=0, last_error=NULL) and return; else continue. NodeParked → park; other exception → attempts ≥ CRM_WALKER_MAX_ATTEMPTS ? park : `record_run_error` with jittered exp backoff (cap 3600s).
- `runs.py`: list_runs, resume_run (parked → waiting, wake now, attempts 0), `run_retention_sweep_tick` (DELETE exited older than CRM_RUN_RETENTION_DAYS, batched). `workers.py`: claim callable also runs the sweep every CRM_RUN_SWEEP_INTERVAL_SECONDS.
- `api.py` (admin only, merchant_id query): POST "" create · GET "" list · GET /{id} · PUT /{id}/draft · POST /{id}/publish · POST /{id}/status · GET /{id}/runs?status · POST /{id}/runs/{run}/resume.
- db: 21 query builders. Notables: `exit_run_query` keeps context unless given (source_event_id must survive for replay idempotency); `cancel_open_runs` hits waiting+parked; `resume_run_on_event` only `status='waiting' AND current_node=$4`; `occupied_nodes` = status<>'exited'.
- Config knobs: CRM_WALKER_LEASE_SECONDS, CRM_WALKER_MAX_ATTEMPTS, CRM_RUN_RETENTION_DAYS, CRM_RUN_SWEEP_BATCH_SIZE, CRM_RUN_SWEEP_INTERVAL_SECONDS, CRM_WORKER_BATCH/INTERVAL/HEARTBEAT, CRM_DISPATCH_BATCH.

### Workflow observations / candidate findings
- W-1 (semantic bug, medium): a wait_event reply whose payload LACKS `node.key` is written as `{reply_<node>: None}` and wake_at=now. `pick_next` reads None → takes the "timeout" branch immediately. An event on the listened topic without the key thus ends the listening window early and mis-routes as timeout. Fix: skip the resume when answer is None (or write a sentinel and treat it as no-match).
- W-2 (lost update race, low-medium): `advance_run` overwrites `context` and `wake_at` unconditionally. If a reply (`resume_run_on_event`) lands while the walker is mid-visit on that wait_event node's timeout path, the walker's `advance_run` clobbers the reply and the timeout branch wins. Guard: `AND context = <as-claimed>`/version column, or `WHERE wake_at = <leased value>`.
- W-3 (500 instead of 422): `POST /{id}/status {live}` on a never-published draft violates CHECK (status='draft' OR definition NOT NULL) → asyncpg CheckViolation → 500. plans.set_status should refuse "live" without a definition (or catch and 422).
- W-4 (footgun): admission facts are per (workflow, customer), not per enrollment_key. A keyed plan (entry.key="order_id", the WISMO case) with default `reenter=False`/`cooldown_hours=24` refuses the second order. Keyed flows silently need reenter=True + cooldown 0; validator could enforce/warn, or admission could scope to the key.
- W-5 (validator gap): no check that a wait_event has a "timeout" edge, no reachability check (orphan nodes), cycles allowed (a stale `reply_<node>` in context makes a re-entered wait_event resolve instantly — see W-1 family). Also `live_entry` comparison is raw-dict equality: omitted defaults vs explicit defaults compare unequal → spurious "entry rule changed" on publish.
- W-6 (docstring overclaims): `_publish_in_txn` says the occupied read "must not race a walker" but READ COMMITTED + no lock on the workflow row does not prevent a token moving onto a removed node between the read and apply_publish. Consequence is an honest park ("node X not in live definition"), so low risk.
- W-7 (consistency nit): nodes.py says nobody matches type strings, but entry.py (`node.type == "wait_event"`) and walker.pick_next (`node.type != "wait_event"`) do. Could be a NodeSpec flag (`is_listener`/`branches`).
- W-8 (schema): crm_workflow_enrollment.customer_id has NO composite FK to crm_customer (crm_message and crm_workflow do pin tenancy via composite FK). A wrong merchant_id could file a run against another tenant's customer. Check canon T20.
- W-9 (context PII): `_context_from_payload` copies every scalar payload key ≤256 chars (email, name, addresses…) into `context` jsonb, which is then listed via GET runs and forwarded as send variables / lead payload. By design (template-variable bridge) but the run listing exposes it to any admin; note for the console.
- W-10: `execute_call` allows templates with merchant_id NULL (global). Confirm intended.
- W-11: goal-cancel in entry uses `entered_at < occurred_at`, walker re-check uses `COALESCE(occurred_at, received_at) > entered_at` — consistent for stamped events; for NULL occurred_at entry cancels ALL open runs while walker uses received_at. Fine but two definitions.
- Idempotency story is sound: source_event_used (context->>'source_event_id', kept on exit), open-run partial unique, dedupe_key run:node on manifest, uuid5 lead id, lease via wake_at, attempts++ by claim.

## 5. migrations (CRM era 048+)
- 048 platform_identity: kind CHECK (phone,email,device), E.164/lowercase CHECKs, `is_suppressed` recomputed by BEFORE INSERT/UPDATE trigger from suppressions jsonb (until NULL or future), suppression_log append-only trigger (prefix check).
- 049 crm_customer: partial uniques per handle WHERE status='active'; composite FK (merchant_id, merged_into_id) → same tenant; handle-history trigger appends `_handle_history` into attributes on replaced non-NULL handle. status ∈ active/merged_away/erased.
- 051 crm_event_raw (no partition; immutability trigger). 055 journey view. 056 crm_message (status ladder CHECK; dedupe (merchant, dedupe_key) total unique; provider_message_id global unique; immutability trigger, amended in 060 so binding_id is set-once). 057 crm_workflow. 058 crm_workflow_enrollment (exit_reason adds 'completed'; open-run unique (merchant, workflow, key) WHERE status<>'exited'; due index (wake_at,id) WHERE waiting; CHECK waiting→wake_at NOT NULL, exited→exit_reason NOT NULL). 059 lead_call_tracker.enrollment_id. 060 crm_connector_installation + crm_channel_binding (composite FKs; credential_id FK RESTRICT to legacy `credentials`; binding (channel,address) global unique WHERE not retired; one primary per (merchant, channel)). 061 crm_channel_template (natural key incl. provider_account_ref; provider_template_id global partial unique; no vocab CHECKs).

## 6. connectivity (crm_message T16, crm_connector_installation T11, crm_channel_binding T12, crm_channel_template T23; /crm/connectors)
- contracts: `claim_sends/dispatch_send` (dispatcher role), `queue_message(...)` (the producer door; the walker's send node uses it), `onboard/get_installation/list_installations/disconnect`, template family `create_template_draft/submit/edit/retire/get/list_templates`. `send()` and route resolution are deliberately OFF the surface.
- Registries (vocabulary in code): `channels.CHANNELS` {whatsapp: gate_handle_kind=phone, registers_templates=True} (unknown channel → gate fails CLOSED, registers_templates defaults True → refuse); `connectors.CONNECTORS` {whatsapp: ConnectorSpec(channel, onboarder, templates, request_model)}; `providers.ADAPTERS` (behind send.py only; rule 11). `reasons.py` one name per REASON_*; `status.py` open-set words + transition sets (INSTALLATION_USABLE = {healthy} only); `topics.py` template.* spine topics.
- queue.py: SOURCE_KINDS (broadcast, workflow, agent, transactional), PURPOSE_ROOTS (marketing, utility, transactional, authentication); `normalize_address` by channel's gate handle kind (unparseable → ValueError, never stored); insert ON CONFLICT (merchant_id, dedupe_key) DO NOTHING → None.
- dispatch.py: `claim_sends` = requeue stale 'sending' rows (claimed_at older than CRM_DISPATCH_STALE_MINUTES; dead if attempt ≥ max) then claim queued due rows (status→sending, attempt+1, SKIP LOCKED). `_dispatch_one`: `_gate` (suppression probe via platform `is_suppressed({kind: address})`, wait_for'd; unknown channel/timeout/raise → gate_unavailable, fail closed) → `send(mint_send_token(msg), msg)` → `plan_for_outcome` PURE (accepted / blocked terminal / failed non-retryable / dead at max / requeue with capped exp backoff + ±20% jitter) → `apply_outcome` CAS on (status='sending' AND attempt=claim generation). At-least-once by design.
- send.py: `token_grants` identity check; `resolve_send_route` binding (named or primary, active only) → installation (usable=healthy) → approved template on channel that registers (exactly ONE approved row by name; >1 language → refuse) → vault bundle (KMS decrypt last; DB error raises → retryable, AccountError → no_credential terminal). `send()` wraps resolve+deliver in ONE wait_for(CRM_MESSAGE_SEND_TIMEOUT_SECONDS) so a stalled pool can't outlive the lease; timeout/raise → failed retryable.
- accounts.py: single door policy (`healthy_installation`, `bundle_for`, AccountError) shared by send/templates/disconnect. Vault is legacy `credentials` table scoped by reseller_id (no merchant column) → credential NAME carries tenancy `connector:merchant:account`.
- onboarding.py: merchant exists → `_refuse_before_spending` (disabled door / retired endpoint, via `identify()` PURE) → `spec.onboarder.gather()` (provider calls, outside any txn) → store credential (rotate by name) → atom `_onboard_in_txn`: upsert installation (WHERE status<>'disabled'), refuse retired binding, upsert binding (first pipe becomes primary; is_primary only ever raised; status→active on conflict). UniqueViolation on primary race → 400 "try again". `disconnect`: best-effort provider revoke, then atom revoke installation + pause bindings (clearing is_primary — load-bearing for the partial unique).
- templates.py: create_draft (validated closed: channel registered + healthy installation; idempotent while draft), submit (claim draft→submitting atom → provider → record atom; failure releases claim only if provider_template_id IS NULL), edit (draft local; approved/rejected/paused → provider edit in place if `edits_in_place`, CAS on expected_status), retire (best-effort provider delete by name+hsm_id, then local 'deleted'). `approved_template()` = the send-time read. Webhook consumer (status/category/quality + crashed-submit resume) is NOT yet built (comment in db/queries/template.py).
- providers/base.py: ports ChannelAdapter (deliver; never raises for provider answers), ConnectorOnboarder (gather/identify/revoke), TemplateProvider (submit/edit/retire/normalize_event, edits_in_place); ProviderError base → messages pass through to merchants; anything else → fixed sentence.
- providers/meta/graph.py: single `call()` (status read first, body parsed second, throttle codes retryable, secrets never in query string, `segment()` URL-pins ids). whatsapp/: adapter (template-only sends; digits recipient; positional vs named params; refusals before the wire are 'blocked'), classify (RETRYABLE/TERMINAL/CREDENTIAL code sets), onboard (Embedded Signup: code → short token → long-lived (+expires_in → token_expires_at) → verify phone on WABA (paged) → subscribe; subscribe failure = degraded door, not refusal), templates (Meta SHOUTING → lowercase at this boundary; delete with hsm_id to avoid deleting all languages).
- api.py: merchant-facing via `get_current_user_with_rbac` + `assert_merchant_access` (NOT crm_admin_user). Routes: POST /{connector_key}/onboard, GET /installations, GET /installations/{id}, POST /installations/{id}/disconnect, POST/GET /templates, GET/PATCH /templates/{id}, POST /templates/{id}/submit|retire. pydantic errors returned with include_input=False (signup code never echoed).
- db: split per table (queries/accessors/decoders). All decoders TOTAL via shared/decode.

### Connectivity observations
- C-1: `_gate` probes suppression by the message's address only (not the customer's other handles) — fine, matches "STOP wrote this kind".
- C-2: T16 comment says nothing validates source_kind — now queue.py does (SOURCE_KINDS). Migration note is stale but harmless.
- C-3: onboarding stores credential BEFORE the atom; if the atom fails (e.g., disabled door discovered inside), the rotated credential persists — acceptable (idempotent by name), but a disabled door's token gets rotated by a merchant re-running signup. Minor.
- C-4: `INSTALLATION_USABLE = {healthy}`; `_STATUS_FOR_HEALTH['authenticated'] = degraded`. So a WABA whose subscribe call failed cannot send at all until re-onboarded. Deliberate (docstring), but note for ops.
- C-5: Template webhook consumer not built → templates stay 'pending' forever until it lands; sends refuse with template_not_approved. Known gap (documented).

## 7. CI guard `scripts/check_crm_boundaries.py` (12 rules)
1 table literal only in owner's db/; 2 SQL only in db/queries*; 3 asyncpg only in shared/db + db/; 4 import direction (crm↛app.ai; buddy → contracts only; app/database ↛ ai/crm; cross-module → contracts only; shared/auth/api exempt); 5 no driver calls on txn/conn in logic; 6 every crm_/platform_ CREATE TABLE in TABLE_OWNERS; 7 crm_transaction only in shared/db; 8 atomically callee named *_in_txn; 9 ATOMIC: docstring within 400 chars; 10 crm_connection never in logic; 11 provider faces: adapters/ADAPTERS ← send.py only, onboard/templates ← connectors.py only, base ← both, graph ← nowhere outside providers/; 12 record imports no subscriber module.
- NOTE: rule 4 lets outreach import `app.database.accessor` (crm → data layer is allowed; only the reverse is banned).

## 8. Workflow tests (tests/crm/test_workflow_*.py) — what's pinned
- plans: validator laws (dup ids, unknown edge nodes, >1 plain edge, per-type needs, shape errors, exits>0, cooldown≥0, occupied node deletion, entry change with open runs, wait_event labelled/distinct edges, only wait_event labels, send needs channel + purpose_key, entry.key vocabulary).
- nodes: Literal == NODE_TYPES keys; is_wait ⇔ execute None.
- admission: reenter/cooldown, first wake for wait/wait_event/action, context scalar filter, phone normalization in context.
- runs: resume SQL shape, sweep SQL, list SQL, unknown status, retry delay, bookkeeping filter, lead_request_id.
- queries: claim lease/attempts/skip paused, insert positional, goal-cancel shape + time-aware, live read merchant-scoped, publish requires draft, park/exit guards, exit keeps context, merchant-first reads, record error, goal recheck COALESCE.
- decoder: string jsonb.
- NOT pinned (gaps): `pick_next` semantics (W-1), `resume_run_on_event` with missing key, `_advance` loop, `walk_run` error ladder, `consume_attributed_event` ordering, `set_status('live')` on a draft (W-3), keyed-plan admission (W-4).

## 9. Verification done during the read
- `uv run python scripts/check_crm_boundaries.py` → OK (12 rules clean).
- `uv run pytest tests/crm` → 499 passed after `uv sync --extra dev` (pytest-asyncio was missing in the fresh container; the 121 "failures" before that were all "async def functions are not natively supported").
- Branch `claude/crm-workflow-review-k2gko2` == `origin/release` (no diff); working tree clean. Nothing committed.
- Repro'd with pure functions (no DB):
  - W-1 CONFIRMED: `pick_next(wait_event, [(confirm,YES),(call,timeout)], {"reply_ask": None})` → `call` (timeout branch). entry.py writes exactly `{reply_<node>: None}` when the payload lacks `node.key`, with wake_at=now → early, wrong branch.
  - W-4 CONFIRMED: keyed plan (`entry.key="order_id"`) with default reenter/cooldown, second order 5 min later → `(False, "reenter_disabled")`.
  - W-5 CONFIRMED: draft `{"topic": ...}` vs live entry with explicit defaults → "entry rule changed while runs are open" although semantically identical (raw-dict comparison).
- Not repro'd (need DB): W-2 (advance_run lost-update race), W-3 (CHECK violation → 500 on `status=live` for a never-published draft — read from 057 DDL + set_workflow_status_query), record poison-row loop.

## 10. One-page mental model of the workflow layer
```
producer (Shopify relay / buddy mirror / POST /crm/ingest/events)
   └─ crm_event_raw (store first, dedupe by (merchant, source, external_id))
        └─ event-worker pass: claim → extract → resolve() → assert_facts() → CONSUMERS → stamp
              └─ outreach.consume_attributed_event (per live plan of the merchant)
                    ├─ goal topic  → cancel_open_runs(goal_met, time-aware)
                    ├─ wait_event  → resume_run_on_event(context[reply_<node>], wake now)
                    └─ entry topic → enrol(): source_event_used? admission(reenter/cooldown) → INSERT run
                                       (open-run unique (merchant, workflow, key) WHERE not exited)
walker pod: claim_due_runs (wake_at lease push + attempts++) → walk_run
   ├─ archived → ejected · paused → snooze
   ├─ max_age → timed_out · customer_has_event(goal, since entered_at) → goal_met
   └─ loop ≤10: execute(node) [send → queue_message dedupe run:node | call → lead uuid5(run:node)]
                 pick_next (wait_event: on==reply else "timeout") → wait? advance_run(wake=now+min) : continue
                 no edge → exited completed ; NodeParked → parked (operator resume) ; other → backoff/park
dispatcher pod: requeue stale → claim queued → _gate(suppression, fail closed) → send() [route: binding→installation→approved template→vault] → adapter.deliver → plan_for_outcome → apply_outcome (CAS on attempt)
```

## 11. Tally — bugs vs probable issues vs nits

### Bugs (4) — wrong behaviour, reproduced or unambiguous from code + DDL
| # | Where | What |
|---|---|---|
| B1 (W-1) | outreach/entry.py + walker.pick_next | wait_event reply without `node.key` writes `{reply_<node>: None}` + wake now → walker takes the "timeout" edge early. Repro'd. |
| B2 (W-4) | outreach/enrol.py `_admission` | Keyed plans (entry.key, the documented WISMO case) are refused on the second key by the per-customer reenter/cooldown guard with default settings. Repro'd. |
| B3 (W-5) | outreach/plans.py `validate_definition` | Live-entry comparison is raw-dict equality; omitted vs explicit defaults → spurious "entry rule changed" publish refusal. Repro'd. |
| B4 (W-3) | outreach/plans.py `set_status` + 057 CHECK | `status=live` on a never-published draft violates `CHECK (status='draft' OR definition IS NOT NULL)` → asyncpg error → HTTP 500 instead of 422. From DDL + query; not run against a DB. |

### Probable issues (8) — plausible defects or design gaps that need a DB run or a canon/ADR check to confirm
| # | Where | What |
|---|---|---|
| P1 (W-2) | outreach/db advance_run vs resume_run_on_event | Unconditional overwrite of context/wake_at; a reply landing mid-visit can be clobbered by the timeout path. Race, needs a DB to demonstrate. |
| P2 | record/workers.py | No attempt counter on crm_event_raw: a deterministically failing consumer keeps the row pending forever and re-runs resolve/assert_facts every poll. |
| P3 | identity/resolve.py staple | Only incoming handles attach to the survivor; a loser's other handles are unreachable (probe filters status='active') and a later event re-mints a customer. Check ADR 0021 intent. |
| P4 (W-8) | migration 058 | crm_workflow_enrollment.customer_id has no composite FK to crm_customer, unlike crm_message/crm_workflow. Check canon T20. |
| P5 (W-6) | outreach/plans.py `_publish_in_txn` | Docstring claims the occupied-node read cannot race the walker; READ COMMITTED with no lock does not guarantee it. Consequence is an honest park, so low risk. |
| P6 | platform/suppression.py `is_suppressed` | Gate ignores channel; any live suppression on any channel blocks all channels. Conservative today, a design question once channels multiply. |
| P7 (W-9) | outreach/entry.py `_context_from_payload` | Every scalar payload key (email, name, address…) lands in run context, listed by GET runs and forwarded as send variables. By design as the template bridge; privacy exposure to review. |
| P8 | record/api.py `within_size_limit` | Cap checks Content-Length only, after FastAPI has already parsed the body; chunked bodies bypass. |

### Nits / known gaps (not counted): W-7 type-string matching in entry.py & pick_next; W-10 global (merchant NULL) templates allowed for call nodes; W-11 two definitions of "goal after entry" (entry vs walker); C-2 stale 056 comment about source_kind validation; C-3 credential rotated before the atom can refuse; C-4 authenticated→degraded cannot send (deliberate); C-5 template webhook consumer not built (documented).

## 12. The abandoned-cart flow and PR #1041 (repeat entries)

### The flow as asked (checkout abandonment → 30m → WhatsApp → 30m → call (+WhatsApp from the call) → 1d → close)
| Step | Construct | Status |
|---|---|---|
| Shopify abandonment event | `entry.topic` = the checkout topic the relay pushes; bursts coalesce via open-run unique + `source_event_used` | Works |
| Wait 30m; recovered by any means → nothing | `wait` node; `goal.topics` = order topics; entry consumer cancels open runs on the goal event, walker re-checks goal before every action | Works |
| Send WhatsApp | `send` node (channel, template) + plan `purpose_key` | Works (given connectors + approved template) |
| Wait 30m; recovered → `goal_met` | second `wait` | Works |
| Trigger call | `call` node (`template_id`) → BACKLOG lead | Works |
| WhatsApp **from inside the call** via functions | **Gap** — buddy has only tts_say / end_conversation / function(HTTP, builtin) / alert; no send-WhatsApp builtin and no `/crm/messages` route; `queue_message` is a Python contract only | Needs a `send_whatsapp` builtin (buddy → connectivity contracts, `source_kind: agent`, dedupe `lead:<id>:<fn>`) or a POST route |
| Wait 1d; recovered → `goal_met` else close | third `wait` with no outgoing edge → `completed` | Works |
- Plan settings to set deliberately: `reenter: true` + a `cooldown_hours`; phone must be extractable (email-only checkout parks at the send node); goal topic must match the relay string exactly; timers start at queue/push time, not delivery/call time; `exits.max_age_days` default 7 covers the flow.
- `entry.reenter` is fully implemented (schemas → `_admission` → atom → tests). Per customer, exited runs count; guard is per customer not per key (B2).

### The three follow-up questions
1. **All events for the customer into one run**: already true by refusal (unique + guard). Before #1041 later events were dropped, not applied: timer started at the FIRST update and the nudge carried the first snapshot → nudges a customer still on the checkout page. `wait_event` is not the fix (stale `reply_<node>` in context would loop a self-edge forever).
2. **Same-cart order mapping**: sending the cart link works today — `abandoned_checkout_url`, `token`, `cart_token` are scalars → context → template variables. Matching the order back is NOT built: goal matching is customer+topic only (`cancel_open_runs`, `customer_has_event`). Needs a `goal.key` (order `checkout_token` vs run's `token`) and a distinct exit reason.
3. **Different order with overlapping items**: already handled conservatively — ANY order by the customer ends every open run (`goal_met`, time-aware). Item-overlap logic is not expressible (`where` is equality on top-level keys; goal has no filter). If cart-keyed goals are added later, keep two tiers (exact match → recovered; any other order → still exit, different reason) or this case regresses into spam. Cross-run spam control = `reenter` + `cooldown_hours`; per-purpose frequency caps belong to the unbuilt permission gate (B5).

### PR #1041 — feat(crm): repeat entries (on_repeat + debounce_minutes), manas-narra, branch feat/crm-repeat-entries, base 75594cb
- Adds `entry.on_repeat` ∈ {ignore (default), refresh_latest, refresh_max(<field>), accumulate} and `entry.debounce_minutes`. New `repeat.py` (vocabulary, PURE `repeat_plan`, `apply_repeat`); `patch_open_run_query` = ONE idempotent UPDATE: `WHERE status='waiting' AND current_node = nodes[0].id AND enrollment_key=$3 AND NOT (repeat_event_ids ? event_id)`; merges/appends facts per policy, `wake_at = now() + debounce`, appends event id to `context.repeat_event_ids`. Hooked in `entry._try_enrol` when `enrol()` returns None. Validator: policy word must parse; debounce>0 needs `is_wait(nodes[0])`. `repeat_event_ids`/`repeat_items` are bookkeeping (dropped from run_facts); `repeat_count` is a fact. No migration, no API change. 16 tests (SQL shape + pure + monkeypatched hook; no DB).
- State (2026-09-02): open, 1 commit, CI green (build, Yama, SentinelOne), merges cleanly onto today's release (merge-base 9 commits behind; `git merge-tree` clean). CodeRabbit: 3 unresolved minor comments — (a) `accumulate` errors on `jsonb_array_length` if a payload carries a scalar `repeat_items` key; (b) `_as_number` accepts NaN/inf → always "wins" refresh_max; (c) repeat path passes `_context_from_payload(payload)` so refreshed `phone` is not carried (do NOT pass `source_event_id` though — see below).
- **Supersedes my `entry.refresh` sketch** — same shape (first node only, one UPDATE, per-event dedupe) with a richer vocabulary. Do not build a second version.
- **What it solves for the flow**: `on_repeat: refresh_latest` + `debounce_minutes: 30` (first node `wait 30`) = timer restarts on every checkout update and the nudge carries the latest cart. Use `refresh_max(cart_value)` only for several distinct carts.
- **What it does NOT solve**: in-call WhatsApp send; cart→order attribution (goal.key); bugs B1–B4; races P1/P2. It adds a second concurrent writer of `wake_at`/`context` beside the walker, so P1 (unconditional `advance_run` overwrite) gets slightly more likely.
- **Two asks on the PR before relying on it** (both one-liners in `patch_open_run_query`):
  - P9: exclude the founding event — a redelivered copy of the run's own `source_event_id` is refused by `source_event_used`, falls into `apply_repeat`, is NOT in `repeat_event_ids`, and so overwrites newer facts with the first snapshot and restarts the timer. Add `AND context->>'source_event_id' IS DISTINCT FROM $5` (or seed `repeat_event_ids` with the founding id at insert).
  - P10: `wake_at = now() + N` can pull the alarm EARLIER when debounce < remaining wait (author flagged it). Use `GREATEST(wake_at, now() + make_interval(...))` — a debounce may only extend the window.
- Author's open questions addressed to Swaroop in the PR comment: (1) list-shaped facts (`line_items`) never reach templates — producers owe a scalar summary, or a fire-time letter re-read, or a per-plan extractor? (2) `accumulate(<field>)` joining values into one scalar at fire time? (3) GREATEST vs now()+N for debounce (answer: GREATEST).

## 13. Loan-onboarding funnel (stage drop-off → call after 30m)
Requirements: customer-level; ~10 stage events (profile_created → kyc → bank_linked → offer → agreement → disbursed) plus unrelated events (order placed, refund); usually top-down but a stage event may be missing; drop-off at ANY stage → call after 30m.

### Option A — one small plan per stage (works today, + #1041)
- Plan k: `entry.topic = stage_k`, `reenter: true` + cooldown, `on_repeat: refresh_latest`, `debounce_minutes: 30`; nodes `wait 30 → call`; `goal.topics = every stage downstream of k`.
- Mechanics: goal-cancel runs before entry in the consumer, so one stage event closes run k (`goal_met`) and opens run k+1; walker re-checks goal before acting; skipped stages are harmless because goals list ALL downstream topics; unrelated topics are ignored; retries covered by reenter + debounce.
- Cost: N-1 plans; a journey = up to N-1 short runs each exiting `goal_met` ("progressed", not "converted"); funnel reporting = join runs across plans per customer.
### Option B — one plan, a chain of `wait_event` nodes (one run per journey)
- Each stage node listens for all downstream stage topics (30m), a labelled edge per stage jumps to the right node, `timeout → call_k → listen_k_after_call (1d) → timeout → completed`. Distinct node ids after the call (no cycles).
- **Blocked today by**: B1 (event lacking the key → timeout → false call); `wait_event` branches on `payload[key]`, not the topic (needs `key: "$topic"` resolved from `event.topic` in entry.py); single-topic entry (a journey first seen at KYC never enrols — needs `entry` as a list of `{topic, start}`); the reply written by `resume_run_on_event` carries only `reply_<node>`, no event facts (later stages' facts never reach call templates — needs a merge of scalars on resume, the refresh_latest shape); stale `reply_<node>` on any cycle (clear reply keys on advance); `exits.max_age_days` must cover the whole onboarding; O(n²) edges (console-generated).
- Same for both: out-of-order delivery (goal cancel compares goal `occurred_at` vs run `entered_at`, so a late-delivered earlier stage can still get a call — compare vs the entry event's `occurred_at` instead); a phone must be extractable; the call obeys buddy calling hours/DND; two concurrent applications → key by application id (B2).
### Verdict
Start with A (no code changes beyond #1041). B earns its keep only when per-journey funnel analytics or a single editable document matters, and it needs five small features plus the B1 fix first. A hybrid — A for behaviour + a reporting view joining a customer's runs across the stage plans in workflow order — gives most of B's analytics with none of its risk (one big plan's state machine parks every journey on a bug; entry edits are blocked while any run is open, and B's runs live for days).

## 14. Option B written out, why not B (plain-language version), industry practice, and the canon decision underneath

### 14.1 Option B, once B1 + four features exist (four stages shown; ten stages ≈ 30 nodes / 80 edges — console-generated only)
Prerequisites beyond the B1 fix: (1) `key: "$topic"` on wait_event → branch on `event.topic`; (2) `entry` as a list of `{topic, start}` so a journey first seen at KYC enrols on the KYC node; (3) `resume_run_on_event` merges the event's scalars (refresh_latest shape) so later stages' facts reach call templates; (4) clear `reply_<node>` keys on advance so a revisited node cannot resolve on a stale answer.
```json
{
  "entry": [
    {"topic": "loan.profile_created", "start": "at-profile"},
    {"topic": "loan.kyc_completed",   "start": "at-kyc"},
    {"topic": "loan.bank_linked",     "start": "at-bank"},
    {"topic": "loan.offer_accepted",  "start": "at-offer"}
  ],
  "reenter": true, "cooldown_hours": 0,
  "goal": {"topics": ["loan.disbursed"]},
  "exits": {"max_age_days": 30},
  "nodes": [
    {"id": "at-profile", "type": "wait_event", "key": "$topic", "minutes": 30,
     "topics": ["loan.kyc_completed", "loan.bank_linked", "loan.offer_accepted"]},
    {"id": "call-profile", "type": "call", "template_id": "<profile dropoff>"},
    {"id": "after-call-profile", "type": "wait_event", "key": "$topic", "minutes": 1440,
     "topics": ["loan.kyc_completed", "loan.bank_linked", "loan.offer_accepted"]},
    {"id": "at-kyc", "type": "wait_event", "key": "$topic", "minutes": 30,
     "topics": ["loan.bank_linked", "loan.offer_accepted"]},
    {"id": "call-kyc", "type": "call", "template_id": "<kyc dropoff>"},
    {"id": "after-call-kyc", "type": "wait_event", "key": "$topic", "minutes": 1440,
     "topics": ["loan.bank_linked", "loan.offer_accepted"]},
    {"id": "at-bank", "type": "wait_event", "key": "$topic", "minutes": 30,
     "topics": ["loan.offer_accepted"]},
    {"id": "call-bank", "type": "call", "template_id": "<bank dropoff>"},
    {"id": "after-call-bank", "type": "wait_event", "key": "$topic", "minutes": 1440,
     "topics": ["loan.offer_accepted"]},
    {"id": "at-offer", "type": "wait", "minutes": 30},
    {"id": "call-offer", "type": "call", "template_id": "<offer dropoff>"}
  ],
  "edges": [
    ["at-profile", "at-kyc", "loan.kyc_completed"], ["at-profile", "at-bank", "loan.bank_linked"],
    ["at-profile", "at-offer", "loan.offer_accepted"], ["at-profile", "call-profile", "timeout"],
    ["call-profile", "after-call-profile"],
    ["after-call-profile", "at-kyc", "loan.kyc_completed"], ["after-call-profile", "at-bank", "loan.bank_linked"],
    ["after-call-profile", "at-offer", "loan.offer_accepted"],
    ["at-kyc", "at-bank", "loan.bank_linked"], ["at-kyc", "at-offer", "loan.offer_accepted"],
    ["at-kyc", "call-kyc", "timeout"], ["call-kyc", "after-call-kyc"],
    ["after-call-kyc", "at-bank", "loan.bank_linked"], ["after-call-kyc", "at-offer", "loan.offer_accepted"],
    ["at-bank", "at-offer", "loan.offer_accepted"], ["at-bank", "call-bank", "timeout"],
    ["call-bank", "after-call-bank"], ["after-call-bank", "at-offer", "loan.offer_accepted"],
    ["at-offer", "call-offer"]
  ]
}
```
Walk-through: profile event enrols on `at-profile` (30m alarm). KYC event within the window → labelled edge → `at-kyc`, alarm restarts. Skipped stage = longer jump on another labelled edge. Silence → `timeout` → call → 1-day listening window → silence → `completed` (the drop-off record). `loan.disbursed` ends the run anywhere as `goal_met`. Post-call nodes have distinct ids so the graph stays acyclic.

### 14.2 A vs B comparison
| | A: plan per stage | B: one journey plan |
|---|---|---|
| Works today | Yes (+ #1041) | No — B1 + four features |
| Runs per journey | up to N-1 short runs | one |
| Funnel analytics | join runs across plans per customer | read one run (current node, exit reason, time per stage) |
| Call template variables | that stage's event facts | only with feature 3; risk of cross-stage key collisions |
| Editing while live | pause one stage plan | entry edits blocked while any run is open; runs live for weeks |
| Blast radius of a bug | one stage's 30-minute run | every journey in flight (a parked run stops being covered) |
| Write contention | short-lived rows, rarely concurrent | one hot row per customer → P1 race more likely |
| Document | N tiny plans | one large generated document; one missing edge = a wrong call |
| New vocabulary | none | five words, each an ADR + validator rule + test, forever |
Both call the same customer at the same minute. B wins only on reporting/product surface.

### 14.3 Why not B even after the fixes (the kid version — Ravi's loan)
Ravi climbs five steps: profile → KYC → bank → offer → money. Stop 30 minutes on any step and we phone him.
- **A = five small alarm clocks, one per step.** Finish profile → profile clock starts. Finish KYC in time → profile clock thrown away, KYC clock starts. Go quiet → the clock rings, we call. Each clock only knows "did he move past my step?"
- **B = one board game, Ravi is the token.** One board with all five squares; the token moves when he does a step; sit still 30 minutes and the square rings.
Both ring at the same moment. Why still the clocks:
1. **You cannot redraw a board while people stand on it.** Rules: entry can't change and occupied squares can't be removed while tokens are on the board. B's tokens sit for weeks, so someone is on nearly every square; adding "upload salary slip" means wiping every token (archive → ejected) or running two boards. Clocks run 30 minutes, so they are almost always empty and free to change; adding a step = one more clock.
2. **One broken square parks the whole token.** Bank square's call template deleted → Ravi parked and invisible until a human resumes him; even finishing the bank step later isn't watched. With clocks only the bank clock is broken; the offer clock still starts when he accepts.
3. **One backpack collects everybody's stuff.** B's single context gets every stage's payload; `status`/`amount`/`reason` mean different things at KYC and offer, the last writer wins, the stage-five call reads the wrong one. Clocks: one small bag per step.
4. **Everyone scribbles on the same page.** In B every stage event, repeat patch and walker visit write Ravi's one row → the P1 lost-update race gets likelier. Clocks: separate pages, rarely written together.
5. **Lots of arrows; one missing arrow = a wrong phone call.** Ten steps ≈ 30 squares / 80 arrows; the validator checks shape, not "every square lists all downstream steps". Machine-drawn only.
6. **New rules kept forever.** Five vocabulary words vs. none.
Where the board is nicer: one token shows where Ravi is, time per step, where he gave up. That is a report, not a behaviour — buildable from the clocks with one query.
What would change the verdict: the console generates boards; per-journey state becomes a merchant-facing feature; or clocks sprawl (10 plans × many lenders) becomes the maintenance problem.

### 14.4 What the industry does (from training knowledge, not checked against current docs)
- **The product merchants buy is a board**: Braze Canvas, SFMC Journey Builder, Iterable, Customer.io, Klaviyo Flows, MoEngage, CleverTap, WebEngage, HubSpot workflows, Dittofeed (cited in migration 057). Visual journey, customer as token: trigger · wait · wait-for-event with branches · action · exit.
- **Two things keep their boards safe**: (a) **version pinning** — an edit creates vN+1; people already inside finish vN; new entrants take vN+1 (Braze, Journey Builder; Temporal/Step Functions do the same for code); (b) **exit criteria re-checked at every step** — Klaviyo abandoned-cart: trigger Started Checkout, every step re-checks "has not Placed Order since entering" (= the walker's goal re-check, already built here).
- **For funnels, enterprises mostly run clocks**: the source system writes a profile attribute/segment (`onboarding_stage = kyc_pending`); one small campaign per stage fires on segment entry and exits on segment exit (= Option A keyed on STATE, not events). Reasons: different teams own different stages and A/B test independently; ten-stage canvases with skip-ahead arrows become spaghetti; audit ("why did we call him") is simpler. Boards are used for short linear journeys (cart recovery, post-purchase, welcome series).

### 14.5 The canon decision underneath ("not what the repo's canon chose")
- **Repo (T19, migration 057, plans.py)**: ONE live `definition`; publish replaces it in place and every run not yet past the change follows the new document; `version` is an audit stamp ("she entered under v3"), explicitly "never an execution pin"; safety = the publish validator BLOCKS stranding edits (occupied node removed, entry changed while runs open). Edits reach everyone — a feature for 30-minute runs (fix a template name, every waiting run benefits).
- **Enterprise engines**: PIN instead of block — keep every version that still has someone on it, answer "which version is this run on" at every claim, tool for draining/migrating old versions. Nothing is blocked because nobody in flight sees the change.
- **Consequence**: the repo's choice fits short runs; a three-week loan board hits the validator on most meaningful edits (only exits: archive → eject all, or a second plan). Making long boards enterprise-grade is therefore not the five vocabulary features — it is an ADR-level decision on pinning in-flight runs to the definition they entered under (multiple live definitions, per-run version resolution, drain tooling). Neither choice is wrong; they fit different run lengths.

### 14.6 What we should do (recommendation)
1. **Abandoned cart**: build the board — short, fits the current design; `on_repeat: refresh_latest` + `debounce_minutes: 30` from #1041; still needs the in-call WhatsApp send and cart→order `goal.key`.
2. **Loan funnel**: clocks (Option A) now — no canon change, no new vocabulary. Borrow the industry twist: trigger on a stage attribute/segment from the source system rather than raw stage events, which also removes skipped-event and out-of-order worries. Add a reporting view joining a customer's runs across stage plans in stage order for funnel analytics.
3. **Long boards as a product later**: put version pinning in front of Swaroop as the prerequisite; once decided, the five B features are small. Keep the B vocabulary on the roadmap, not in the corpus, until then.
4. Regardless: fix B1 (missing key → timeout), B2 (keyed plans vs reenter), B3 (entry dict compare), B4 (500 on draft→live), P1 (CAS on advance_run), P2 (event attempt counter); land P9/P10 on #1041.

### 14.7 Decision: build the board + version pinning ("runs complete on the version they entered under") — does the loan funnel become a board?
- **What pinning changes**: removes the biggest structural objection (#1 — edits blocked while runs are open). The occupied-node and entry-change guards become unnecessary for in-flight safety (they only protect the CURRENT version's new entrants).
- **What pinning costs** (beyond storing old definitions): the walker resolves the run's pinned definition on every claim (a `crm_workflow_version` table or a jsonb history on crm_workflow); the entry consumer changes shape — today it iterates the merchant's LIVE flows once per event; with pinning, goal topics and wait_event listening must be evaluated per OPEN RUN against that run's pinned version, while entries use the latest version → iterate the customer's open runs + latest definitions, not flows; drain/migrate-forward tooling; retention of old versions while any run references them; template references in old versions must stay resolvable (retiring a call/WA template a pinned run still names = park).
- **The other objections and what closes each**: #2 parked journey stops being watched → let stage events move a PARKED run too (resume_run_on_event currently touches `waiting` only) or park the action, not the listening; #3 context pollution → namespace facts per node (`facts.<node>`) in feature 3; #4 one hot row → the P1 CAS fix (generation/attempt guard on advance_run and every patch); #5 O(n²) edges → authoring sugar: an ordered `stages` list expanded into the wait_event ladder (validator-owned expansion), or `on: "$topic"` edges implied by node naming; #6 vocabulary → accepted cost once boards are the product.
- **Verdict with pinning + those five**: yes, the loan funnel can be a board, and one operating model (boards) beats two (boards + clocks). The remaining preference for clocks in enterprises is organisational (per-stage ownership, A/B), not technical — for a lender integrating via API, one pinned board is fine.
- **Sequencing**: pinning touches storage, walker, entry consumer and canon — weeks, not days. Ship the loan funnel as clocks NOW (five tiny plans, disposable), build pinning + the five items, then migrate the funnel to one board. Nothing is wasted: the stage topics, call templates and goal lists carry over one-to-one.

## 15. End-to-end plan (agreed direction) and the "boards everywhere?" answer

### 15.1 The plan, in order
**Phase 0 — hygiene (any time, small, independent)**
- Fix B1 (missing reply key → skip resume, never route to timeout), B2 (admission per enrollment_key when the plan is keyed, or validator warns), B3 (compare entries via model_dump, not raw dicts), B4 (refuse `live` without a definition → 422).
- P1: compare-and-set on every enrollment write (advance_run, resume_run_on_event, #1041 patch, record_run_error) — a `revision` column or the leased `wake_at` as the guard.
- P2: attempt counter / quarantine after N for crm_event_raw.
- Land #1041 with P9 (exclude founding `source_event_id`) and P10 (`GREATEST(wake_at, now()+N)`), plus the three CodeRabbit nits.

**Phase 1 — the abandoned-cart board (short journey; current live-document semantics are right for it)**
- Plan: entry `checkouts/update` + `reenter: true` + cooldown + `on_repeat: refresh_latest` + `debounce_minutes: 30`; `wait 30 → send WA → wait 30 → call → wait 1d`; goal = order topics; `abandoned_checkout_url` as a template variable.
- Build: `send_whatsapp` builtin in buddy (buddy → connectivity contracts, `source_kind: agent`, dedupe `lead:<id>:<fn>`) for the in-call send. `goal.key` (order `checkout_token` vs run `token`) with a distinct exit reason ("recovered" vs "converted elsewhere"); keep the customer-level goal as the second tier so an unrelated order still stops the nudge.
- Ship the loan funnel as CLOCKS in parallel (five/nine tiny stage plans; ideally triggered on a stage attribute/segment from the source system). Disposable; nothing wasted.

**Phase 2 — version pinning (ADR-level; touches storage, walker, entry consumer, canon)**
- Storage: `crm_workflow_version` (merchant_id, workflow_id, version, definition, published_at, published_by) or a jsonb history; runs already carry `workflow_version` — it becomes the execution pin. Old versions retained while any non-exited run references them; retirement guard on templates a pinned version names.
- Walker: resolve the run's pinned definition on every claim (cache by (workflow_id, version)).
- Entry consumer redesign: entries evaluate the LATEST version; goal topics and wait_event listening evaluate per OPEN RUN against that run's pinned version → iterate the customer's open runs + the merchant's latest definitions, not flows alone.
- Publish semantics per plan: `on_publish: pin` (default; new entrants take vN+1, in-flight finish vN) or `on_publish: migrate` (today's "edits reach everyone", allowed only when the stranding validator passes — keeps the short-board benefit of fixing a template name for every waiting run). Braze offers the same choice.
- Tooling: drain/migrate-forward (move waiting runs from vN to vN+1 when their current node still exists), per-version run counts on the list endpoint (#1053 is adding run counts — align).

**Phase 3 — long-board vocabulary (small once pinning exists)**
- `key: "$topic"` (branch on topic); `entry` as a list of `{topic, start}`; facts merged on resume, namespaced per node (`facts.<node>`); clear `reply_<node>` on advance; events may move a PARKED run (or park the action, keep listening); `stages` authoring sugar expanded into the wait_event ladder by the validator.
- Then migrate the loan funnel to one pinned board; retire the stage clocks.

### 15.2 Once all of it is built: boards everywhere, or do clocks survive?
- **There is only one engine.** A "clock" is a board with one wait and one action: same `crm_workflow` row, same walker, same enrol(). Clocks are a PATTERN (small plan, short run), not a second mechanism. After Phase 2/3 nothing is "clock code" that could be deleted.
- **Long boards become the default for journeys with state that matters**: cart recovery, onboarding funnels, WISMO, post-purchase sequences — anything where "where is this customer in the journey" is a product fact or a report.
- **Small boards (clocks) remain the right shape for**:
  1. One-shot / transactional flows — order confirmation → send; OTP; a single reminder. A one-node board.
  2. Cross-cutting nudges that fire regardless of where the customer is in another journey — "payment failed → retry call", "refund initiated → WA update". These must be separate plans anyway: the open-run unique is per workflow, and one customer can legitimately be in the cart board and the loan board at once.
  3. State/segment-triggered nudges from the source system ("stage attribute changed to X") when the source owns the funnel position and we only react.
  4. Independently owned or A/B-tested steps, where teams want to pause/edit one step without touching the journey.
  5. Very high-churn plans where "edits reach everyone" (`on_publish: migrate`) is the point and runs are minutes long.
- **The decision rule per plan**: run length × edit frequency. Runs of minutes-to-hours → small board, migrate-on-publish. Runs of days-to-weeks with position that matters → one pinned board. Never a long board on live-document semantics (the loan-funnel objection), never ten clocks for one journey once pinning exists (the reporting-join tax).
- **Merchant-facing framing**: the console shows one concept ("workflow"), with a template gallery: "Cart recovery" (a board), "Order confirmation" (a one-step board), "Onboarding funnel" (a stages ladder). Nobody outside engineering should hear the words clock or board.

### 15.3 "Once versioning is done, there is only one system" — yes, with the precise meaning
- **One engine, always**: one table pair (crm_workflow + crm_workflow_enrollment, plus the version table), one walker, one enrol(), one entry consumer, one validator, one API. This is already true today — clocks and boards are the same `crm_workflow` mechanism differing only in document size and run length. Versioning does not merge two systems; it removes the reason to keep long journeys OUT of the one system.
- **What varies per plan, not per system**: document size (one node or thirty); `on_publish: pin | migrate`; entry words (reenter, cooldown, key, on_repeat, debounce); goal tiers; exits. All are words in the definition jsonb, read by the same code.
- **The one place the code must know about both**: the entry consumer evaluates entries against the LATEST version and goals/listening against each open run's PINNED version. That is one consumer with two reads, not two consumers. A `migrate`-mode plan simply has every open run pinned to the latest version, so the same code path serves it with no branch.
- **What disappears once pinning exists**: the occupied-node and entry-change publish guards as blockers (they survive only as the precondition for `migrate` mode); the need to archive-and-eject to change a long plan; the reporting join across stage clocks for one journey; the "clock vs board" vocabulary itself outside engineering.
- **What never disappears**: small plans as a pattern (one-shot sends, cross-cutting nudges, source-owned stage triggers) — they are boards too, just short ones, and they run through pinning like everything else (a one-node board pinned to v3 is trivially fine).
- **Sanity check for the corpus**: after Phase 2 the canon sentence "version is an audit stamp, never an execution pin" (057) is reversed and needs an ADR; T19's "edits reach everyone not yet past them" becomes the definition of `migrate` mode, not the law.

## 16. Both flows in the final vocabulary (after Phases 0–3), and what would still be missing

Assumed vocabulary after the plan: entry as object or list of `{topic, start}`; `on_publish: pin | migrate`; `goals` as a list of tiers `{topics, key?, exit_reason}` (key = `{event: <payload field>, run: <context field>}`); nodes wait · send · call · wait_event (`key: "$topic"` allowed); `stages` ladder sugar expanded by the validator; facts merged on resume, namespaced per node; `current_stage` exposed in run_facts.

### 16.1 Cart abandonment — final shape
```json
{
  "name": "cart-recovery",
  "on_publish": "migrate",
  "purpose_key": "marketing.cart.recovery",
  "entry": {"topic": "checkouts/update", "reenter": true, "cooldown_hours": 24,
            "on_repeat": "refresh_latest", "debounce_minutes": 30},
  "goals": [
    {"topics": ["orders/create", "orders/paid"],
     "key": {"event": "cart_token", "run": "cart_token"}, "exit_reason": "goal_met"},
    {"topics": ["orders/create", "orders/paid"], "exit_reason": "converted_elsewhere"}
  ],
  "exits": {"max_age_days": 7},
  "nodes": [
    {"id": "wait-30m",   "type": "wait", "minutes": 30},
    {"id": "wa-nudge",   "type": "send", "channel": "whatsapp", "template": "cart_recovery_1"},
    {"id": "wait-30m-2", "type": "wait", "minutes": 30},
    {"id": "rescue-call","type": "call", "template_id": "<cart rescue template — uses the send_whatsapp builtin mid-call>"},
    {"id": "wait-1d",    "type": "wait", "minutes": 1440}
  ],
  "edges": [["wait-30m","wa-nudge"],["wa-nudge","wait-30m-2"],["wait-30m-2","rescue-call"],["rescue-call","wait-1d"]]
}
```
- `migrate` on publish: runs are ≤ 1 day, fixing a template name should reach every waiting run; the stranding validator still guards it.
- `cart_token` rather than checkout `token` for the goal key: Shopify mints a new checkout token when a customer re-enters checkout, the cart token is stabler. Confirm against the relay's payload.
- The mid-call WhatsApp is a buddy template function (`send_whatsapp` builtin → `queue_message`, `source_kind: agent`), not a workflow node — the workflow only sees the call.

### 16.2 Loan onboarding — final shape (stages ladder)
```json
{
  "name": "loan-onboarding-dropoff",
  "on_publish": "pin",
  "key": "application_id",
  "reenter": true, "cooldown_hours": 0,
  "exits": {"max_age_days": 30},
  "goals": [
    {"topics": ["loan.disbursed"], "exit_reason": "goal_met"},
    {"topics": ["loan.rejected", "loan.withdrawn"], "exit_reason": "withdrawn"}
  ],
  "stages": {
    "order": ["loan.profile_created", "loan.kyc_completed", "loan.bank_linked",
              "loan.offer_accepted", "loan.agreement_signed"],
    "idle_minutes": 30,
    "on_idle": {"type": "call", "template_id": "<stage-aware dropoff template>"},
    "after_action_minutes": 1440,
    "restart_on_repeat": true,
    "overrides": {
      "loan.offer_accepted": {"idle_minutes": 120}
    }
  }
}
```
Expansion (validator-owned): entry = every stage topic → its `at-<stage>` node; per stage `at-<stage>` wait_event(`$topic`, downstream topics, idle_minutes) → timeout → `<on_idle>-<stage>` → `after-<stage>` wait_event(downstream, after_action_minutes) → timeout → completed; labelled edges to every downstream `at-*`; `restart_on_repeat` = the stage's own topic re-arms the current node's alarm (the #1041 debounce generalised to any node). `current_stage` rides run_facts so one call template can say "you stopped at KYC".

### 16.3 Will they be perfect? No. What still needs building (functionality, not bugs)
| Gap | Affects | Why it matters | Where it lives |
|---|---|---|---|
| G1 Permission gate B5 (consent, purpose caps, quiet hours, cross-plan frequency caps) | both | Today the dispatcher only checks suppression; a marketing WA send has no consent check; a 30-min wait ending at 2am sends at 2am | dispatch `_gate` body + permission module (PR #1021 is the ledger) |
| G2 Send/call OUTCOME back into the run (delivered / failed / no-answer / busy) | both | No fallback channel, no "call didn't connect → WA", no retry ladder; `send` and `call` nodes have no outcome edges | receipts + inbound via #1040/#1052 onto the spine; then `wait_event` on `message.status` / `call.completed` with `key: outcome` (call.completed is already mirrored by crm_mirror, so the call half is nearly expressible) |
| G3 Inbound reply handling (STOP → suppression; "not interested" → skip the call; button replies) | cart mainly | Compliance and spam; the `wait_event` branch exists, the inbound letters do not yet | #1040 webhooks + #1052 extractor + a STOP → `record_suppression` consumer |
| G4 List-shaped facts (`line_items`) never reach templates | cart | The nudge cannot name the items (manas's open question on #1041) | producer scalar summary, or fire-time letter read via a record contract, or per-plan extractor |
| G5 Generic action nodes: `http` (create a discount code, hit the lender's API), `condition` (branch on a customer attribute / context fact), `split` (percentage / A-B) | both | Coupons, tiering ("KYC tier A gets a call, B gets WA"), experiments | new NODE_TYPES entries |
| G6 Human handoff / task node (assign to an agent, open a ticket) | loan | Escalation after two failed calls | new node type + wherever tasks live (assist data layer PR #963?) |
| G7 Out-of-order delivery guard (compare goal/listen against the entry event's `occurred_at`, not `entered_at`) | loan | Late-delivered earlier stage still gets a call | entry.py + walker re-check |
| G8 Same-stage repeat re-arms mid-board nodes (`restart_on_repeat`) | loan | KYC retried → timer should restart; #1041 covers the entry node only | generalise the #1041 patch to `current_node` |
| G9 Funnel/journey reporting (time per stage, drop-off by stage, recovered revenue with order amount captured at goal) | both | The reason boards were chosen; runs carry the data, no read yet | reporting view / endpoint (#1053 adds run counts — extend) |
| G10 Dry-run / simulate a plan against a sample event; plan-level test mode | both | Merchants publish blind today | validator + a `simulate` endpoint that walks the document without writing |
| G11 Send pacing / throughput budgets per merchant and channel (W8, named in channels.py) | cart at scale | A promo day queues 50k rows; Meta throttles | dispatcher + Channel fields |
| G12 Template-variable contract validation at publish (does `cart_recovery_1` exist, approved, and take these variables?) | both | Today failures surface at send time as `template_not_approved` / bad variables | publish validator consults the T23 registry via connectivity contracts |
Verdict: with Phases 0–3 the two flows RUN correctly end to end. G1–G3 are what make them shippable to real customers (compliance + feedback loops); G4–G6 and G9–G12 are what make them a product rather than a pipeline.
