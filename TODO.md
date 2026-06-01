# TODO

Living index of follow-ups for this repo. Append new items at the bottom of the relevant section. Remove rows when the underlying work lands. Last updated: 2026-05-21 (PR #778 buddy-assist widget rollout, rebased on PR #779 dispatcher rate-limit / number-unavailable fix).

---

## 1. Pipecat 1.0 — subtle risks worth knowing

These are not bugs in our code; they are semantic shifts introduced by the bump. Production keeps working, but ops/monitoring should be aware.

| # | Risk | Where it lives | Recommended action |
|---|---|---|---|
| 1 | **WebSocket reconnect failures are now non-fatal.** Before 1.0, exhausted retries on Soniox / Deepgram / ElevenLabs / Cartesia killed the pipeline (clean call-end). 1.0 emits an `ErrorFrame` instead and the pipeline keeps running with a dead service → silent dead-air. | Anywhere in the pipeline that uses a websocket-backed STT/TTS service. | Add an alert on `ErrorFrame` from STT/TTS providers (or wire `pipecat.processors.service_switcher.ServiceSwitcher` upstream of STT/TTS for primary→fallback failover). |
| 2 | **`group_parallel_tools=True` is the new pipecat default.** Multi-tool LLM responses now produce exactly one inference after the last call completes (was: one inference per result). | All flows that issue parallel function calls. | Watch the first multi-call template; if you need per-result LLM commentary back, set `group_parallel_tools=False` on the LLM service. |
| 3 | **Pipecat-flows raises `FlowError` / `InvalidFunctionError` instead of `ValueError`.** Previously bare `except ValueError` could swallow flow validation errors. | `app/ai/voice/agents/breeze_buddy/template/*.py`, anything that constructs `FlowsFunctionSchema` or calls `set_node_from_config`. | Don't `except ValueError` around flow-construction code; catch the specific flow exceptions instead. |
| 4 | **`mem0/memory.py` is unimportable under 1.0.** Still references removed `OpenAILLMContext` / `OpenAILLMContextFrame` / `LLMMessagesFrame`. The file is dead code (only call site is commented out at `automatic/__init__.py:57-58`), so prod is safe — but anyone reviving mem0 must port it first. | `app/ai/voice/agents/automatic/services/mem0/memory.py` | Either delete the file outright, or port it to `LLMContext` + `LLMMessagesUpdateFrame(run_llm=True)` if mem0 comes back. |
| 5 | **`LLMSpyProcessor.stt_mute_filter` constructor param is a dead branch.** Caller in `automatic/__init__.py:497` always passes `None`, but the conditional `_stt_mute_filter.process_frame(...)` block is still in `processors/llm_spy.py:211-213`. Cosmetic clutter; not load-bearing. | `app/ai/voice/agents/automatic/processors/llm_spy.py:146,153,211,213` | Drop the parameter and the dead branch in a small follow-up PR. |
| 6 | **DB-stored templates use `role: "system"` for `task_messages`.** Pipecat-flows 1.0 prefers `role: "developer"` for application instructions. Pipecat still accepts `"system"` (advisory, not a hard break), but the new role is semantically correct and helps providers that distinguish developer-tier instructions. | DB tables holding template JSON; `app/ai/voice/agents/breeze_buddy/examples/templates/*.json`. | Optional one-shot SQL: `task_messages` rows with `role: "system"` → `role: "developer"`. Verify against template authoring tools first. |

---

## 2. Open in-code TODOs

Direct grep of `# TODO` markers in `app/`. Sourced 2026-04-27.

| File:line | Note |
|---|---|
| `app/ai/voice/agents/breeze_buddy/managers/calls.py:584` | Advance `next_attempt_at` by the rate-limit window so the scheduler does not immediately re-pick a rate-limited lead and re-fire the alert on the next cycle. |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py:235` | Add a breeze-buddy-specific context summarizer. Pipecat 1.0 has `LLMContextSummarizer` building blocks but no out-of-the-box summarizer wired into telephony pipelines yet. Long conversations risk hitting model context limits. |
| `app/api/routers/breeze_buddy/telephony/answer/handlers.py:390` | Twilio block-response branch not yet implemented (currently returns a generic JSON fallback). Plivo/Exotel paths exist; Twilio needs the equivalent TwiML response. |
| `app/api/routers/breeze_buddy/telephony/answer/handlers.py:678` | Twilio block-handling for the answer flow not yet implemented (matches the gap above). |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` (`get_observers`) | Per-turn user→bot latency observability is currently OFF. Pipecat's `UserBotLatencyObserver` is VAD-coupled (`on_latency_measured` only fires on `VADUserStoppedSpeakingFrame`), and prod runs with `BREEZE_BUDDY_ENABLE_VAD=false`, so attaching it would emit only one greeting-latency event per call. Either enable VAD in prod, or write a VAD-free observer that measures `UserStoppedSpeakingFrame → BotStartedSpeakingFrame` and pushes the value onto the active OTEL span (so Langfuse sees it). |
| `app/ai/voice/llm/types.py` (`LLMConfiguration`) | Symmetric nested refactor — pull text-LLM fields (`provider`, `sdk`, `model`, `region`, `endpoint`, `api_key_name`, `temperature`, `max_tokens`, `thinking`) into a `TextLLMConfig` so `LLMConfiguration` becomes `text: Optional[TextLLMConfig]` + `realtime: Optional[RealtimeConfig]` + the shared `function_call_timeout_secs`. Cleaner schema (set exactly one of text/realtime; no meaningless `provider="azure"` on realtime-only templates), but touches every text-LLM caller (~10–20 sites in `app/ai/voice/agents/breeze_buddy/llm/__init__.py`, schemas, tests). Deferred from the direct-mode + S2S PR — for now `provider` is `Optional[LLMProvider] = None` and `get_llm_service` resolves it to Azure when unset. |
| `app/ai/voice/agents/breeze_buddy/agent/__init__.py` (Daily greeting latency) | Daily-mode time-to-first-audio is dominated by Daily WebRTC handshake + pipecat lifecycle (StartFrame → `transport.join()` → `on_client_connected`), not by our greeting code. Telephony beats Daily here because its WS is already open before the pipeline starts. To close the gap we'd need to optimise the spin-up itself: extend the Daily room pool (`app/helpers/automatic/daily_room_pool.py`) to keep a bot pre-joined with audio source ready, pre-warm the agent process so `pipeline.run()`/`transport.start()` is mostly cached, and possibly publish a custom Daily audio track via the `daily-python` SDK directly (bypass pipecat's transport for the first audio frame). Investigate when there's profiling evidence on real calls — micro-optimising the greeting fetch (~10–25 ms) is not worth the work without it. |

---

## 3. Pipecat 1.0 features adopted on 2026-04-27

Recorded here so reviewers have a single place to see what behavioural shifts we deliberately opted into.

- **Async function calls for HTTP global functions.** `GlobalHttpFunction.cancel_on_interruption` defaults to `False` (`app/ai/voice/agents/breeze_buddy/template/types.py:1037-1041`) — the LLM keeps the user engaged while a slow HTTP call runs; the result is injected back as a developer message that re-triggers inference. Builtins (`GlobalBuiltinFunction.cancel_on_interruption=True`, `types.py:1075-1079`) stay synchronous because warm-transfer / end-conversation are control-flow critical.
- **Anthropic Vertex prompt caching.** `claude_vertex.py:127-136` sets `enable_prompt_caching=True` on `AnthropicLLMSettings`. Long system prompts + per-template instructions get cached across turns — cuts Vertex Anthropic spend and time-to-first-token. (Note: replaces the removed `enable_prompt_caching_beta` flag.)

---

## 4. Pipecat 1.0 features available but not yet adopted

Lower-priority wins. Pull from here when next touching the relevant area.

- **Streaming intermediate function-call results** (`result_callback(..., is_final=False)`). Builds on the async function feature — useful for "I'm still checking..." UX during long HTTP calls. Plug-in point: `app/ai/voice/agents/breeze_buddy/handlers/transport/http_handler.py`.
- **`LLMMessagesTransformFrame`.** Replaces the racy "snapshot context, mutate, push update" pattern. Affects warm-transfer prep and end-conversation hooks. Refactor pass, not a P0.
- **`ServiceSwitcher` for STT/TTS failover.** Pairs with risk #1 above. Wraps a primary + fallback websocket service so a dead provider auto-fails over instead of silently dying.
- **ElevenLabs `pcm_32000` / `pcm_48000` sample rates.** Telephony stays on mu-law 8 kHz, so this only matters if Daily.co web ever needs higher fidelity. Low priority.
- **`LLMContext.get_messages(truncate_large_values=True)`.** Useful for emitting context snapshots into Langfuse/logs without binary blobs. Cosmetic.

---

## 5. Provider-specific follow-ups

Discovered during the 1.1.0 review pass. None of these are blockers; tracking so we don't lose context.

### Sarvam TTS — migrate to bulbul:v3, then delete the auto-detection wrapper

**Step 1 — switch default model to `bulbul:v3`.** Pipecat 1.1.0 already supports it as a first-class model option. Update `BB_VOICE_PROVIDER_DEFAULTS("sarvam").model` (Redis) and the per-template defaults. Caveats:
- V3 ignores `pitch` and `loudness` (pipecat warns and clamps; harmless).
- V3 `pace` range is 0.5–2.0 (V2 was 0.3–3.0). Existing pace values outside this clamp.
- Default sample rate is **24000 Hz** on V3 vs 16000 Hz on V2. Verify the telephony pipeline correctly resamples to 8kHz mu-law for outbound calls before flipping prod.
- V3 is in unlimited free preview until 2026-02-28 — good window for a soak.

**Step 2 — audit prod templates** for any conversation flows that produce cross-Indian-script LLM output (e.g., a single call where the LLM emits Telugu *and* Hindi). Hindi+English (Hinglish) does NOT count — V3 handles that natively in one request, so the wrapper would be redundant for those flows.

**Step 3 — delete `LanguageAwareSarvamTTS` + `detect_script` + `SCRIPT_RANGES` + `SCRIPT_TO_SARVAM_LANG`** from `app/ai/voice/tts/sarvam.py` (~115 lines, one file). Only safe if step 2 confirms zero cross-Indian-script flows. If step 2 finds even one, leave the wrapper — V3's text-normalization is still driven by the per-request `target_language_code` and would mangle off-script text.

Also worth fixing as a one-liner whether or not we delete the wrapper: `_switch_language_if_needed` assigns a bare `str` into `self._settings.language` after the first script switch, while `build_sarvam_tts` initializes it as a `Language` enum. Cosmetic type drift only — `Language` is a `StrEnum` so equality and JSON serialization both work — but if pipecat ever tightens the field type to `Language` only, this breaks. Fix at `app/ai/voice/tts/sarvam.py:161`: wrap the lookup in `Language(...)`.

### Twilio — `_get_available_number` treats transient IN_USE as permanent

PR #779 (2026-05-21) collapsed any `None` return from `_get_available_number` into a terminal `FINISHED + NUMBER_UNAVAILABLE` outcome plus a throttled P1 alert. Correct for the permanent cases (template's `outbound_number_id` deleted/disabled, empty unassigned-default fallback pool). **Wrong for Twilio.**

Twilio uses the DB `outbound_number.status` column as a binary 1-bit lock: `_acquire_number` flips `AVAILABLE → IN_USE` at call-start, `_release_number` flips back via the call-end webhook (could be minutes later). While a Twilio call is in flight, every concurrent dispatch for the same number sees `status != AVAILABLE` → returns `None` from `_get_available_number` → now gets marked `FINISHED` instead of the prior 10s defer.

Deterministic for any Twilio number with `maximum_channels >= 2`. At `maximum_channels = 1` the Redis channel-token semaphore (`dispatch/channel_semaphore.py`) mostly defends it (a narrow race window remains between gate-1 and gate-2). Plivo/Exotel are unaffected — they increment a counter on `_acquire_number` and don't touch the `status` column during normal calls, so `status` stays AVAILABLE throughout.

**Impact**: Twilio tenants with multi-channel numbers silently drop all-but-one of concurrent leads. Not observed in production yet — the 2026-05-21 incident that motivated PR #779 was Plivo/Exotel traffic only (Indian shop "Amir and Sons" via `BB_SHOPIFY`). Re-verify before any Twilio onboarding.

**Suggested fix** (in `app/ai/voice/agents/breeze_buddy/managers/calls.py`, `_get_available_number`): when the only reason for `None` is `status == IN_USE` on Twilio, log it and return the number anyway — let the Redis semaphore plus the worker's existing `_acquire_number`-failure branch (already defers 5s, `worker.py:413-421`) handle capacity. Alternatively distinguish "permanent" vs "transient capacity" via a sentinel return so the worker can defer for IN_USE and `_fail_and_release` for the rest. Also tighten the `raise_no_outbound_number` alert guidance not to fire for transient IN_USE.

**Defer-and-fix gate**: cheap SQL check first — `SELECT COUNT(*) FROM outbound_number WHERE provider='TWILIO' AND maximum_channels >= 2`. Empty → safe to leave as follow-up. Non-empty → fix before next deploy.

### Dispatch — atomic record bucket inflation on `make_call` failure

In `app/ai/voice/agents/breeze_buddy/services/call_limiter.py`, `record_outbound_call_attempt` runs the atomic ZADD **before** `provider.make_call` (intentional — strict cap requires rejecting before the customer's phone rings). If `make_call` then raises or returns no SID, the bucket has been incremented for a call that didn't reach the customer. Matches pre-PR semantics (pre-PR also counted attempts that didn't reach `make_call`) and only triggers on rare provider failures, so deferred from PR #779.

**Suggested fix**: on the `make_call`-failure branches in `worker.py:445-465` (exception and no-SID paths), call a new `undo_outbound_call_attempt(phone, lead_id)` helper that `ZREM`s the specific member `{now}:{lead_id}` from the bucket. ~10 lines in `call_limiter.py` + 1 test in `tests/breeze_buddy/dispatch/test_end_to_end.py`. Defer until production telemetry shows actual inflation (i.e., a phone hitting the limit when the operator can confirm the customer wasn't actually dialed that many times).

### Dispatcher comment drift — "record runs after make_call success"

PR #779 moved `record_outbound_call_attempt` from after `make_call` to before `make_call` (between `_acquire_number` success and `provider.make_call`) to restore the strict cap. Four comments/docstrings still describe the old placement:

| File:line | Stale text |
|---|---|
| `app/ai/voice/agents/breeze_buddy/dispatch/worker.py:351-352` | "The matching record_outbound_call_attempt() runs only after provider.make_call succeeds." |
| `tests/breeze_buddy/dispatch/test_end_to_end.py:104-105` | "record runs once (only after make_call success)" |
| `tests/breeze_buddy/dispatch/test_end_to_end.py:178-179` | "record (ZADD, runs only after provider.make_call succeeds)" |
| `tests/breeze_buddy/dispatch/test_end_to_end.py:332-333` | "only record does, and record runs only after a successful provider.make_call" |

Tests pass (they assert call counts, not order). Pure documentation drift. Trivial cleanup — fix next time the file is touched.

### Soniox STT — `SonioxSTTServiceWithEndpointDelay` is fragile

Our subclass at `app/ai/voice/stt/soniox/service.py` exists solely to inject `max_endpoint_delay_ms` into the WebSocket connection config. Pipecat 1.1.0 does not expose this field on `SonioxSTTSettings` and `_connect_websocket` does not forward `s.extra` either, so a subclass override is currently the only option. The override copies the entire body of pipecat's private `_connect_websocket` — any restructure upstream silently drops our injection.

**Action**: file an upstream PR adding `if isinstance(s.extra, dict): config.update(s.extra)` to `pipecat/services/soniox/stt.py:_connect_websocket` (mirrors what `DeepgramSTTSettings` already does via `_sync_extra_to_fields`). Once merged, our subclass goes away — we just pass `extra={"max_endpoint_delay_ms": ...}` to `SonioxSTTService.Settings`. Tiny PR, generally useful, lets us delete `app/ai/voice/stt/soniox/service.py` entirely.

---

## 6. Buddy-Assist widget rollout — PR #778 follow-ups (2026-05-21)

PR #778 (`feat(buddy-assist): widget public mode + SpecStream UI + UCP cutover`) shipped Phases 0–2 of the review fixes inline. Everything below was reviewed in `/tmp/pr-778-review-tracker.md`, triaged as "not a dealbreaker", and deferred here. Reference the tracker for original line numbers + agent rationale.

### 6.A Cross-repo coordination (do these immediately after #778 merges)

| # | What | Where | Why it can't ship in this PR |
|---|---|---|---|
| 1 | **Commit + push `static/buddy-assist-agent/canonical.template.json`** — flip from `"idempotency_key": "uuid_v4"` to `"idempotency_key": "idempotency_hash"` (already modified in working tree, file is still untracked in nautilus). | `nautilus/static/buddy-assist-agent/canonical.template.json` lines 51 + 60 | Nautilus repo, separate PR. The clairvoyance generator is in place (back-compat — still honors `uuid_v4`); B11 is **plumbed but inert** until the template flips. |
| 2 | **Re-provision Milton merchant** so the updated template lands in the DB. | `nautilus/scripts/provision-buddy-assist-merchant.sh` (also currently untracked) | Operational, post-template-merge. |
| 3 | **Smoke-test idempotency on Milton** (task #104, RR-1) — verify a retry of `create_cart`/`update_cart` with identical args produces an identical key and Shopify returns the cached result. | Manual / e2e harness | Depends on #1 + #2 above. |
| 4 | **Move UCP signing keys out of `/tmp/ucp-keys/`** (task #105, RR-2) into a stable production secret store (Cloud SQL Secret Manager, K8s secret, etc.). | Infrastructure / deploy config | Required before RFC 9421 signing lands (see 6.B/B10). Pre-existing concern, not introduced by #778. |
| 5 | **Consolidated e2e validation pass** on Milton after the rollout (task #106, RR-3): chat → voice → end → resume, cart flow end-to-end, cancel mid-stream, content_blocks/ui_blocks persisted + replayed. | Manual / Showcase harness | Post-deploy gate before declaring the rollout complete. |

### 6.B P0 — RFC 9421 HTTP signing (do this BEFORE Shopify flips enforcement on)

Currently UCP requests go out **unsigned**. Curl-probed Milton on 2026-05-21: `tools/call search_catalog` returns HTTP 200 with the catalog body, no `Signature-Input` required. The agent profile at `https://breezebuddy.ai/.well-known/ucp/agent.json` already advertises the public key (kid `hQuP1Q-e_SCrtOo4df-PoQBVy2Tm8Zi-8fZcLzT8bZg`) so the infrastructure side is ready — only the request-signing code never landed.

**What to ship**:
1. Add `httpsig` (or RFC-9421-compliant equivalent) to the direct-HTTP poster in `app/ai/voice/agents/breeze_buddy/mcp/__init__.py:_create_direct_http_tool_handler`. Sign `@method`, `@target-uri`, `content-digest`, `content-type`, plus a fresh `created` timestamp; ed25519 / ES256 to match the kid in the profile.
2. Load the private key from a stable location (see 6.A item #4). Cache the parsed key at process start; refuse to start if missing.
3. Bench: TLS-per-call + signing-per-call together — pair with B7 (connection pooling) so the perf hit doesn't compound.
4. Unit test against a `httpx.MockTransport` that verifies the signature parses.

**Trip-wire**: add a daily synthetic that POSTs unsigned and EXPECTS success. The day Shopify flips enforcement on, that synthetic starts failing and gives us a heads-up before real merchants see 401s.

### 6.C Connection-layer hardening (compounds with 6.B; ship together)

| # | What | Where |
|---|---|---|
| B7 | **Pool the `httpx.AsyncClient`.** Currently `async with httpx.AsyncClient(...)` per tool call → fresh TLS handshake every dispatch. Mirror the `MCPPool` pattern: one client per turn, closed in `close_mcp_pool` alongside the legacy MCP clients. | `mcp/__init__.py:_create_direct_http_tool_handler` |
| B9 | **One retry with backoff on 5xx + `httpx.TransportError`.** Shopify deploys cause expected 502 bursts. **Only safe to enable after B11/idempotency-hash is live in production** — without stable keys, retries create duplicates. | `mcp/__init__.py:_create_direct_http_tool_handler` |
| M21 | **Lift `inject_tool_args` into the channel-agnostic handler.** Voice mode UCP tool calls currently bypass `inject_tool_args` entirely — they get `_deep_merge_defaults` (static profile) but NO idempotency hash, NO state-driven cart_id injection. Architectural lift: handlers don't have per-call session context today; either thread `flow_manager.session_id` through, or move the injection invocation into the call site that has both. | `mcp/__init__.py:_create_direct_http_tool_handler` + `agent/__init__.py:1028` |

### 6.D UCP observability + tests

| # | What | Where |
|---|---|---|
| M22 | **Schema drift counter.** Emit a metric (`tool_call_status` keyed by `tool_name`) on every non-2xx UCP response so a Shopify-side schema change is visible before it pages. | `mcp/__init__.py` |
| M23 | **UCP response-transform error-path tests.** Current `tests/test_response_transform.py` covers happy paths only. Add `httpx.MockTransport` cases: 4xx body, 5xx body, empty body, non-JSON, missing `result.content`, malformed JSON inside `text` block. | `tests/test_response_transform.py` |
| M24 | **Same-key-across-retries idempotency test.** Existing `test_session_state.py:362` asserts a fresh value per call — flip the assertion to verify two identical inputs produce the SAME hash (the broken behaviour was enshrined by the test). Add a second test for "same args, different turn_id → different hash". | `tests/test_session_state.py` |
| M25 | **Surface JSON-RPC error fields.** Today an upstream JSON-RPC error returns `{"status":"error","data": json.dumps(err)}` — the LLM has to string-parse to find the code. Promote to discrete fields: `error_code`, `error_message`. | `mcp/__init__.py:262-267` |

### 6.E Chat-agent / cancel-bus robustness

| # | What | Where |
|---|---|---|
| M31 | **`_MAX_TOOL_CYCLES` bail path persists half-state.** If a turn hits the 8-cycle ceiling with `tool_calls` still pending, the loop bails and persists `tool_use` blocks WITHOUT matching `tool_result` rows → next turn's history replay is malformed and most LLM providers reject the conversation. Either skip the persist on the bail OR write synthetic `tool_result` error envelopes for the unmatched calls. | `chat/agent.py:444-457` |
| H2 | **TOCTOU on channel gating.** The widget `/message` route checks `current_channel == CHAT` BEFORE acquiring the per-session Redis lock; session can flip to VOICE between check and lock → chat turn lands mid-voice. Pass an `access_check` closure into `send_chat_message_handler` that re-checks under the lock. | `widget/handlers.py:241-243` |
| M30 | **Multi-cancel `uncancel()` loop.** CPython 3.11's `Task.cancel()` is counted — cross-pod cancel + client disconnect produces count >1 and a single `uncancel()` leaves the task still cancelling. Loop: `while current_task.cancelling(): current_task.uncancel()`. | `chat/handlers.py:482-488` |
| M33 | **Cleanup chaining can leak the MCP pool.** `run_turn` finally block: if `aiohttp_session.close()` raises, `close_mcp_pool()` is skipped → StreamableHTTP connection leak. Use `contextlib.AsyncExitStack` so each cleanup runs independently. | `chat/agent.py:157-162` |
| M27 | **`agent_session_state` race under slow LLM calls.** The per-session Redis lock has a 180s TTL; an LLM call that overruns the TTL allows the next turn to start, then both write `data_json` last-write-wins on the older turn. Add row-level `version` + CAS, OR document the TTL as load-bearing. | `template/session_state.py:160-205` |

### 6.F Anonymous-endpoint hardening

| # | What | Where |
|---|---|---|
| M5 | **Normalize `allowed_origins` matching.** Currently exact-string; `https://example.com` ≠ `https://example.com:443` ≠ `https://EXAMPLE.com`. Normalize on ingress + storage (lowercase host, strip default port); validate entries against a strict URL regex on admin write. | `widget_common.py:_caller_origin` + `widget_config/handlers.py` |
| M8 | **Validate client-supplied `template_vars` + `metadata` on `POST /widget/session`.** Currently passed through unchecked → prompt-injection vector if any template_var lands in a system-prompt slot, plus storage pollution (no size cap). Schema validation against the per-template `template_vars` declaration; cap `metadata` size (~4KB); disallow client-supplied `widget` key. | `widget/handlers.py:create_widget_session_handler` |
| M37 | **Prompt-injection via interpolation ordering.** `{{ui_primitives_section}}` is spliced AFTER template_vars are rendered → a malicious template_var value of `"{{ui_primitives_section}}"` can inject the catalog block in arbitrary positions. Render the primitives section FIRST, then template_vars with a strict key allowlist. Pair with M8. | `template/ui_prompt.py` + `template/builder.py:60-75` |
| M1 | **Widget JWT claims: add `aud`/`iss`/`jti`.** 24h TTL with no revocation means a compromised token is usable for a day. Add `aud="breeze-buddy/widget"` + `iss`, enforce on verify; add `jti` and a revoke-by-jti Redis set so ops can kill specific tokens. Consider shortening TTL with refresh. | `api/security/breeze_buddy/widget_token.py:80-88` |
| M2 | **RBAC `typ` allowlist (not denylist).** Today's verifier rejects `typ=widget` and `demo=true` — brittle because any new token type accidentally inherits RBAC. Mint RBAC tokens with `typ="rbac"`/`"s2s"`; verifier rejects anything else. Stage with a feature flag during back-fill. | `api/security/breeze_buddy/rbac_token.py:98-111` |
| M7 | **CORS preflight unit test.** Assert `Access-Control-Allow-Credentials: true` is NEVER paired with `Access-Control-Allow-Origin: *`. Current code is safe; the test is a guardrail against future drift. | `widget_common.py` + new test file |

### 6.G Database polish

| # | What | Where |
|---|---|---|
| M10 | **`CREATE INDEX CONCURRENTLY` for chat_message indexes.** Migration 030 creates indexes without `CONCURRENTLY` → write lock for build duration. Today the table is small (<100k rows) so the lock is sub-second; this becomes mandatory before the table grows past ~1M rows. | `database/migrations/030_widget_public_mode_and_session_persistence.sql:142-144, 188-189` (large-table indexes) |
| M11 | **Document down-migration policy.** No reversal path for migration 030. Confirm with infra whether "deploy-forward only" is the team rule; if rollback is supported, write the reverse. | `database/migrations/030_*` + new `docs/MIGRATIONS.md` |
| M13 | **`list_widget_configs_query` empty list semantics.** Empty `reseller_ids: List` produces `= ANY('{}')` → matches zero rows (silent "deny all"). Either treat empty as "no filter" or as "deny all" with an explicit comment + caller guarantee. | `database/queries/breeze_buddy/widget_config.py:115,119` |
| M14 | **`reset_widget_voice_lead_query` clobbers analytics fields.** Verify analytics pipeline reads `call_id`/`outcome`/`cost` from a snapshot table (not directly from `lead_call_tracker`); if the latter, this query loses per-attempt history on re-attach. | `database/queries/breeze_buddy/lead_call_tracker.py:687-703` |
| M16 | **`accessor/widget_config.py:list count_query values[:-2]`** assumes last 2 binds are LIMIT/OFFSET via implicit coupling. Match the defensive guard in `merchants.py:152`: `values[:-2] if len(values) > 2 else []`. | `database/accessor/breeze_buddy/widget_config.py:144` |

### 6.H SpecStream polish

| # | What | Where |
|---|---|---|
| M34 | **`replace` ops bypass catalog validation.** Healer rules + `primitive_disabled` only fire on `add`. Track `id → type` in extractor state; run the full validation pipeline (healer + props + allowlist) on `replace` too. Otherwise LLM can land arbitrary props on existing tree nodes. | `chat/ui_stream.py:255-275` + `chat/ui_healer.py:_rule_*` |
| M26 | **`apply_state_reducers` key allowlist.** Reducers write raw JMESPath results into `next_state` with no key filter — a malicious or buggy template reducer could overwrite security-relevant state keys other code trusts. Either declare reducer-allowed key shape or document that all state keys are LLM-influenced. Templates are internally authored so this is defense-in-depth. | `template/session_state.py:120-131` |
| M35 | `_CARRY_MAX` couples to the literal marker length — if `<ui_stream>` ever lengthens (e.g. v2 marker), partial markers across small chunks silently miss. Drop the `_CARRY_MAX` subtractor. | `chat/ui_stream.py:181-187` |
| M36 | `remove` op with unknown id passes through silently. Add a drop rule + `ui_op_dropped: reason=remove_unknown_id` telemetry so LLM hallucination is distinguishable from legitimate cross-turn remove. | `chat/ui_healer.py:182-198` + `chat/ui_stream.py:387-391` |
| M38 | **`ToAssistantAction.display` server-side XSS sanitization.** The widget is the sole defense today. Add a Pydantic field validator that strips `<script>` etc. so a buggy LLM payload doesn't depend on widget hygiene. | `template/ui_catalog.py:71-78` |
| M39 | **Cap `Message.msg` field length.** `min_length=1` but no max → 10MB LLM-emitted strings can round-trip through the API. Add `max_length=8192`. | `template/ui_catalog.py:68-78` |

### 6.I Cancel-bus polish

| # | What | Where |
|---|---|---|
| M28 | `cancel_bus.cancel()` returns 202 on Redis publish failure → 3-min wedge with no client telemetry. Add a counter on publish-failure. | `chat/cancel_bus.py:107-130` |
| M29 | Subscriber reconnect loop has no circuit-break → sustained Redis outage = `error` log spam at 2/min/pod forever. Downgrade to `warning` after first ~5 consecutive failures; drop `exc_info=True`. | `chat/cancel_bus.py:188-198` |
| M32 | Instance-level `_ui_extractor` + `_known_ui_ids` on `ChatAgent` are correct today (fresh agent per `/message`), but the implicit guarantee isn't enforced. Move into a `run_turn`-local so a future refactor to long-lived agent can't silently regress. | `chat/agent.py:108-116` |

### 6.J Minors / log hygiene / type tightening (do alongside whatever you're already touching)

- `widget_token.py:106` — log JWT exception **type only**, not message (PyJWT messages can include token fragments).
- `rbac_token.py:151-154` — drop `reseller_ids`/`merchant_ids` logging to `debug` (hot-path noise + scope leak surface).
- `agent/__init__.py:313-324` — tighten widget seed gate with an explicit `is_widget` check (today: any truthy `widget_session_id` triggers; copy-paste during debugging on a telephony lead would silently kill its greeting).
- `flow.py:268-279` — cap `prior_history` by token count before assembling resume seed (avoid 64k context blow-up on long widget sessions).
- `queries/widget_config.py:34-44` — `created_at`/`updated_at` are passed as bind values AND have DB `DEFAULT now()`. Pick one source (app clock drifts on skewed hosts).
- `decoder/widget_config.py:14` — `row.get("allowed_origins") or []` swallows falsy non-empty values. Use `if row.get(...) is not None else []`.
- `chat/agent.py:139` — `history: List[Dict[str, Any]]` is loosely typed (violates CLAUDE.md "no any types"). Re-export `LLMContextMessage` and use it.
- `chat/agent.py:618` — exception envelopes can leak SQL strings / internal paths. Sanitize before returning to the LLM context.
- `tool_result_normalizer.py` — file is a 63-LOC MCP envelope unwrapper, not "JIT UI instructions" as the brief suggested. Rename or update docstring + SCALE_ROADMAP.
- `WidgetVoiceEndResponse.status` (`schemas/breeze_buddy/chat.py:259-263`) — free `str`. Promote to `Enum` so clients can switch without string-matching.
- `decoder/widget_config.py:6` — untyped `row` param. Add `asyncpg.Record` annotation.
- Drop the duplicate "Money is absent" regression assertion (currently in BOTH `tests/test_ui_stream.py:202-211` and `tests/test_ui_catalog_groups.py:101-104`).
- `schemas/breeze_buddy/widget_config.py:5` — module docstring references "migration 029" but the table actually lands in migration 030. Trivial doc fix.
- `api/routers/breeze_buddy/widget/__init__.py:12` — docstring says "Six routes" but the router now also exposes `POST /widget/session/{id}/cancel`. Bump count or list explicitly.
- `tests/test_jit_instructions.py`, `tests/test_primitive_disabled.py`, `tests/test_response_transform.py` — test funcs missing `-> None`. Apply once across all three (and `test_ui_stream.py` summary tests landed without it too — pick all up in one sweep).
- `chat/agent.py:252` — loop counter `cycle` is unused inside the body. Rename to `_cycle` to silence the linter without behavior change.
- `chat/ui_stream.py:412-426` — `__all__` is in declaration order, not alphabetical. Sort once when something else touches the file.
- `template/types.py:935` — `tool_schemas: Optional[List[Dict[str, Any]]]` accepts any dict shape. Promote to a `ToolSchema(BaseModel)` with explicit `name: str`, `description: Optional[str]`, `properties: Dict[str, Any]`, `required: Optional[List[str]]`. A malformed template currently passes Pydantic validation and only blows up later with `KeyError` during tool-load. Defense-in-depth, not a present bug.
- `mcp/__init__.py:_maybe_inject_ui_instructions` (line ~190) — collapses `ToolUiTrigger.ON_SUCCESS` and `ON_ANY` into the same branch. The helper only runs on success paths, so `on_any` never fires on direct-HTTP error responses. Plumb an `is_success: bool` arg through the three call sites (lines ~317, ~390, ~410) plus the early-return error branch in `_create_direct_http_tool_handler` so `on_any` fires correctly. Latent — currently no template uses `on_any`, but it's a footgun for the next one.
