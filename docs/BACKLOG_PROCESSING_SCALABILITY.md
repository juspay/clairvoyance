# Backlog Processing & Call Lifecycle — Architecture

## Table of Contents
1. [Call Lifecycle](#1-call-lifecycle)
2. [Event-Driven Architecture](#2-event-driven-architecture)
3. [Resource Management](#3-resource-management)
4. [Safety Nets](#4-safety-nets)
5. [Configuration](#5-configuration)
6. [Known Issues & Next Steps](#6-known-issues--next-steps)

---

## 1. Call Lifecycle

### 1.1 Overview

```
push_lead API ──→ on_lead_created(id, next_attempt_at)
                       │
                       ├─ immediate (offset=0) → enqueue to worker pool
                       └─ delayed (offset>0)   → Redis sorted set → wake at exact time
                                                      │
_retry_call ──→ on_lead_created(id, next_attempt_at) ─┘
                                                      │
on_channel_freed ──→ grab_next_backlog_lead (SKIP LOCKED) → enqueue
                                                      │
                              ┌────────────────────────┘
                              ▼
                    ┌──────────────────┐
                    │  Worker Pool     │  N concurrent workers (BACKLOG_WORKER_COUNT)
                    │  (LeadDispatcher)│
                    └────────┬─────────┘
                             ▼
                    process_single_lead(lead_id)
                      │
                      ├─ 1. LOCK: acquire_lock(expected_status=BACKLOG)
                      ├─ 2. VALIDATE: config → enable_calling → blacklist → calling hours → pre-checks
                      ├─ 3. RESOURCES (CallResourceManager):
                      │     acquire number (channel++) → store greeting (Redis, TTL=600s)
                      ├─ 4. CALL: make_call() → provider → status=PROCESSING
                      │     transfer resource ownership to callback handler
                      └─ 5. CLEANUP on any failure: __aexit__ → release number → delete greeting → release lock
                             │
                    ┌────────┴────────┐
                    │                 │
             Customer answers    Customer doesn't
                    │                 │
             /answer webhook     status callback
             allocate pod             │
             WebSocket                │
             conversation             │
                    │                 │
             WebSocket closes         │
                    │                 │
                    └────────┬────────┘
                             │
                    handle_call_ended()  ← UNIFIED handler for ALL paths
                      ├─ 1. release pod (idempotent)
                      ├─ 2. find lead → FINISHED guard
                      ├─ 3. release number
                      ├─ 4. delete greeting
                      ├─ 5. status=FINISHED, outcome from agent or provider
                      ├─ 6. _retry_call() → on_lead_created() (if BUSY/NO_ANSWER/FAILED)
                      └─ 7. on_channel_freed() → grab next lead
```

### 1.2 Resource Inventory

| Resource | Acquire | Release | Safety Net | Leak Risk |
|----------|---------|---------|------------|-----------|
| **DB Lock** (`is_locked`) | `process_single_lead` | `finally` block at end | `_cleanup_stuck_leads` (every 10 min via BackgroundTaskScheduler) | **Low** — `try/finally` guarantees release |
| **Outbound Number** (channels) | `CallResourceManager.acquire_number()` | `CallResourceManager.__aexit__()` or callback handler | `reconcile_outbound_channels` (every 5 min via BackgroundTaskScheduler) | **Low** — manager + reconciliation |
| **Pod** | `/answer` webhook | 3 release sites + Smart Router zombie cleanup | Smart Router 30s garbage collection | **Low** |
| **Redis Greeting** | `CallResourceManager.store_greeting()` | `CallResourceManager.__aexit__()` or callback handler | **TTL=600s** auto-expire | **Low** — TTL prevents permanent leaks |
| **Call** (telephony) | `make_call()` | Provider ends naturally | Provider-side timeout | **Low** |

### 1.3 Lead Status Transitions

```
BACKLOG ──[process_single_lead]──→ PROCESSING ──[handle_call_completion]──→ FINISHED
   ▲                                    │                                      │
   │                                    │                                      │
   └──[_retry_call]─────────────────────┴──[handle_unanswered_calls]───────────┘
                                        │
                              [_cleanup_stuck_leads]──→ FINISHED (outcome=UNKNOWN)
```

Guards:
- `acquire_lock_on_lead_by_id(expected_status=BACKLOG)` — atomic lock + status check
- `update_lead_call_details(WHERE status='BACKLOG')` — prevents overwriting active calls
- `handle_unanswered_calls` FINISHED guard — prevents duplicate retries

---

## 2. Event-Driven Architecture

### 2.1 LeadDispatcher (`lead_dispatcher.py`)

No external cron. No fixed-interval timer. Three event sources:

**Event 1: `on_lead_created(lead_id, next_attempt_at)`**
- Fired by: `push_lead` API handler, `_retry_call`
- If `next_attempt_at <= now`: enqueue lead for immediate processing
- If `next_attempt_at > now`: store in Redis sorted set (`lead_dispatcher:scheduled_leads`)
- The delayed scheduler sleeps until that exact time, then enqueues

**Event 2: `on_channel_freed()`**
- Fired by: `handle_call_ended` (unified call termination handler) after releasing number
- Grabs next eligible BACKLOG lead via `SELECT FOR UPDATE SKIP LOCKED`
- Enqueues for processing — zero wait time between calls

**Event 3: Startup recovery**
- On app startup: drains overdue entries from Redis sorted set + scans DB for overdue BACKLOG leads
- Handles pod restarts, missed events, and downtime recovery

### 2.2 Worker Pool

- `BACKLOG_WORKER_COUNT` concurrent asyncio workers (default: 20)
- Bounded `asyncio.Queue` (size: workers × 2) provides backpressure
- Each worker calls `process_single_lead(lead_id, session)`
- `SELECT FOR UPDATE SKIP LOCKED` guarantees no two workers process the same lead

### 2.3 Delayed Scheduler

- Single coroutine per pod watching the Redis sorted set
- Uses `ZRANGEBYSCORE` to find overdue leads
- Sleeps dynamically until next scheduled time (NOT a fixed interval)
- Wakes early when `on_lead_created` adds a new scheduled lead

### 2.4 Multi-Pod Safety

| Concern | Solution |
|---------|----------|
| Two pods grab same lead | `SELECT FOR UPDATE SKIP LOCKED` — DB guarantees exactly-once |
| Two pods dequeue same scheduled lead | `ZRANGEBYSCORE + ZREM` — atomic per pod |
| Pod dies mid-processing | `_cleanup_stuck_leads` resets lock after 10 min |
| Pod restarts, loses in-memory state | Startup recovery scans Redis sorted set + DB |

### 2.5 Throughput

| Leads | Workers | Time/lead | Total |
|-------|---------|-----------|-------|
| 10,000 | 20 | 2s | ~17 min |
| 10,000 | 50 | 2s | ~7 min |
| 20,000 | 50 | 2s | ~13 min |
| 20,000 | 100 | 2s | ~7 min |

---

## 3. Resource Management

### 3.1 CallResourceManager (`resource_manager.py`)

Async context manager that centralizes resource acquire/release for `process_single_lead`:

```python
async with CallResourceManager(lead) as resources:
    number = await resources.acquire_number(config, template)  # channel++
    await resources.store_greeting(payload, template)          # Redis SET with TTL

    call = make_call(...)
    if call:
        resources.transfer_ownership()  # callback handler now owns resources
        return
    # Any exception or early return → __aexit__ calls cleanup():
    #   - _release_number (channel--)
    #   - redis.delete(greeting key)
```

**Key properties:**
- `__aexit__` guarantees cleanup on any exception or early return
- `transfer_ownership()` prevents double-release when call succeeds
- `cleanup()` is idempotent — safe to call multiple times

### 3.2 Channel Management

- **Acquire**: `increment_outbound_number_channels()` — atomic `UPDATE SET channels = channels + 1 WHERE channels < maximum_channels`
- **Release**: `decrement_outbound_number_channels()` — atomic `UPDATE SET channels = GREATEST(0, channels - 1)`
- **Reconciliation**: `reconcile_outbound_channels()` every 5 min — resets channels to match actual PROCESSING lead count

### 3.3 Redis Greeting Lifecycle

- **Store**: `prepare_and_store_initial_greeting()` with `ex=600` (10-minute TTL)
- **Consume**: Read by agent at call answer time, then deleted
- **Cleanup on failure**: `CallResourceManager.__aexit__()` deletes key
- **Safety net**: TTL auto-expires even if all cleanup paths fail

---

## 4. Safety Nets

Three periodic jobs registered in `BackgroundTaskScheduler` (Redis-locked, one execution across all pods):

| Job | Interval | Purpose |
|-----|----------|---------|
| `cleanup_stuck_leads` | 10 min | Finds leads stuck in PROCESSING > 10 min, marks FINISHED, releases resources |
| `reconcile_outbound_channels` | 5 min | Resets channel counts to match actual active calls (PROCESSING leads) |
| `langfuse_score_monitor` | 10 min | Monitors LLM evaluation scores and alerts (existing) |

**Redis greeting TTL** (600s) is an additional passive safety net — no job needed.

---

## 5. Configuration

| Env Var | Default | Purpose |
|---------|---------|---------|
| `BACKLOG_WORKER_COUNT` | 20 | Number of concurrent lead processing workers per pod |
| `CHANNEL_RECONCILIATION_INTERVAL_SECONDS` | 300 | How often to reconcile channel counts |
| `ENABLE_BACKGROUND_TASKS` | false | Enables BackgroundTaskScheduler (reconciliation, stuck lead cleanup) |
| `BACKGROUND_TASKS_LOOP_INTERVAL_SECONDS` | 60 | How often the scheduler checks for due tasks |

**Rollback**: Set `BACKLOG_WORKER_COUNT=1` to revert to single-worker sequential behavior.

---

## 6. Known Issues & Next Steps

### 6.1 Resolved Bugs

- **Channel leak from orphaned callbacks** — `handle_call_ended` triggers `on_channel_freed` even when lead not found. Reconciliation job corrects channel counts every 5 min.
- **Hardcoded NO_ANSWER outcome** — `handle_call_ended` maps actual provider status (`busy` → `BUSY`, `failed` → `FAILED`, `no-answer` → `NO_ANSWER`).
- **Greeting leak on successful calls** — `handle_call_ended` always deletes greeting key (idempotent).
- **Double pod release** — unified handler releases pod once; status callback no longer does its own release.
- **Stuck leads don't release pods** — `_cleanup_stuck_leads` now calls `safe_release_pod` + `on_channel_freed`.
- **No FINISHED guard in completion path** — `handle_call_ended` has FINISHED guard for all paths, preventing race between WebSocket close and status callback.

### 6.2 Not Yet Implemented: Observability

No backlog-specific metrics are exported. Proposed:

| Metric | Type | Description |
|--------|------|-------------|
| `clairvoyance_backlog_depth` | Gauge | Count of BACKLOG leads |
| `clairvoyance_leads_processed_total` | Counter | Leads processed per worker |
| `clairvoyance_lead_processing_seconds` | Histogram | Time per lead |
| `clairvoyance_channel_utilization` | Gauge | Channels in use / total per number |
| `clairvoyance_channel_reconciliation_drift` | Gauge | Abs difference corrected by reconciliation |

### 6.3 File Map

| File | Purpose |
|------|---------|
| `managers/lead_dispatcher.py` | Event-driven orchestrator, worker pool, delayed scheduler, startup recovery |
| `managers/resource_manager.py` | `CallResourceManager` + `_get_available_number`, `_acquire_number`, `_release_number` |
| `managers/reconciliation.py` | `reconcile_outbound_channels` periodic job |
| `managers/calls.py` | `process_single_lead`, `handle_call_ended`, `_cleanup_stuck_leads`, `_retry_call` |
| `managers/utils.py` | `prepare_and_store_initial_greeting` (with TTL=600s) |
| `services/redis/client.py` | Redis sorted set operations (`zadd`, `zrangebyscore`, `zrem`, `zcard`) |
| `database/queries/lead_call_tracker.py` | `grab_next_backlog_lead_query` (SELECT FOR UPDATE SKIP LOCKED) |
