# 06 — Operations

Tuning knobs, metrics, alerts, and runbooks for the event-driven dispatch system.

## Tuning knobs

All live in `app/core/config/static.py` (env-var driven) unless marked otherwise.

### Promoter

| Knob | Default | Meaning | When to tune |
|---|---|---|---|
| `BB_PROMOTER_TICK_MS` | 200 | How often the leader runs `ZRANGEBYSCORE`. | Lower to reduce dispatch latency (at cost of more Redis ops). Raise if Redis CPU high. |
| `BB_PROMOTER_BATCH` | 500 | Max leads moved per tick. | Raise if a single tick can't keep up with due leads. Watch for Redis command latency. |
| `BB_PROMOTER_LEADER_TTL_S` | 5 | Redlock TTL. | Rarely tuned. |
| `BB_PROMOTER_LEADER_RENEW_S` | 2 | Heartbeat interval (must be < TTL). | Rarely tuned. |

### Workers

| Knob | Default | Meaning | When to tune |
|---|---|---|---|
| `BB_WORKERS_PER_SHARD` | 4 | Async tasks per pod per shard. | Raise for hotter shards. |
| `BB_WORKER_BLPOP_TIMEOUT_S` | 30 | Worker `BLPOP` timeout (for shutdown responsiveness). | Rarely tuned. |
| `BB_SHARD_COUNT` | 16 | Total shards. **Deploy-time constant.** | Set once; changing requires careful migration. |
| `BB_WORKER_HEARTBEAT_TTL_S` | 60 | Worker liveness key TTL. | Rarely tuned. |
| `BB_WORKER_HEARTBEAT_REFRESH_S` | 10 | Worker heartbeat refresh interval. | Must be < TTL. |

### Channel semaphore

| Knob | Default | Meaning | When to tune |
|---|---|---|---|
| `BB_CHANNEL_BLPOP_TIMEOUT_S` | 10 | How long worker waits for a free channel. | Lower -> more re-scheduling, less provider timeout risk. Raise -> smoother dispatch at peak. |
| `BB_CHANNEL_WAIT_BACKOFF_MAX_S` | 3 | Jitter range added to ZADD when channels were unavailable. | Rarely tuned. |

### Reconcilers

| Knob | Default | Meaning |
|---|---|---|
| `BB_RECONCILE_BACKLOG_INTERVAL_S` | 60 | How often to heal missing ZSET entries. |
| `BB_RECONCILE_BACKLOG_LIMIT` | 1000 | Max rows scanned per reconciler tick. |
| `BB_REAP_PROCESSING_INTERVAL_S` | 30 | Stuck-worker reaper interval. |
| `BB_REAP_PROCESSING_STUCK_AFTER_S` | 300 | Worker considered stuck after this much heartbeat silence. |
| `BB_RECONCILE_CHANNELS_INTERVAL_S` | 60 | Channel-token reconciler. |

### Operational flags (Redis-backed, runtime-flippable)

| Key | Default | Effect |
|---|---|---|
| `bb:dispatch:enabled` | `true` | Kill-switch. If `false`, workers `BLPOP` but no-op. |
| `bb:promoter:paused` | absent | If exists, promoter skips its tick. |
| `bb:reseller:paused:{id}` | absent | If exists, workers re-ZADD leads for this reseller without dispatching. |

## Metrics

Emit via existing OTEL setup (`app/ai/voice/agents/breeze_buddy/observability/`).

### Gauges

| Metric | Labels | Purpose |
|---|---|---|
| `bb_schedule_size` | - | `ZCARD bb:schedule:leads` |
| `bb_schedule_overdue_count` | - | Leads in ZSET with score < now. Should be small. |
| `bb_ready_list_size` | shard | `LLEN bb:ready:leads:{shard}` |
| `bb_processing_list_size` | worker_uuid | Per-worker in-flight count |
| `bb_channel_tokens_available` | number_id | `LLEN bb:channel:{id}` |
| `bb_channel_tokens_expected` | number_id | DB-derived expected count |
| `bb_promoter_leader_pod` | pod_id | 1 on the leader, 0 elsewhere |
| `bb_workers_alive` | shard | Count of workers heartbeating |

### Counters

| Metric | Labels | Purpose |
|---|---|---|
| `bb_ingest_zadd_total` | result | Increment on every ingest `ZADD`. result=ok/fail. |
| `bb_promoter_ticks_total` | result | Promoter tick count. |
| `bb_promoter_moved_total` | - | Total leads moved from ZSET to ready. |
| `bb_worker_picked_total` | shard, outcome | Worker picked a lead. outcome=dispatched/skipped/rescheduled/errored. |
| `bb_channel_wait_timeout_total` | number_id | Worker gave up waiting for a channel. |
| `bb_reconciler_fix_total` | reconciler | Number of inconsistencies fixed. |
| `bb_leader_elections_total` | - | Every time a pod becomes leader. |

### Histograms

| Metric | Labels | Purpose |
|---|---|---|
| `bb_promoter_tick_duration_ms` | - | ZRANGEBYSCORE + moves. |
| `bb_dispatch_latency_ms` | - | `next_attempt_at` to `make_call` attempt. |
| `bb_channel_wait_duration_ms` | number_id | BLPOP wait time on channel semaphore. |
| `bb_worker_process_duration_ms` | outcome | Full worker loop iteration. |

## Alerts

Recommended, in priority order:

| Alert | Condition | Severity |
|---|---|---|
| Dispatch halted | `bb_promoter_moved_total` rate = 0 for > 2 min AND `bb_schedule_overdue_count` > 10 | P0 |
| No leader | `sum(bb_promoter_leader_pod) == 0` for > 30s | P0 |
| Dispatch latency high | `bb_dispatch_latency_ms` p99 > 5s for 5 min | P1 |
| Ingest ZADD failing | `bb_ingest_zadd_total{result="fail"}` rate > 1% | P1 |
| Channel token drift | `abs(bb_channel_tokens_available - bb_channel_tokens_expected) > 5` sustained 2 min | P2 |
| Reconciler working hard | `bb_reconciler_fix_total{reconciler="backlog_to_zset"}` > 10/min | P2 |
| Hot shard | `bb_ready_list_size` > 100 sustained for one shard | P3 |
| Workers dying | `bb_workers_alive` drops > 50% | P2 |

## Runbooks

### Dispatch halted

**Symptoms:** lead dispatch stops; `bb_schedule_overdue_count` climbs.

**Checklist:**

1. Check `bb_promoter_leader_pod` — is there a leader?
2. If no leader: check Redis connectivity from all pods. Check for `bb:promoter:paused` key existence (someone paused it).
3. If leader exists but not moving leads: check `bb_promoter_tick_duration_ms`. Hung Redis?
4. Check worker health: `bb_workers_alive`. Are workers `BLPOP`ing?
5. Check `bb:dispatch:enabled` flag. Is it accidentally false?

**Emergency recovery:**

- If promoter is stuck, kill the leader pod. New leader elects within 5s.
- If Redis is unreachable, dispatch resumes when Redis is back; boot reconciler restores state.
- If workers are all dead (process pool issue), rolling restart pods.

### Channel token drift

**Symptoms:** `bb_channel_tokens_available` differs from expected by > 5.

**Likely causes:**

1. Call-end webhook losses (token not returned).
2. Duplicate webhooks (token returned twice).
3. Worker crashed after `make_call` but before releasing on error.

**Checklist:**

1. Check `reconcile_channel_tokens` log — is it detecting and fixing? If yes, watch for 2 min; should converge.
2. If drift is growing despite reconciler, check for provider webhook failures in the telephony callback logs.
3. Manual fix (last resort): `DEL bb:channel:{id}` then restart the pod that owns channel init. Reconciler will rebuild from DB.

### Hot shard

**Symptoms:** one shard's ready list grows continuously.

**Likely cause:** one reseller dominates that shard.

**Immediate mitigation:**

- Pause the offending reseller: `SET bb:reseller:paused:{id} 1`. Workers will re-ZADD their leads; peak drains.
- Un-pause when the spike passes.

**Longer-term:**

- Increase `BB_WORKERS_PER_SHARD`.
- Consider sharding by `(reseller_id, template)` pair if one template dominates.

### Lead stuck in BACKLOG forever

**Symptoms:** a specific lead stays BACKLOG well past its `next_attempt_at`.

**Checklist:**

1. Is the reseller paused? Check `bb:reseller:paused:{id}`.
2. Is the row `is_locked=TRUE`? Check `locked_at` — if old, `clean_stale_bb_locks` should handle it within 5 min.
3. Is it in `bb:schedule:leads`? Run `ZSCORE bb:schedule:leads <lead_id>`.
   - If missing, `reconcile_backlog_to_zset` will add it within 60s.
   - If present with score > now, it's correctly waiting.
4. Check if the lead's number has available channels — worker might be stuck waiting.

**Manual fix:**

```
ZADD bb:schedule:leads <now_ms> <lead_id>
```

### Startup failed, pod stuck

**Symptoms:** pod starts but dispatch doesn't begin.

**Checklist:**

1. Check startup logs for "channel semaphore init failed" or "promoter start failed".
2. If Redis was unreachable at startup, the semaphore init would have logged and retried. Confirm Redis is now healthy and restart the pod.
3. Confirm migrations have run (`026_event_dispatch_columns.sql`).

### Rolling deploy behavior

- A pod going down mid-dispatch leaves entries in `bb:processing:leads:{worker_uuid}`. The reaper recovers them within 5 min.
- A pod coming up does NOT need to do a full reconcile — the periodic reconciler already runs on every pod.
- The leader might hop between pods during deploy. Leader-election handles this naturally.
- **Do not run `FLUSHDB` on the Redis instance.** If you must, the boot reconciler will rebuild, but any in-flight call's channel token is gone (DB still correct, just tokens wrong). Recovery within 60s.

## Dashboards

Minimal dashboard suggestion (Grafana or equivalent):

**Row 1 - Health**

- `bb_promoter_leader_pod` (stat — current leader)
- `bb_schedule_overdue_count` (graph)
- `bb_dispatch_latency_ms` p50/p95/p99 (graph)

**Row 2 - Throughput**

- `bb_promoter_moved_total` rate (graph)
- `bb_worker_picked_total` by outcome (stacked graph)
- `bb_ingest_zadd_total` by result (graph)

**Row 3 - Capacity**

- `bb_channel_tokens_available` vs `bb_channel_tokens_expected` per number (graph)
- `bb_channel_wait_duration_ms` p99 (graph)
- `bb_channel_wait_timeout_total` rate (graph)

**Row 4 - Sharding**

- `bb_ready_list_size` per shard (graph, side-by-side)
- `bb_workers_alive` per shard (graph)
- `bb_processing_list_size` sum (graph — rough in-flight count)

**Row 5 - Reconcilers**

- `bb_reconciler_fix_total` per reconciler (graph)
- Reconciler duration histogram (graph)

## Capacity planning checklist

Before a known high-volume campaign:

1. Sum up `maximum_channels` across active numbers. This is your calls/sec ceiling (roughly; depends on call duration).
2. Check current `bb_schedule_size` baseline.
3. Estimate expected peak ingest rate. ZSET should handle orders of magnitude more.
4. Verify worker count per shard can saturate channels. Rough formula: `workers_per_shard >= channels_per_shard`. If you have 100 channels and 16 shards, ~6-8 workers/shard is a starting point.
5. Confirm Redis instance headroom (CPU, memory). At the scale described in [03-data-model.md](03-data-model.md) this is negligible, but worth checking.
