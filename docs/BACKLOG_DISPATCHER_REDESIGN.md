# Breeze Buddy Backlog Dispatcher — Final Plan

Status: **Implemented in PR #770.**
Owners: Breeze Buddy team.
Deep-dive reference: [docs/event-driven-dispatch/](./event-driven-dispatch/) (PR #722). This file is the decision log; that folder has the architecture, data-model, reliability catalog, migration phases, and operations runbooks.

---

## 1. Overview

A Redis-backed, event-driven replacement for the cron-based backlog picker. DB stays as the source of truth; Redis is the dispatch fabric. Five planes:

```text
Ingest  →  Schedule  →  Promote  →  Dispatch  →  Control
(HTTP)     (ZSET)      (leader)    (workers)    (reconcilers)
```

- **Throughput ceiling** = sum of `maximum_channels` across active telephony numbers. Not cron cadence, not worker count.
- **Dispatch latency** = promoter tick (200ms) + DB CAS + pre-checks + provider HTTP.
- **No hot-path DB polling.** Ingest writes an event; promoter reads Redis; workers read Redis.
- **Horizontally scalable.** Add pods → more workers → more throughput, up to the telephony ceiling.
- **Safe to lose Redis.** Reconcilers rebuild from `lead_call_tracker` within 60s.

## 2. Architecture

### Plane 1 — Ingest

`POST /leads` does `INSERT lead_call_tracker (status=BACKLOG)` then `ZADD bb:schedule:leads <next_attempt_at_ms ± jitter> <lead_id>`. Returns 201. If `ZADD` fails (Redis down), log and return 201 anyway — DB is authoritative; reconciler heals within 60s.

Jitter is uniform ±`BB_DISPATCH_QPS_JITTER_MS` (default 200ms), applied at every `ZADD` (ingest and retry). Smooths bursts so identical scheduled times don't all fire in the same millisecond.

#### Manual dispatch (operator endpoint)

`POST /leads/{lead_id}/dispatch-now` — force-schedule a specific BACKLOG lead to fire immediately. Useful for ops, debugging, manual retries.

Auth: reseller-scoped via `get_current_user_with_rbac`; admins can dispatch across resellers.

Behaviour:

1. Load lead; 404 if missing.
2. Authorise — `lead.reseller_id` must match the caller (or caller is admin).
3. Status guards:
   - `PROCESSING` → 409 *"lead is currently being dispatched"*
   - `FINISHED` → 400 *"lead is finished; create a new lead for re-attempt"* (re-creating is the caller's choice, not ours — preserves the analytics one-row-per-attempt contract).
   - `is_locked=TRUE` → 409 *"lead is locked by another dispatcher"*
   - `BACKLOG` → proceed.
4. `UPDATE lead_call_tracker SET next_attempt_at = NOW()` for the row.
5. `ZADD bb:schedule:leads <now_ms> {lead_id}` — **no jitter** (operator intent is "now, not 200ms-ish").
6. Returns `200 {status:"queued", expected_dispatch_within_ms: 200}`.

Goes through every normal guard (pre-checks, rate-limit, calling-hours, channel capacity, idempotency CAS) — no bypass code path. Idempotent: calling it twice is harmless (second `ZADD` overwrites; worker CAS prevents duplicate dispatch). If the dispatcher is paused or the reseller is paused, the lead just sits in the ZSET — operator sees the symptom on the next call.

Out of scope for this endpoint: skipping calling-hours, skipping rate-limit, skipping pre-checks. Those are correctness contracts, not scheduling preferences.

### Plane 2 — Schedule

| | |
|---|---|
| Key | `bb:schedule:leads` |
| Type | ZSET |
| Score | `next_attempt_at` as unix-ms |
| Member | `lead_id` |

Single global ZSET. Never sharded — Redis handles millions of members; sharding the time view multiplies leader-election complexity.

### Plane 3 — Promote

Single leader-elected promoter task runs in every pod; only the leader acts. Redlock on `bb:promoter:leader`, 5s TTL, renewed every 2s. Tick every 200ms:

```text
ids = ZRANGEBYSCORE bb:schedule:leads 0 now LIMIT 0 500
for id in ids:
  if ZREM bb:schedule:leads id == 1:
    LPUSH bb:ready:leads id
```

`ZREM`-returns-0 absorbs any split-brain during leader hand-off. Bounded batch caps work per tick so a 100k-lead surge drains at `500 × 5/sec = 2500/sec` instead of one blocking scan.

Single global ready list in v1. Sharding by reseller is **deferred** until production traffic shows tenant starvation; channel semaphores already provide per-number (≈per-tenant) isolation.

### Plane 4 — Dispatch

Workers run on every pod as long-lived asyncio tasks. Loop:

```text
lead_id = BLPOP bb:ready:leads timeout=30s
RPUSH bb:processing:leads:{worker_uuid} lead_id     # crash recovery
lead = SELECT FROM lead_call_tracker WHERE id = lead_id
if lead.status != BACKLOG: drop

if not acquire_lock_on_lead_by_id(lead_id, expected_status=BACKLOG): drop
if not pass_pre_checks(lead): finalize_with_precheck_failure; continue
if outside_calling_hours(lead): ZADD next_window; release_lock; continue
if rate_limited(lead.phone): ZADD now + window; release_lock; continue   # before channel
number = pick_telephony_number(lead)
if not number: ZADD now+10s; release_lock; continue

token = BLPOP bb:channel:{number.id} timeout=10s
if not token: ZADD now + jitter(1..3s); release_lock; continue

try:
  call_sid = provider.make_call(number, lead)
  UPDATE lead_call_tracker SET status='PROCESSING', call_id=call_sid
    WHERE id=lead_id AND status='BACKLOG'
  # token stays held; released by call-end webhook
except:
  LPUSH bb:channel:{number.id} token
  ZADD now + backoff(attempt_count); release_lock

LREM bb:processing:leads:{worker_uuid} 1 lead_id
```

**Channel semaphore.** Each telephony number has a Redis LIST `bb:channel:{id}` with `maximum_channels` interchangeable tokens. Workers `BLPOP` to acquire; webhook `LPUSH`es to release. Capacity is enforced by Redis, not by a DB counter — no row-lock contention on hot numbers.

Per-customer rate limit is checked **before** the channel `BLPOP` so a rate-limited lead never holds capacity unnecessarily.

### Plane 5 — Control

Four reconcilers registered on the existing `BackgroundTaskScheduler` (`app/core/background_tasks/scheduler.py`). The scheduler uses `SET NX EX` distributed locking — only one pod runs each reconciler per interval, across the whole fleet. **No new locking primitives needed.**

| Reconciler | Interval | Purpose |
|---|---|---|
| `reconcile_backlog_to_zset` | 60s | Scan `WHERE status='BACKLOG' AND next_attempt_at <= NOW() + INTERVAL '2 minutes' AND is_locked=FALSE`. `ZADD` any missing from `bb:schedule:leads`. Heals ingest-time `ZADD` losses and Redis flushes. |
| `reap_stuck_processing_lists` | 30s | For each `bb:processing:leads:{worker_uuid}` whose worker heartbeat (`bb:worker:heartbeat:{uuid}`, TTL 60s) has expired: read DB status. If `PROCESSING`, drop the tracking entry. If `BACKLOG`, re-`ZADD` and `LREM`. |
| `reconcile_channel_tokens` | 60s | For each active telephony number: if `EXISTS bb:channel:{id}` is 0, `RPUSH` all M tokens (cold-start initialisation). Else compute `M − in_flight_calls_from_db` vs `LLEN` and top up or trim. **This is the only place channel state is created or healed.** No boot-time init logic anywhere else. |
| `clean_stale_bb_locks` | 300s | `UPDATE lead_call_tracker SET is_locked=FALSE WHERE is_locked=TRUE AND locked_at < NOW() − INTERVAL '10 minutes'`. |

The app lifespan triggers `reconcile_channel_tokens` once on startup — via the same scheduler `_execute_task` path so the existing lock guarantees single-pod execution. Cuts the cold-start window from 60s to ~0s.

## 3. Pod-role split — which workloads run the dispatcher

The codebase runs as two k8s workloads today, both executing `app.main:app`:

- **Main server** (one large workload): serves HTTP, handles telephony webhooks, runs `BackgroundTaskScheduler`.
- **Agent pool** (many small pods): pre-warmed Daily rooms + voice agent subprocesses; receives call handoff at answer-webhook time.

The dispatcher must run on **main server pods only**. Agent pool pods touch zero Redis dispatcher state.

| Component | Main server | Agent pool |
|---|---|---|
| `POST /leads` ingest + `ZADD` | ✅ | ❌ |
| Telephony webhooks (`LPUSH` channel token on call-end) | ✅ | ❌ |
| Promoter (leader-elected) | ✅ | ❌ |
| Dispatch workers (BLPOP loops) | ✅ | ❌ |
| Reconcilers on `BackgroundTaskScheduler` | ✅ | ❌ |
| Voice agent subprocess pool + Daily room pool | ❌ | ✅ |

Use one primary env var with per-component overrides:

```text
POD_ROLE=main_server   # default for main workload
POD_ROLE=agent_pool    # for agent pool workload
```

Component flags default from `POD_ROLE` but each can be overridden independently:

| Flag | main_server default | agent_pool default |
|---|---|---|
| `ENABLE_DISPATCHER` (new) | true | false |
| `ENABLE_VOICE_AGENT_POOL` (new — today implicit) | false | true |
| `ENABLE_DAILY_ROOM_POOL` (new — today implicit) | false | true |

**Note on `ENABLE_BACKGROUND_TASKS`:** this is an **existing** flag that is NOT
derived from `POD_ROLE`. It is a Redis-backed dynamic config
(`app/core/config/dynamic.py`) defaulting to `false`. The dispatch
reconcilers (`reconcile_backlog_to_zset`, `reap_stuck_processing_lists`,
`reconcile_channel_tokens`, `clean_stale_bb_locks`, `monitor_dispatch_health`)
all register on the existing `BackgroundTaskScheduler`, so this flag must be
set to `true` in DevCycle/Redis for healing to run. The dispatcher workers
and promoter themselves do not depend on this flag — they run unconditionally
when `ENABLE_DISPATCHER` is true.

Lifespan in `app/main.py` becomes a series of `if ENABLE_X: init_x()` blocks. Adding a third workload type later (inbound-only, regional, etc.) = new role enum, no architecture change.

## 4. Retry semantics — unchanged

When a call ends without success (BUSY, NO_ANSWER, FAILED, etc.), the existing `_retry_call` path inserts a **new** `lead_call_tracker` row with `attempt_count+1`. The previous row stays FINISHED with its outcome.

This is preserved exactly. Two reasons:

1. **Analytics depend on it.** `analytics.py` queries count rows-per-outcome. One row per attempt is the contract.
2. **The dispatcher needs zero retry-specific code.** A retry is just another `INSERT + ZADD` — identical to a fresh ingest.

## 5. State machine

```text
   BACKLOG ──worker picks──► PROCESSING ──webhook──► FINISHED
                                                       │
                                                       ▼ (if attempts remaining)
                                              new BACKLOG row + ZADD
```

`LeadCallStatus.RETRY` exists in the enum and the DB `CHECK` constraint but is **never written** in the current codebase — only appears in defensive `IN ('BACKLOG', 'RETRY', 'PROCESSING')` filters. The new design ignores it. Cleaning it up is a separate, low-priority refactor.

## 6. Idempotency

Three guards prevent duplicate dispatch:

1. **Promoter** — `ZREM` returns 0 if another promoter got there first.
2. **Worker pick** — status check after `BLPOP`; drop if not BACKLOG.
3. **DB row lock CAS** — `acquire_lock_on_lead_by_id(lead_id, expected_status=BACKLOG)` is atomic.

A fourth `UPDATE … WHERE status='BACKLOG'` after `make_call` would close the rare worker-crash-mid-`make_call` window (§7.1) but isn't needed at our scale.

## 7. Known limitations

### 7.1 Rare duplicate call window

Worker calls `provider.make_call`. Provider creates the call. Network drops the response OR the worker crashes before persisting `status=PROCESSING`. After 5 min, reaper unlocks the BACKLOG row, lead returns to the ZSET, a second worker dials. Customer is called twice.

- Window: ~50–200ms between HTTP return and DB UPDATE.
- Estimated rate: 0–2 per month at 100k calls/day.
- Today's system has the same risk; we're not regressing.
- **Detection**: Slack alert if a telephony webhook ever arrives for a `call_id` attached to a different `lead_id`. If rate exceeds 1/week, revisit (provider idempotency keys or stored-sid dedup, not a new state).

### 7.2 Operational alerts (Slack, throttled)

Alerts live in `app/ai/voice/agents/breeze_buddy/dispatch/alerts.py` and use Redis `SET NX EX` throttle keys (`bb:alert:fired:*`) so a persistent condition pages once per throttle window, not every minute. Slack failures never break dispatch — every alert is best-effort.

| Alert | Condition | Severity | Throttle | Body action guidance |
|---|---|---|---|---|
| `no_leader` | `bb:promoter:leader` absent | P0 | 5 min | Verify pods alive + Redis reachable; lock re-acquires within `BB_PROMOTER_LEADER_TTL_S`. |
| `dispatch_halted` | Leader present, but `ZCOUNT(score ≤ now) > BB_SCHEDULE_OVERDUE_ALERT_THRESHOLD` | P0 | 5 min | Kill leader to force failover; check `BB_DISPATCH_ENABLED` (DevCycle) / `bb:promoter:paused`. |
| `schedule_depth_high` | `ZCARD bb:schedule:leads > BB_SCHEDULE_DEPTH_ALERT_THRESHOLD` | P2 | 15 min | Channel capacity is the bottleneck; pause noisy reseller or scale numbers. |
| `channel_drift:{number_id}` | Per-tick drift > `BB_CHANNEL_DRIFT_ALERT_THRESHOLD` for that number | P2 | 15 min per-number | Reconciler tops up automatically; sustained drift = provider webhook problem. |
| `orphan_webhook:{call_id}` | Webhook arrived for a `call_id` with no matching lead (§7.1 signal) | P1 | 30 min per-call | Cross-reference provider call list; check pod-crash logs ±10 min. Revisit §7.1 if >1/week. |

Wiring:
- `monitor_dispatch_health` reconciler (default 60s, knob `BB_HEALTH_MONITOR_INTERVAL_S`) emits `no_leader`, `dispatch_halted`, `schedule_depth_high`. Healthy ticks call `clear_throttle` so a brief recovery + relapse re-alerts immediately rather than waiting out the window.
- `reconcile_channel_tokens` (existing reconciler) emits `channel_drift` per number when its per-tick drift exceeds the threshold.
- `handle_call_completion` and `handle_unanswered_calls` emit `orphan_webhook` when `get_lead_by_call_id` returns None.

Threshold knobs (all env-tunable):

| Knob | Default |
|---|---|
| `BB_SCHEDULE_DEPTH_ALERT_THRESHOLD` | 50000 |
| `BB_SCHEDULE_OVERDUE_ALERT_THRESHOLD` | 100 |
| `BB_CHANNEL_DRIFT_ALERT_THRESHOLD` | 5 |
| `BB_HEALTH_MONITOR_INTERVAL_S` | 60 |

Backpressure on ingest is still policy: `POST /leads` writes unconditionally; the alerts above surface the symptom rather than throttling at the API layer.

### 7.3 Carrier and policy floors (out of our control)

- Provider HTTP + carrier signalling: 1–4s. Hard floor.
- Twilio QPS caps: account-level. ZADD jitter mitigates bursts; sub-account fan-out is the structural fix.
- Per-customer rate-limit window, calling-hours window, TCPA: policy floors. Preserved.
- IST hardcoded in `calls.py:115`: known wart, out of scope.

## 8. Cutover

**Single cutover, no per-reseller phasing.** The cron path (`process_backlog_leads`, `GET /cron/initiate`) is **deleted** in the same PR that introduces the dispatcher. There is no dual-write window, no per-reseller flag, no migration safety net.

What this means operationally:

- At deploy time, the dispatcher starts on every main-server pod simultaneously.
- The boot-time `reconcile_channel_tokens` call seeds channel state from the DB.
- The periodic `reconcile_backlog_to_zset` picks up any pre-existing BACKLOG rows within 60 seconds and queues them on `bb:schedule:leads`.
- Pre-existing stuck `PROCESSING` rows (calls whose end-webhook was lost before deploy) are handled by `reconcile_stuck_processing_leads` — the same 10-minute stale sweep that the cron path used, now registered on `BackgroundTaskScheduler`.

Rollback story: flip `BB_DISPATCH_ENABLED` to `false` in DevCycle (or the Redis feature-flag blob) — workers short-circuit their loops and stop consuming. Leads accumulate on `bb:schedule:leads`; the reconciler keeps them recoverable. To actually re-enable dispatching, flip the switch back. **There is no cron fallback** — if the dispatcher is broken in a way the kill-switch can't fix, redeploy.

DB schema impact: one migration `029_event_dispatch_columns.sql` adds a single nullable `dispatched_at` column (stamped by the worker via the lock-acquire query, used to measure end-to-end event-path latency). No `CHECK` constraint changes. No new enum values. Existing rows are unaffected.

Files deleted in this PR:

- `app/api/routers/breeze_buddy/cron.py` (the `GET /cron/initiate` route).
- `process_backlog_leads` from `app/ai/voice/agents/breeze_buddy/managers/calls.py`.
- The cron-router include from `app/api/routers/breeze_buddy/__init__.py`.

Functions preserved (now called by the dispatcher or registered on the scheduler):

- `_get_lead_config`, `_is_within_calling_hours`, `_get_available_number`, `_acquire_number`, `_release_number`, `_run_pre_checks_for_lead`, `_retry_call` — used by `dispatch.worker`.
- `reconcile_stuck_processing_leads` (was `_cleanup_stuck_leads`) — now a registered reconciler.

## 9. Configuration

On top of PR #722's knob list ([06-operations.md](./event-driven-dispatch/06-operations.md)):

| Knob | Default | Meaning |
|---|---|---|
| `BB_DISPATCH_QPS_JITTER_MS` | 200 | ± jitter added to all `ZADD` scores at ingest and retry. |

PR #722's knobs cover everything else (promoter tick, batch, worker count per shard, BLPOP timeouts, reconciler intervals, kill-switches).

## 10. Punctuality budget

| Component | Time |
|---|---|
| Promoter tick latency | 0–200ms (avg 100ms) |
| Worker `BLPOP` pickup | <10ms |
| DB row lock CAS | 10–50ms |
| Pre-checks (inline) | 300–1500ms |
| Greeting TTS | 300–800ms |
| Rate-limit + channel `BLPOP` | <50ms (good path) |
| Provider `make_call` HTTP | 200–800ms |
| **Time we send `make_call`** | **T + 800–3200ms** |
| Carrier signalling → phone ringing | 1–4s |
| **Time customer's phone rings** | **T + 2–7s** |

**SLO: phone rings within 3–7s of `next_attempt_at`.** Carrier signalling dominates; software cannot get under the PSTN floor. Shaving inline pre-checks + TTS would change our internal "T → `make_call`" number but not the customer-visible "T → phone rings" bucket, so no software-side optimisation here is worth the complexity.

## 11. Scaling envelope

| Daily call volume | Verdict |
|---|---|
| Up to 10M/day | Holds with tuning: promoter tick 100ms + batch 1000, reconciler limit 10k/min, DB connection pool sized to peak, `lead_call_tracker` partitioned by `created_at` monthly, ~50–100 pods. |
| 50M+/day | Needs sharding: the schedule ZSET becomes a Redis hot key, the promoter saturates, Postgres needs write-sharding. Plan for it; not v1. |

At 10M/day the bottleneck is not the dispatcher — it's number procurement, provider QPS, TCPA compliance, and DB table growth.

## 12. Implementation TODOs

Sequenced for the Phase-0 PR. None of these are research questions — they're the punch list.

1. New module `app/ai/voice/agents/breeze_buddy/dispatch/` — `queue.py`, `promoter.py`, `worker.py`, `channel_semaphore.py`, `reconcilers.py`, `leader.py`. **Promoter must wrap its `ZREM`+`LPUSH` pair in a single Lua script** so a mid-loop Redis hiccup or pod death can't remove a lead from the schedule without adding it to the ready list. Without Lua, every promoter-pod restart could orphan whichever leads were mid-loop until the 60s reconciler heals; Lua wrapping eliminates that window. Pure atomicity — no logic change.
2. Migration `029_event_dispatch_columns.sql` — single nullable `dispatched_at TIMESTAMP` column, stamped by the lock-acquire query when expected_status=BACKLOG. No enum/CHECK changes.
3. `POST /leads` handler (`app/api/routers/breeze_buddy/leads/handlers.py:324`) and `_retry_call` path: `INSERT + ZADD` with jitter. Add `POST /leads/{lead_id}/dispatch-now` operator endpoint (see §2 Plane 1).
4. Telephony callback handlers (`app/api/routers/breeze_buddy/`): `LPUSH bb:channel:{id} token` on call-end. All three providers.
5. App lifespan (`app/main.py`): start promoter task, start workers, register four reconcilers on `BackgroundTaskScheduler`, trigger `reconcile_channel_tokens` once for fast cold start. K8s readiness probe blocks until that first reconcile returns.
6. Delete cron path: remove `process_backlog_leads`, `/cron/initiate`, and the `cron_router` registration. Register `reconcile_stuck_processing_leads` (renamed from `_cleanup_stuck_leads`) on `BackgroundTaskScheduler` to keep the 10-min stuck-PROCESSING sweep.
7. DevCycle flag: `bb_dispatch_enabled` (global kill-switch only; default true). No per-reseller flag.
8. Abort handler (`abort_lead_by_id_query`): also `ZREM bb:schedule:leads <lead_id>` so the promoter doesn't pull a zombie.
9. Graceful shutdown: SIGTERM handler stops accepting new `BLPOP`s, drains in-flight, releases channel tokens.
10. `defer_lead_next_attempt_and_release_lock` accessor: DB write first, then `ZADD`. DB write survives Redis loss; ZADD is the fast path.
11. Tests: unit (queue, promoter, worker, semaphore), integration against real Redis, chaos (kill leader / worker / Redis / webhook), load (10k leads at one timestamp), migration flip & rollback.
12. Dashboards through existing OTEL; runbooks reviewed with on-call before deploy. Slack alerts are already wired in `dispatch/alerts.py` (§6.2) — only thresholds may need tuning per environment.

## 13. References

- [docs/event-driven-dispatch/01-motivation.md](./event-driven-dispatch/01-motivation.md) — why the cron breaks at scale
- [docs/event-driven-dispatch/02-architecture.md](./event-driven-dispatch/02-architecture.md) — full five-plane spec
- [docs/event-driven-dispatch/03-data-model.md](./event-driven-dispatch/03-data-model.md) — every Redis key, DB schema, Lua scripts
- [docs/event-driven-dispatch/04-reliability.md](./event-driven-dispatch/04-reliability.md) — failure-mode catalog
- [docs/event-driven-dispatch/05-migration.md](./event-driven-dispatch/05-migration.md) — six-phase rollout
- [docs/event-driven-dispatch/06-operations.md](./event-driven-dispatch/06-operations.md) — knobs, metrics, alerts, runbooks
