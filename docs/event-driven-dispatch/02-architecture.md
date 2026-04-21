# 02 — Architecture

The system is split into five planes. Each plane has a single responsibility; failures in one plane are contained by the others.

```
                                   ┌──────────────────────────────────────┐
                                   │  PLANE 1 - INGEST  (HTTP-facing)     │
   client ─────► POST /leads ──────►  insert into lead_call_tracker       │
                                   │  ZADD schedule:leads <ts> <id>       │
                                   │  return 201                          │
                                   └────────────────┬─────────────────────┘
                                                    │
                                                    ▼
                                   ┌──────────────────────────────────────┐
                                   │  PLANE 2 - TIME SCHEDULING           │
                                   │  Redis ZSET: schedule:leads          │
                                   │  score = next_attempt_at_unix_ms     │
                                   │  member = lead_id                    │
                                   └────────────────┬─────────────────────┘
                                                    │
                                                    ▼
                                   ┌──────────────────────────────────────┐
                                   │  PLANE 3 - PROMOTION                 │
                                   │  Leader-elected promoter             │
                                   │  every 200ms:                        │
                                   │    ZRANGEBYSCORE 0 now LIMIT 0 500   │
                                   │    for each id:                      │
                                   │      ZREM schedule:leads id          │
                                   │      LPUSH ready:leads:{shard}       │
                                   └────────────────┬─────────────────────┘
                                                    │
                                                    ▼
           ┌────────────────────────────────────────┴────────────────────────────────────────┐
           │  PLANE 4 - DISPATCH                                                             │
           │                                                                                 │
           │   ready:leads:0    ready:leads:1    ...   ready:leads:15                        │
           │        │               │                         │                              │
           │        ▼               ▼                         ▼                              │
           │   workers[shard 0]  workers[shard 1]   ...   workers[shard 15]                  │
           │        │                                                                        │
           │        ▼                                                                        │
           │   BLPOP lead_id                                                                 │
           │   row lock (existing)                                                           │
           │   pre-checks                                                                    │
           │   pick outbound number                                                          │
           │   BLPOP channel:{number_id}  ◄── capacity gate                                  │
           │   provider.make_call(...)                                                       │
           │   UPDATE status -> PROCESSING                                                   │
           │                                                                                 │
           └─────────────────────────────────────────┬───────────────────────────────────────┘
                                                     │
                                          webhook ──►│── LPUSH channel:{number_id} token
                                                     │
                                                     ▼
                                   ┌──────────────────────────────────────┐
                                   │  PLANE 5 - CONTROL PLANE             │
                                   │  (existing BackgroundTaskScheduler)  │
                                   │                                      │
                                   │  reconcile_backlog_to_zset    60s    │
                                   │  reap_stuck_processing_lists  30s    │
                                   │  reconcile_channel_tokens     60s    │
                                   │  clean_stale_bb_locks         300s   │
                                   └──────────────────────────────────────┘
```

## Plane 1 — Ingest

**Responsibility:** accept lead pushes and make them visible to the scheduler.

**Touchpoint:** `POST /leads` (`app/api/routers/breeze_buddy/leads/__init__.py:42`).

**Behavior:**

1. Validate payload against template schema (unchanged).
2. Insert into `lead_call_tracker` with `status=BACKLOG`.
3. Emit the scheduling event: `ZADD schedule:leads <next_attempt_at_unix_ms> <lead_id>`.
4. Return 201 with the tracker id.

Steps 2 and 3 are **not** atomic. If the pod dies between step 2 and step 3, the lead is in BACKLOG without an event. The boot-time and periodic reconciler (Plane 5) will detect this and emit the missing event within 60 seconds. This is an acceptable SLA for the rare crash window; see [04-reliability.md](04-reliability.md).

Reasons we don't use an outbox pattern:
- It would add a second DB write and a dedicated relay process.
- The reconciler already provides at-least-once delivery.
- Duplicate enqueue is harmless (workers are idempotent via the existing `acquire_lock_on_lead_by_id` atomic UPDATE).

## Plane 2 — Time Scheduling

**Responsibility:** hold all scheduled leads in time order.

**Structure:** one Redis sorted set.

| | |
|---|---|
| key | `schedule:leads` |
| type | ZSET |
| score | `next_attempt_at` as Unix milliseconds (double) |
| member | lead id (string) |

**Why ZSET and not alternatives:**

- **Redis Streams** have no native delayed-delivery. Implementing delay on a Stream means time-bucketed streams, a scanner that rotates buckets, and mid-second precision workarounds. That is a worse ZSET.
- **Keyspace notifications on TTL'd keys** are lossy. Subscribers that disconnect lose events. The docs explicitly state keyspace notifications are best-effort. Not acceptable for money-adjacent dispatch.
- **External libraries (arq, rq-scheduler)** add a framework, a second worker model, and a dependency. We already have asyncpg + Redis; we don't need more.
- **ZSET** gives O(log N) insert, O(log N + K) range pop, trivial re-scheduling (same `ZADD` overwrites), and natural ordering. It's the right primitive.

**Sizing:** the ZSET contains only leads whose firing time has not yet passed. Its steady-state size is bounded by *ingestion rate × average scheduling delay*, not by cumulative lead history. If you ingest 1000 leads/minute with an average 5-minute initial offset, steady state is ~5000 members. Trivial for Redis.

**Never shard this ZSET.** It is the global time view. Sharding it would require a promoter per shard, which multiplies leader-election complexity without solving a real problem. Redis handles millions of ZSET members without breaking a sweat.

## Plane 3 — Promotion

**Responsibility:** move leads from "scheduled for later" to "ready to dispatch" at the right moment.

**Implementation:** a single leader-elected async task that runs in every pod. Only the leader acts.

**Leader election:** Redlock pattern on `promoter:leader` with 5s TTL, renewed every 2s. Standard algorithm. On leader death, another pod takes over within ~5s.

**Tick loop (leader only):**

```
every PROMOTER_TICK_MS (default 200):
    now = current_unix_ms()
    ids = ZRANGEBYSCORE schedule:leads 0 now LIMIT 0 PROMOTER_BATCH (default 500)
    if ids is empty:
        continue
    # Single Lua script for the move:
    for id in ids:
        if ZREM schedule:leads id == 1:
            LPUSH ready:leads:{hash(id) % SHARD_COUNT} id
```

**Why Lua for the move:** not for cross-command atomicity (not needed — the `ZREM` result is the guard against double-dispatch), but for network efficiency. Moving 500 leads in one `EVAL` cuts 500 RTTs to 1. At promoter tick rates, this is the difference between a cheap promoter and a Redis CPU hot-spot.

**Why 200ms:** fast enough for sub-second dispatch SLA, slow enough to not burn CPU when idle. Tunable.

**Why a bounded batch (500):** caps work per tick. If 100k leads become due simultaneously (e.g. after a DB maintenance window), the promoter drains them at `500 × 5/sec = 2500/sec`, not in a single blocking scan.

**Why leader-elected and not per-pod:** the promoter's only job is `ZREM + LPUSH`. Running it on multiple pods just means redundant `ZRANGEBYSCORE` calls that find nothing (because the leader already `ZREM`'d). Single-leader is simpler and cheaper.

**Sharding into `ready:leads:{shard}`:**

- `SHARD_COUNT` is a small constant (default 16). Fixed at deploy time.
- Shard key = `hash(reseller_id) % SHARD_COUNT` (lives on the lead row, resolved via the promoter's in-memory cache).
- Per-tenant isolation: one noisy reseller fills its own shards; other shards flow freely.
- Number of workers per shard is configurable. High-volume resellers get more workers on their shards.

## Plane 4 — Dispatch

**Responsibility:** pick a lead, acquire a channel, make the call.

Every pod runs K async worker tasks per shard it handles. Workers are long-lived asyncio tasks, not processes.

**Worker loop:**

```
while running:
    # 1. Receive an event - blocking, not polling
    lead_id = BLPOP ready:leads:{my_shard} timeout=30s
    if no lead_id: continue          # cycle for shutdown signal

    # 2. Move to reliability list for crash recovery
    RPUSH processing:leads:{worker_uuid} lead_id

    try:
        # 3. Load authoritative state from DB
        lead = SELECT ... FROM lead_call_tracker WHERE id = lead_id
        if lead.status != BACKLOG:
            continue                  # already handled (e.g. cancelled)

        # 4. Acquire row lock (existing atomic UPDATE)
        if not acquire_lock_on_lead_by_id(lead_id, expected_status=BACKLOG):
            continue                  # someone else owns it

        # 5. Pre-checks (unchanged)
        if not pass_pre_checks(lead):
            finalize_with_precheck_failure(lead)
            continue

        # 6. Calling-hours guard
        if outside_calling_hours(lead):
            ZADD schedule:leads <next_window_ms> lead_id
            release_row_lock(lead)
            continue

        # 7. Pick outbound number
        number = pick_outbound_number(lead)
        if not number:
            ZADD schedule:leads <now + 10s> lead_id
            release_row_lock(lead)
            continue

        # 8. Capacity gate
        token = BLPOP channel:{number.id} timeout=10s
        if not token:
            # All channels on this number are busy right now
            ZADD schedule:leads <now + jitter(1..3s)> lead_id
            release_row_lock(lead)
            continue

        # 9. Per-customer rate limit (existing sliding window)
        if rate_limited(lead.phone):
            LPUSH channel:{number.id} token
            ZADD schedule:leads <now + rate_limit_window_s> lead_id
            release_row_lock(lead)
            continue

        # 10. Make the call
        try:
            call_sid = provider.make_call(number, lead)
            UPDATE lead_call_tracker
              SET status=PROCESSING, call_id=call_sid
              WHERE id=lead_id AND status=BACKLOG
            # token stays held - released by call-end webhook
        except Exception:
            LPUSH channel:{number.id} token
            ZADD schedule:leads <now + backoff(lead.attempt_count)> lead_id

    finally:
        LREM processing:leads:{worker_uuid} 1 lead_id
```

### The channel semaphore

Each `outbound_number` row with `maximum_channels = M` has a Redis list:

| | |
|---|---|
| key | `channel:{outbound_number_id}` |
| type | LIST |
| length at idle | M |
| member | opaque token string (generated at startup, content does not matter) |

**On startup:** for each active outbound number, `DEL channel:{id}` then `RPUSH channel:{id} token1 token2 ... tokenM`.

**On dispatch:** worker does `BLPOP` — this blocks until a token is available or timeout.

**On call end** (telephony callback handler at `app/api/routers/breeze_buddy/telephony/callbacks/`): `LPUSH channel:{id} token`.

**Why not use `outbound_number.channels` DB counter directly:**

- A `BLPOP` is O(1) and blocks cheaply. A DB counter needs `SELECT ... FOR UPDATE` (row lock) or optimistic `UPDATE ... WHERE channels < maximum_channels` (then retry on zero rows). Both are slower and create DB contention.
- The worker already needs to decide "wait or skip" on capacity. `BLPOP` with a timeout gives that choice for free.
- The DB counter doesn't disappear — it stays as the eventually-consistent view for operators and for the reconciler that detects token leaks. But the *hot path* uses Redis.

This is the single most important design decision in the system. Re-read this section if nothing else.

### Worker scaling

Total concurrent call attempts in the system = sum of workers waiting at line 8 (`BLPOP channel:...`). This is naturally bounded by `sum(M across active numbers)`. Running more workers doesn't cause more calls than channels allow — excess workers just sit in `BLPOP`.

This is why the earlier requirement "telephony channel count is the only throughput limiter" holds: workers can be oversubscribed freely.

## Plane 5 — Control Plane

**Responsibility:** heal Redis-vs-DB drift, clean up stuck state, emit metrics.

**Implementation:** the existing `BackgroundTaskScheduler` (`app/core/background_tasks/scheduler.py`). This framework was purpose-built for slow, single-pod, periodic chores. It fits the control plane exactly.

Registered tasks:

| Task | Interval | Purpose |
|---|---|---|
| `reconcile_backlog_to_zset` | 60s | Scan BACKLOG rows not present in `schedule:leads`; re-enqueue them. Heals lost ingest events and worker crashes before `ZADD`-on-retry. |
| `reap_stuck_processing_lists` | 30s | Scan `processing:leads:*` for entries held > 5 min. Re-ZADD to scheduler with `now`. Checks DB state first to avoid re-dispatching a lead that's already PROCESSING. |
| `reconcile_channel_tokens` | 60s | For each active outbound number, compute `M - in_flight_calls_from_db` vs `LLEN channel:{id}`. Top up leaked tokens. |
| `clean_stale_bb_locks` | 300s | Free rows with `is_locked=TRUE` and `locked_at > 10 min`. Last-line defense against stuck locks. |

**What is NOT in the scheduler:**

- The promoter. Its 200ms tick is far below the scheduler's 60s minimum loop interval. Promoter runs as a dedicated asyncio task on startup.
- The workers. They are always-on consumers, not periodic tasks.
- Channel semaphore initialization. Runs once on startup, not periodically.

See [04-reliability.md](04-reliability.md) for the exact SQL of each reconciler and the failure case each one covers.

## Component placement in the codebase

Proposed layout (for implementation reference):

```
app/ai/voice/agents/breeze_buddy/dispatch/
  __init__.py
  queue.py                  # ZADD / ZRANGEBYSCORE / ZREM helpers
  promoter.py               # leader election + tick loop
  worker.py                 # BLPOP -> dispatch flow
  channel_semaphore.py      # token bucket per outbound number
  reconcilers.py            # functions registered on BackgroundTaskScheduler
  shards.py                 # shard resolution helpers
```

Touchpoints in existing code:

- `app/api/routers/breeze_buddy/leads/handlers.py:97` (`push_lead_handler`) — add ZADD after insert.
- `app/api/routers/breeze_buddy/telephony/callbacks/` — release channel token on call-end.
- `app/main.py` lifespan — start promoter task, initialize channel semaphores, register control-plane tasks on the scheduler.
- `app/ai/voice/agents/breeze_buddy/managers/calls.py:439` (`process_backlog_leads`) — kept during migration, deleted at the end.
