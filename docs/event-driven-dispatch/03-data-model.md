# 03 — Data Model

## Redis keys

All keys use the prefix `bb:` (breeze buddy) to namespace away from other Redis uses in the app.

| Key | Type | Purpose | Produced by | Consumed by | Eviction |
|---|---|---|---|---|---|
| `bb:schedule:leads` | ZSET | Delayed queue of leads awaiting dispatch. Score = `next_attempt_at` in Unix ms. Member = lead id. | Ingest (`POST /leads`), Worker retry, Reconciler | Promoter | Never (members removed by `ZREM` on promotion or `ZREM` on cancel) |
| `bb:ready:leads:{shard}` | LIST | Ready-to-dispatch leads for a shard. FIFO. | Promoter | Worker `BLPOP` | Never (drained by workers) |
| `bb:processing:leads:{worker_uuid}` | LIST | In-flight leads held by a single worker for crash recovery. | Worker on pick | Worker on finish, Reaper on stuck | Never (normal drain + reaper) |
| `bb:channel:{outbound_number_id}` | LIST | Channel-capacity semaphore. Each member is a token; `LLEN` == available channels. | Startup init, Call-end webhook, Error paths | Worker `BLPOP` before `make_call` | Never (rebuilt from DB by reconciler) |
| `bb:promoter:leader` | STRING | Leader-election lock. Value = pod instance id. TTL 5s, renewed every 2s. | Candidate promoters via `SET NX EX` | Leader itself for heartbeat | TTL |
| `bb:promoter:paused` | STRING | Emergency stop flag. If exists, promoter skips its tick. | Operators (runbook) | Promoter | Manual |
| `bb:reseller:paused:{reseller_id}` | STRING | Per-reseller pause. | Operators | Workers (skip + re-ZADD) | Manual |

Existing keys (referenced for completeness, unchanged):

| Key | Notes |
|---|---|
| `greeting:{lead_id}` | Pre-computed greeting audio. Untouched. |
| `breeze_buddy:outbound_rate_limit:{token}` | Per-customer sliding window. Used by workers before `make_call`. |
| `background:task:{name}:lock` | Existing `BackgroundTaskScheduler` locks. Used for control-plane tasks. |

## DB schema changes

Minimal. The `lead_call_tracker` table already has everything the new system needs (`id`, `status`, `next_attempt_at`, `is_locked`, `attempt_count`, `reseller_id`). No new columns required for dispatch itself.

**Possible additions** (optional, for observability and for better reconciler queries):

| Column | Type | Purpose |
|---|---|---|
| `lead_call_tracker.dispatched_at` | TIMESTAMP NULL | Set when worker picks the lead off the ready list. Lets us measure event-path latency end-to-end. |
| `lead_call_tracker.shard` | SMALLINT NULL | Materialized shard assignment. Avoids repeated `hash(reseller_id)` computation and makes shard re-balancing observable. |

If added, do it as a new sequential migration (e.g. `026_event_dispatch_columns.sql`) per the project's migration rule.

**No new tables.** The ZSET, ready lists, and processing lists live entirely in Redis.

## Lead state machine

Existing states (`app/schemas/breeze_buddy/core.py:33`) are sufficient. The transitions change.

```
              ┌──────────────────────────────┐
              │           BACKLOG            │
              │  (row exists, may or may not │
              │   be in schedule:leads ZSET) │
              └───────┬────────────────┬─────┘
                      │                │
        promoter      │                │ cancel
        + worker      │                │
        pick          │                │
                      ▼                ▼
              ┌──────────────┐   ┌───────────┐
              │  PROCESSING  │   │  FINISHED │
              │  (call_id   │   │ (CANCELLED)│
              │   set)       │   └───────────┘
              └───────┬──────┘
                      │
        webhook       │
        or timeout    │
                      │
              ┌───────▼──────┐
              │   FINISHED   │  ── if attempts remaining ──► new BACKLOG row + ZADD
              │  (terminal   │                                (existing retry pattern)
              │   outcome)   │
              └──────────────┘
```

Key invariants:

1. **BACKLOG rows may or may not have a corresponding ZSET entry.** The reconciler ensures eventual consistency.
2. **PROCESSING rows are never in the ZSET.** The worker does `ZREM`-implicit (by consuming from the ready list) before transitioning.
3. **FINISHED is terminal for that row.** Retries create new rows.
4. **`is_locked=TRUE` always implies a worker holds it.** Reaper clears it after timeout.

## Channel semaphore lifecycle

```
                 startup                 make_call OK                  call_end_webhook
                   │                         │                               │
        outbound_number                      │                               │
        M = maximum_channels                 │                               │
                   │                         │                               │
                   ▼                         ▼                               ▼
    DEL bb:channel:{id}            token consumed (stays              LPUSH bb:channel:{id}
    RPUSH token x M                 with in-flight call)                    token
                   │
                   │  LLEN = M  (idle state, all channels free)
                   │
                   ▼  worker BLPOP  ──► LLEN = M-1  ──► ... ──► LLEN = 0 (all busy)
                                                                  │
                                                                  ▼
                                                   next BLPOP blocks for timeout
                                                   worker reschedules via ZADD
```

Token identity does not matter — every token is interchangeable. What matters is count.

**On outbound number config change** (`maximum_channels` edited): the reconciler will top up or drain tokens to match within 60s. For instant reflection, provide an admin endpoint that re-runs the init for that one number.

## Query shapes (reference)

### Ingest

```python
# Inside push_lead_handler, after the existing INSERT:
await redis.zadd(
    "bb:schedule:leads",
    {lead_id: int(next_attempt_at.timestamp() * 1000)},
)
```

### Promoter tick (Lua script for atomic multi-move)

```lua
-- KEYS[1] = bb:schedule:leads
-- ARGV[1] = now_ms
-- ARGV[2] = batch_limit
-- ARGV[3..N] = shard-key-prefix (we append id modulo SHARD_COUNT in Python before EVAL)

local ids = redis.call("ZRANGEBYSCORE", KEYS[1], 0, ARGV[1], "LIMIT", 0, tonumber(ARGV[2]))
if #ids == 0 then return 0 end
local moved = 0
for i, id in ipairs(ids) do
  if redis.call("ZREM", KEYS[1], id) == 1 then
    -- shard resolution must be done by caller; pass shard-specific script per batch
    moved = moved + 1
  end
end
return moved
```

*Implementation note:* the promoter will typically group ids by shard in Python, then issue one Lua per shard. Keeps the script simple and composable.

### Worker pick

```python
shard = f"bb:ready:leads:{my_shard}"
popped = await redis.blpop(shard, timeout=30)
if popped is None:
    return  # periodic wake for shutdown signal
_, lead_id = popped
await redis.rpush(f"bb:processing:leads:{worker_uuid}", lead_id)
```

### Reconciler (periodic `reconcile_backlog_to_zset`)

```sql
SELECT id, EXTRACT(EPOCH FROM next_attempt_at) * 1000 AS score_ms
FROM lead_call_tracker
WHERE status = 'BACKLOG'
  AND is_locked = FALSE
  AND execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST')
  AND next_attempt_at <= NOW() + INTERVAL '10 minutes'
ORDER BY next_attempt_at ASC
LIMIT 1000;
```

Then, for each returned row, check `ZSCORE bb:schedule:leads id` — if missing, `ZADD`. The filter "next_attempt_at <= NOW() + 10 min" bounds the scan window; far-future leads are handled on a subsequent tick as their firing time approaches.

**This is the only SQL scan in the hot-adjacent path**, and it runs once a minute on a bounded time window with an index on `(status, next_attempt_at)`. Compare to the current cron which runs the same scan every 30s with *no* bound beyond "past due", and you see why scaling improves by orders of magnitude.

### Reaper (periodic `reap_stuck_processing_lists`)

```python
# For each bb:processing:leads:* key:
for key in await redis.scan_iter(match="bb:processing:leads:*"):
    worker_uuid = key.split(":")[-1]
    if worker_last_seen(worker_uuid) > now - 5min:
        continue  # still alive
    for lead_id in await redis.lrange(key, 0, -1):
        lead = await get_lead_by_id(lead_id)
        if lead.status == "PROCESSING":
            # Call already in flight, just clean up tracking
            await redis.lrem(key, 1, lead_id)
            continue
        if lead.status == "BACKLOG":
            await redis.zadd("bb:schedule:leads", {lead_id: now_ms})
            await redis.lrem(key, 1, lead_id)
```

Worker liveness is tracked via `bb:worker:heartbeat:{worker_uuid}` with TTL 60s, refreshed every 10s.

## Sizing assumptions

For capacity planning. Order-of-magnitude, not precise.

| Metric | Assumed | Redis impact |
|---|---|---|
| Peak ingest rate | 1000 leads/minute | 17 ZADDs/sec |
| Avg scheduling delay | 5 minutes | steady-state ZSET size ≈ 5000 members |
| Peak simultaneous dispatch | 500/sec (if channels allow) | 500 BLPOPs/sec, 500 LPUSHes/sec |
| Total channel tokens | 1000 (across all numbers) | 1000 LIST elements total |
| Promoter tick | 200ms, 500 lead batch cap | 5 EVAL/sec at peak |
| Reconciler | 60s, 1000-row scan | negligible |

All of this fits comfortably on a single Redis node. Cluster is not required for scale; it would be a HA decision.
