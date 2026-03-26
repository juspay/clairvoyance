# Backlog Processing & Call Lifecycle — Scalability & Reliability Proposal

## Table of Contents
1. [Current Architecture Audit](#1-current-architecture-audit)
2. [Identified Problems](#2-identified-problems)
3. [Proposed Architecture](#3-proposed-architecture)
4. [Implementation Plan](#4-implementation-plan)
5. [Migration Guide](#5-migration-guide)

---

## 1. Current Architecture Audit

### 1.1 Call Lifecycle Overview

```
push_lead API                    Cron (30s)                  Telephony Provider
     │                               │                            │
     ▼                               ▼                            │
  [BACKLOG]  ──────────►  process_backlog_leads()                 │
                           │                                      │
                           ├─ acquire lock                        │
                           ├─ config/blacklist/hours checks       │
                           ├─ TTS greeting → Redis                │
                           ├─ acquire outbound number (channels++)│
                           ├─ make_call() ─────────────────────► ring customer
                           ├─ status → PROCESSING                 │
                           └─ release lock                        │
                                                                  │
                           ┌──────────────────────────────────────┘
                           │
                   Customer answers?
                   ├─ YES: /answer webhook → allocate pod → WebSocket
                   │       conversation happens
                   │       WebSocket closes → handle_call_completion()
                   │       ├─ release number (channels--)
                   │       ├─ release pod
                   │       ├─ status → FINISHED
                   │       └─ if NO_ANSWER/BUSY → _retry_call() → new [BACKLOG]
                   │
                   └─ NO: status callback (no-answer/busy/failed)
                          → handle_unanswered_calls()
                            ├─ release pod
                            ├─ delete greeting from Redis
                            ├─ release number (channels--)
                            ├─ status → FINISHED
                            └─ _retry_call() → new [BACKLOG]
```

### 1.2 Resource Inventory

Five resources are acquired/released during a call's lifecycle:

| Resource | Acquire Point | Release Points | Cleanup | Leak Risk |
|----------|--------------|----------------|---------|-----------|
| **DB Lock** (`is_locked`) | `process_backlog_leads` for-loop | End of for-loop iteration | `_cleanup_stuck_leads` (10 min timeout) | **Medium** — exception in processing leaves lock held; no `finally` block |
| **Outbound Number** (channels) | `_acquire_number` (increment) | 9 release sites scattered across `calls.py` | **None** | **Critical** — orphaned callbacks can't find lead → number never released |
| **Pod** | `/answer` webhook (customer picks up) | 3 release sites + Smart Router 30s garbage collection | Smart Router zombie cleanup | **Low** — well-designed with 3-tier release |
| **Redis Greeting** (`greeting:{lead_id}`) | `prepare_and_store_initial_greeting` | 3 delete sites | **No TTL, no periodic cleanup** | **High** — if callback never fires, key persists forever |
| **Call itself** (telephony API) | `make_call()` | Provider ends call naturally | Provider-side timeout | **Low** — provider handles cleanup |

### 1.3 Current Problems by Subsystem

#### Outbound Number Channels — 9 scattered release sites, no reconciliation

The number is acquired in 2 places but released in **9 different code paths** across `calls.py`:

```
ACQUIRE (2 sites):
  Line 502: _acquire_number() during primary call
  Line 702: _acquire_number() during retry with alternate provider

RELEASE (9 sites):
  Line 389: _cleanup_stuck_leads
  Line 530: invalid customer mobile
  Line 558: race detected (if not updated)
  Line 570: primary call initiation failed
  Line 729: invalid customer mobile (retry)
  Line 756: race detected in retry (if not retry_updated)
  Line 779: retry call initiation failed
  Line 833: handle_call_completion (normal end)
  Line 921: handle_unanswered_calls (no-answer/busy)

MISSING RELEASE (confirmed bugs):
  - handle_unanswered_calls returns early when lead not found (line 895)
    → orphaned call, number never released
  - handle_call_completion returns early when lead not found (line 827)
    → same issue
```

**No periodic reconciliation exists.** Once a channel leaks, it stays leaked until manual intervention or pod restart.

#### Lead Status — ad-hoc transitions, no formal state machine

```
Defined statuses: BACKLOG, PROCESSING, FINISHED, RETRY
Actually used:    BACKLOG, PROCESSING, FINISHED
Never used:       RETRY (defined in schema but never set anywhere)

Transitions happen ad-hoc:
  - update_lead_call_details: BACKLOG → PROCESSING (guarded)
  - update_lead_call_completion_details: ANY → FINISHED (NO status guard)
  - _cleanup_stuck_leads: PROCESSING → FINISHED (locked first)
  - handle_lead_abort: BACKLOG/RETRY → FINISHED
```

`update_lead_call_completion_details` has **no status guard** — any status can be overwritten to FINISHED. This means a lead in BACKLOG could be set to FINISHED without ever being called, if the wrong callback triggers it.

#### Redis Greeting Keys — no TTL, no cleanup

```
greeting:{lead_id}         — NO TTL, deleted manually in 3 places
greeting:template:{id}     — NO TTL, intentional persistent cache (bounded by template count)
```

If `handle_unanswered_calls` never fires (callback lost, provider issue), the greeting key persists in Redis forever. There is no periodic cleanup job.

#### Retry Flow — loses template_id, hardcodes outcome

```python
# _retry_call creates new lead but DOESN'T copy:
#   - template_id (retries lose template association)
#   - outbound_number_id (retries don't inherit number preference)
#   - langfuse_scores
#
# Also: handle_unanswered_calls hardcodes outcome="NO_ANSWER"
# even when Plivo reports "busy" — masks the real reason
```

---

## 2. Identified Problems

### P1: Sequential processing doesn't scale (CRITICAL)

| Lead count | Time (sequential, ~2s/lead) | Acceptable? |
|---|---|---|
| 1,831 (current) | ~60 min | Borderline |
| 10,000 | ~5.5 hours | No |
| 20,000 | ~11 hours | No |

### P2: Channel leak from orphaned callbacks (CRITICAL, in production)

When `get_lead_by_call_id` returns None (because call_id was overwritten by duplicate processing), the callback handler returns without releasing the outbound number. Channels accumulate permanently. Seen in production: 17/20 channels occupied with no active calls.

### P3: Resource cleanup is scattered and fragile (HIGH)

9 release sites for outbound numbers, 3 for greeting keys, 3 for pod release. Missing one path = permanent leak. No `try/finally` ensures cleanup on exceptions.

### P4: No periodic reconciliation for any resource (HIGH)

- No job resets channel counts to match active calls
- No job cleans up orphaned Redis greeting keys
- No job detects leaked channels
- Pod cleanup delegated to Smart Router (works well, but is the exception)

### P5: Stale query snapshots cause wasted work (MEDIUM)

Even with PR #668 fix, overlapping invocations each hold 1,831-lead snapshots. Failed lock acquisitions waste ~15,000 DB queries per cycle.

### P6: No observability into resource state (MEDIUM)

No metrics for: channel utilization, leaked channels, greeting key count, backlog depth, processing latency.

---

## 3. Proposed Architecture

### 3.1 Core: `SELECT FOR UPDATE SKIP LOCKED` Worker Pool

Replace "fetch all, iterate sequentially" with PostgreSQL-native parallel processing:

```python
async def grab_next_backlog_lead(time: datetime) -> Optional[LeadCallTracker]:
    """Atomically grab one BACKLOG lead. SKIP LOCKED ensures no two workers get the same row."""
    # SQL:
    # UPDATE "lead_call_tracker"
    # SET "is_locked" = TRUE, "updated_at" = NOW()
    # WHERE "id" = (
    #     SELECT "id" FROM "lead_call_tracker"
    #     WHERE "status" = 'BACKLOG'
    #     AND "is_locked" = FALSE
    #     AND "next_attempt_at" <= $1
    #     AND "execution_mode" = 'TELEPHONY'
    #     ORDER BY "next_attempt_at" ASC
    #     LIMIT 1
    #     FOR UPDATE SKIP LOCKED
    # )
    # RETURNING *;

async def lead_worker(worker_id: int, session: aiohttp.ClientSession):
    """Single worker: grabs one lead, processes it, grabs the next."""
    while True:
        lead = await grab_next_backlog_lead(datetime.now(timezone.utc))
        if not lead:
            break  # no more leads
        try:
            await process_single_lead(lead, session)
        except Exception as e:
            logger.error(f"Worker {worker_id} failed on lead {lead.id}: {e}")
        finally:
            await release_lock_on_lead_by_id(lead.id)

async def process_backlog_leads():
    """Spawn N concurrent workers."""
    await _cleanup_stuck_leads()
    async with create_aiohttp_session() as session:
        workers = [lead_worker(i, session) for i in range(WORKER_COUNT)]
        await asyncio.gather(*workers)
```

**Why this eliminates P1, P2, P5:**
- Each grab is a fresh query (no stale snapshots)
- N workers process in parallel (20K leads in ~13 min with 50 workers)
- DB guarantees no two workers get same lead (zero race conditions)
- No duplicate calls = no orphaned callbacks = no channel leaks from overwrites

**Throughput projections:**

| Leads | Workers | Time/lead | Total |
|---|---|---|---|
| 10,000 | 20 | 2s | ~17 min |
| 10,000 | 50 | 2s | ~7 min |
| 20,000 | 50 | 2s | ~13 min |
| 20,000 | 100 | 2s | ~7 min |

### 3.2 Resource Manager: Centralized acquire/release with `try/finally`

Replace scattered acquire/release with a context manager pattern:

```python
class CallResourceManager:
    """Manages all resources for a single call attempt.
    Guarantees cleanup via __aexit__ regardless of success/failure."""

    def __init__(self, lead: LeadCallTracker):
        self.lead = lead
        self.outbound_number: Optional[OutboundNumber] = None
        self.greeting_stored: bool = False
        self.call_sid: Optional[str] = None

    async def acquire_number(self, config, template) -> Optional[OutboundNumber]:
        number = await _get_available_number(config, template)
        if number and await _acquire_number(number):
            self.outbound_number = number
            return number
        return None

    async def store_greeting(self, payload, template):
        await prepare_and_store_initial_greeting(self.lead.id, payload, template)
        self.greeting_stored = True

    async def cleanup(self):
        """Release all acquired resources. Idempotent."""
        if self.outbound_number:
            await _release_number(self.outbound_number.id, self.outbound_number.provider)
            self.outbound_number = None
        if self.greeting_stored:
            try:
                redis = await get_redis_service()
                await redis.delete(f"greeting:{self.lead.id}")
            except Exception:
                pass
            self.greeting_stored = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            await self.cleanup()
        return False  # don't suppress exceptions
```

**Usage in process_single_lead:**
```python
async def process_single_lead(lead, session):
    async with CallResourceManager(lead) as resources:
        config = await _get_lead_config(lead)
        if not config or not config.enable_calling:
            return

        number = await resources.acquire_number(config, template)
        if not number:
            return  # __aexit__ cleans up

        await resources.store_greeting(lead.payload, template)

        call = call_provider.make_call(...)
        if not call or not call.get("sid"):
            return  # __aexit__ cleans up number + greeting

        resources.call_sid = call["sid"]
        updated = await update_lead_call_details(lead.id, PROCESSING, call["sid"], ...)
        if not updated:
            await resources.cleanup()  # explicit cleanup, call is orphaned
            return

        # Success: transfer resource ownership to the callback handler
        resources.outbound_number = None  # don't release in __aexit__
        resources.greeting_stored = False  # don't delete in __aexit__
```

**Why this eliminates P3:**
- ONE place to acquire, ONE place to release (the manager)
- `__aexit__` guarantees cleanup on any exception
- Explicit ownership transfer on success (callback handler takes over)
- Impossible to forget a release path

### 3.3 Channel Reconciliation Job

Add a periodic job that resets channel counts based on actual active calls:

```python
async def reconcile_outbound_channels():
    """
    Periodic job: reset channel counts to match actual active calls.
    Runs every 5 minutes. Fixes any leaked channels.
    """
    # For each outbound number:
    #   actual_active = COUNT(*) FROM lead_call_tracker
    #     WHERE outbound_number_id = $1 AND status = 'PROCESSING'
    #   UPDATE outbound_number SET channels = actual_active WHERE id = $1
    #
    # This is the ONLY way to recover from leaked channels.
```

Register in BackgroundTaskScheduler:
```python
scheduler.register_task(
    name="reconcile_outbound_channels",
    func=reconcile_outbound_channels,
    interval_seconds=300,  # every 5 minutes
)
```

**Why this eliminates P2 and P4:**
- Even if a channel leaks, it's corrected within 5 minutes
- Source of truth is the lead_call_tracker table (PROCESSING leads = active calls)
- Idempotent: can run any time without side effects

### 3.4 Redis Greeting TTL

Add TTL to greeting keys:

```python
# In prepare_and_store_initial_greeting:
GREETING_TTL_SECONDS = 600  # 10 minutes — more than enough for call lifecycle

await redis.set(
    f"greeting:{lead_id}",
    json.dumps({"audio": audio_b64, "text": text}),
    ex=GREETING_TTL_SECONDS  # auto-expire
)
```

**Why this eliminates the Redis leak from P4:**
- Greetings are consumed within seconds of call initiation
- 10-minute TTL is a safety net for any path that misses manual deletion
- No accumulation even if cleanup is missed

### 3.5 BackgroundTaskScheduler for Cron

Replace external cron with Redis-locked scheduler:

```python
# app/main.py — register alongside existing tasks
scheduler.register_task(
    name="process_backlog_leads",
    func=process_backlog_leads,
    interval_seconds=30,
)
scheduler.register_task(
    name="reconcile_outbound_channels",
    func=reconcile_outbound_channels,
    interval_seconds=300,
)
```

**Benefits:**
- ONE invocation at a time across all pods (Redis SETNX)
- No external cron dependency
- Workers within the invocation handle parallelism
- `/cron/initiate` kept as manual trigger for ops

---

## 4. Implementation Plan

### Phase 1: Channel Reconciliation Job (immediate — fixes production leak)

**Effort:** Small | **Risk:** Low | **Impact:** Fixes P2, P4 for channels

1. Add `reconcile_outbound_channels()` function
2. Add SQL: count PROCESSING leads per outbound_number_id, update channels
3. Register in BackgroundTaskScheduler (5-minute interval)
4. Add logging: "Reconciled number X: channels Y→Z"

### Phase 2: Redis Greeting TTL (immediate)

**Effort:** Tiny | **Risk:** None | **Impact:** Fixes P4 for Redis

1. Add `ex=600` to `redis.set()` in `prepare_and_store_initial_greeting`
2. No other changes needed — existing manual deletes still work (TTL is just a safety net)

### Phase 3: Extract `process_single_lead` (refactor)

**Effort:** Medium | **Risk:** Low (pure refactor) | **Impact:** Enables Phase 4-5

1. Move the ~400-line for-loop body into `process_single_lead(lead, session)`
2. Keep exact same logic, just in a function
3. Unit-testable for the first time

### Phase 4: `CallResourceManager` (reliability)

**Effort:** Medium | **Risk:** Medium | **Impact:** Fixes P3

1. Implement the context manager
2. Replace all 9 release sites with manager usage
3. Add `try/finally` with `resources.cleanup()` in manager's `__aexit__`
4. Test each error path to confirm cleanup happens

### Phase 5: `FOR UPDATE SKIP LOCKED` Worker Pool (scalability)

**Effort:** Medium | **Risk:** Medium | **Impact:** Fixes P1, P5

1. Add `grab_next_backlog_lead` query and accessor
2. Implement `lead_worker` loop
3. Replace for-loop in `process_backlog_leads` with `asyncio.gather` workers
4. Add `BACKLOG_WORKER_COUNT` env var (default 20)
5. Load-test with staging data

### Phase 6: BackgroundTaskScheduler Registration (ops)

**Effort:** Small | **Risk:** Low | **Impact:** Eliminates concurrent invocations

1. Register `process_backlog_leads` in scheduler
2. Remove external cron schedule
3. Keep `/cron/initiate` endpoint for manual triggers

### Phase 7: Observability (monitoring)

**Effort:** Medium | **Risk:** None | **Impact:** Fixes P6

Add metrics:
- `clairvoyance_backlog_depth` — gauge: count of BACKLOG leads
- `clairvoyance_leads_processed_total` — counter: leads processed per worker
- `clairvoyance_lead_processing_seconds` — histogram: time per lead
- `clairvoyance_channel_utilization` — gauge: channels in use / total per number
- `clairvoyance_channel_reconciliation_drift` — gauge: abs difference corrected by reconciliation

---

## 5. Migration Guide

### What can be removed after full migration

| Code | Purpose | Remove after |
|---|---|---|
| `expected_status` in `acquire_lock_on_lead_by_id` | Prevents stale snapshot races | Phase 5 (keep as safety net) |
| `WHERE status='BACKLOG'` in `update_lead_call_details` | Defense-in-depth | Phase 5 (keep as safety net) |
| FINISHED guard in `handle_unanswered_calls` | Prevents duplicate retries | Keep (still useful for legitimate edge cases) |
| External cron hitting `/cron/initiate` | Triggers processing | Phase 6 |
| 9 scattered `_release_number` calls | Resource cleanup | Phase 4 (replaced by manager) |
| Manual `redis.delete(greeting:...)` calls | Greeting cleanup | Phase 2 (TTL handles it, manual delete is bonus) |

### Rollback plan

Each phase is independently deployable and reversible:
- Phase 1-2: Feature-flagged or env-var controlled
- Phase 3: Pure refactor, revert = undo function extraction
- Phase 4: Resource manager wraps existing logic, revert = remove wrapper
- Phase 5: `BACKLOG_WORKER_COUNT=1` reverts to sequential behavior
- Phase 6: Re-enable external cron, disable scheduler task

### Backwards compatibility

- `acquire_lock_on_lead_by_id` without `expected_status` still works (default None)
- `update_lead_call_details` callers unchanged (both are in process_backlog_leads)
- `/cron/initiate` endpoint preserved for manual triggers
- All existing callback handlers unchanged
