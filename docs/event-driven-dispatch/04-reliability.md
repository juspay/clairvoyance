# 04 — Reliability and Failure Modes

Distributed systems fail. This document is a per-failure-mode catalog of what goes wrong and how the design recovers. If a failure isn't listed here, either the design doesn't handle it or the document needs updating — both should be treated as a bug.

## Delivery guarantees

- **At-least-once dispatch.** A lead may be picked off the ready list more than once during failure scenarios (worker crash mid-pick). The worker's first action — `acquire_lock_on_lead_by_id` with `expected_status=BACKLOG` — is the idempotency guard. If the lead is already PROCESSING, the worker drops it on the floor.
- **Exactly-once call.** Only the worker that wins the DB row lock calls `provider.make_call`. This is enforced by Postgres, not by Redis.
- **At-most-once channel token consumption per call.** The token is held from `BLPOP` until either `make_call` fails (token returned immediately) or the call-end webhook fires (token returned then). If the webhook is lost, the token reconciler recovers it.

## Failure catalog

### Ingest path: pod dies between INSERT and ZADD

**Symptom:** lead row exists in DB with status=BACKLOG, no ZSET entry, no dispatch.

**Detection:** `reconcile_backlog_to_zset` running every 60s.

**Recovery:** reconciler adds the missing ZSET entry within 60s.

**User impact:** up to 60s additional dispatch latency for this one lead. Acceptable.

**Why not an outbox/transaction:** cost of reconciler is negligible; cost of an outbox and relay process is real (operational burden, another failure mode). We pay the rare-crash-window latency to avoid the ongoing complexity.

---

### Redis goes down (cold restart, OOM, crash)

**Symptom:** promoter can't connect, workers block on `BLPOP` forever, ingest fails to `ZADD` (the INSERT still succeeds).

**Detection:** Redis client errors. Metric: `redis_connection_errors_total`.

**Recovery:** When Redis comes back up:

1. Boot-time reconciler runs (triggered from app lifespan or scheduled immediately):
   - `DEL bb:schedule:leads` (defensive — it's empty anyway)
   - Scan all BACKLOG rows with `next_attempt_at <= NOW() + 1 day` and bulk `ZADD`.
   - Re-initialize all channel semaphores.
2. Promoter resumes leader election and starts ticking.
3. Workers' `BLPOP` timers expire and they retry.

**User impact:** dispatch pauses for the Redis outage duration. Ingest `POST /leads` still returns 201 because the DB write succeeded; the `ZADD` failure is logged and the reconciler will pick it up. No lead is lost.

**Critical design point:** ingest must not fail the HTTP request if Redis is down. Log the `ZADD` failure and return 201. The DB write is authoritative.

---

### Redis data loss (e.g. no AOF, restarted without dump)

**Symptom:** same as above, plus all ZSET entries and channel tokens gone.

**Recovery:** identical to "Redis goes down" — the boot reconciler rebuilds from Postgres. Channel semaphores re-init from `outbound_number.maximum_channels`.

**Why this is safe:** Redis holds no authoritative state. Every key is derivable from the DB.

---

### Promoter leader dies

**Symptom:** no leads move from `schedule:leads` to `ready:leads:*`.

**Detection:** lag metric on `schedule:leads` — members with score < now that haven't been consumed.

**Recovery:** leader lock TTL is 5s. Within 5s of leader death, another pod's candidate loop acquires `bb:promoter:leader` and takes over.

**User impact:** up to 5s of dispatch pause. Negligible.

**Guard against split-brain:** the lock is renewed every 2s with a CAS (`SET ... XX GET` then compare value to self). If renewal fails, the process stops promoting immediately. A brief overlap during hand-off is harmless because `ZREM` is the authoritative pick — two promoters racing just means the slower `ZREM` returns 0 and the lead is only moved once.

---

### Worker crashes between BLPOP and finish

Three sub-cases depending on how far the worker got.

**Sub-case A: crashed between `BLPOP ready` and `RPUSH processing`**

- Lead is lost from Redis. DB row is still BACKLOG.
- Recovered by `reconcile_backlog_to_zset` within 60s.
- Window is tiny (a couple of microseconds between two Redis calls). Rare.

**Sub-case B: crashed after `RPUSH processing`, before DB row lock**

- Lead is in `processing:leads:{worker_uuid}` list. DB row is BACKLOG, not locked.
- Recovered by `reap_stuck_processing_lists`: after 5 min of worker heartbeat silence, reaper re-ZADDs to schedule and `LREM`s from processing list.

**Sub-case C: crashed after `make_call` succeeded, before `LREM` from processing list**

- DB row is PROCESSING with `call_id` set.
- Lead is still in `processing:leads:{worker_uuid}`.
- Reaper checks DB status, sees PROCESSING, just cleans up the processing list entry. Does NOT re-dispatch. No duplicate call.

**Sub-case D: crashed after `make_call` succeeded, before channel token is released**

- This is a call-end webhook concern, not a worker crash concern. The token is held by the "in-flight call", which is now orphaned.
- Recovered by `reconcile_channel_tokens`: reconciler computes expected token count from DB in-flight calls; if Redis is short, top up.

---

### Call-end webhook never arrives

**Symptom:** channel token never returned. Over time, `LLEN bb:channel:{id}` drifts below expected.

**Detection:** `reconcile_channel_tokens` running every 60s compares DB in-flight count to Redis token count.

**Recovery:** top up missing tokens. Also update `lead_call_tracker` status to FINISHED with `outcome=WEBHOOK_LOST` if the call has been "PROCESSING" for longer than a sane upper bound (e.g. 30 min).

---

### Duplicate webhook (provider retries)

**Symptom:** `LPUSH channel:{id} token` called twice for the same call end. `LLEN` exceeds M.

**Impact:** None immediately — extra tokens just allow more concurrent calls on that number.

**Detection:** `reconcile_channel_tokens` sees `LLEN > M - in_flight` and `LTRIM`s the excess.

**Recovery:** self-correcting within 60s.

---

### Stuck row lock (`is_locked=TRUE` for long time)

**Symptom:** lead stays in BACKLOG with `is_locked=TRUE` indefinitely.

**Cause:** worker crashed between acquiring the row lock and either (a) finishing the call, or (b) releasing the lock on error.

**Detection:** query for `is_locked=TRUE AND locked_at < now - 10 min`.

**Recovery:** `clean_stale_bb_locks` every 5 min unlocks them.

---

### DB goes down

**Symptom:** `POST /leads` returns 500; workers fail on the initial SELECT after `BLPOP`.

**Worker behavior:** on DB error, release the channel token (if acquired), return the lead to the ZSET with a short backoff (5-10s), and continue the loop.

**Ingest behavior:** returns 500 to the client. Client retries as usual.

**Why this is safe:** no partial state is persisted to Redis that would later cause a phantom call.

---

### One reseller overloads a shard

**Symptom:** `ready:leads:{shard}` grows large; workers on that shard can't keep up; other shards are idle.

**Detection:** per-shard `LLEN` metric.

**Mitigations in order:**

1. Pause the offending reseller via `bb:reseller:paused:{id}`. Workers drop their leads back to the ZSET with a delay.
2. Rebalance — change shard count or per-shard worker count (requires restart; accept as a known limitation).
3. Longer-term: add per-reseller concurrency cap as a second BLPOP semaphore (`bb:reseller:cap:{id}`).

---

### Provider is flaky (make_call returns errors)

**Symptom:** repeated failures on calls to a specific provider.

**Recovery:** existing retry logic moves unchanged. Worker catches the exception, releases the channel token, re-ZADDs with exponential backoff. `attempt_count >= max_retry` transitions to FINISHED with outcome FAILED.

**Provider fallback (today's logic at `calls.py` — picking Exotel when Twilio fails):** preserved, happens inside the worker's `make_call` path, not at the dispatch layer.

---

### Time desync between pods

**Symptom:** some pods see `now()` ahead of others; promoter on a slow-clock pod might `ZRANGEBYSCORE` a range that missed leads.

**Impact:** marginal scheduling skew bounded by clock drift. With NTP-synced hosts this is < 100ms.

**Mitigation:** none required. The leader is one pod at a time, so there's no inter-pod race on the ZSET range. If the leader's clock is off, it's off — the next leader (different pod) corrects it.

---

### Retry storm after an outage recovery

**Symptom:** reconciler re-ZADDs thousands of leads with score = now, workers try to dispatch all at once.

**Mitigation:** reconciler adds small random jitter to each `ZADD` score (`now + rand(0, 2000ms)`). 10k leads spread over 2s, not 1ms. Combined with the channel semaphore, dispatch naturally paces to capacity.

---

### Poison leads (always fail)

**Symptom:** a lead hits max retries and keeps consuming channel slots.

**Detection:** existing `attempt_count >= max_retry` check transitions to FINISHED with outcome FAILED. Same as today.

**Optional:** a dead-letter ZSET `bb:dlq:leads` for manual inspection. Not part of the critical path; can be added later.

## Idempotency summary

The entire system assumes **at-least-once delivery** at the Redis layer and **exactly-once effect** at the DB layer. The four enforcement points:

1. **Promoter:** `ZREM` returns 0 if another promoter got there first → lead is not moved.
2. **Worker pick:** status check after `BLPOP` — if not BACKLOG, drop.
3. **Worker row lock:** `acquire_lock_on_lead_by_id` with `expected_status=BACKLOG` — atomic UPDATE, fails if already locked.
4. **Provider call:** `UPDATE ... WHERE id=? AND status=BACKLOG` after `make_call` — if two workers somehow got past (3), only one UPDATE succeeds.

Four independent guards. Each one alone would prevent duplicate dispatch; having all four makes it impossible under any failure scenario that still obeys the laws of Postgres.

## What could still go wrong

Honest list of known-unknowns:

- **Redlock-based leader election is not bulletproof under adversarial conditions** (GC pauses, network partitions with clock skew). For our use case it's sufficient because a brief double-promote is absorbed by the `ZREM`-returns-0 guard. If we later find real double-promote in production logs, we switch to a fencing-token approach.
- **Channel semaphore isn't fault-tolerant to Redis partition in multi-DC setup.** We don't run multi-DC; single-DC single-Redis assumption is fine.
- **Very long scheduling horizons** (e.g. schedule a lead 30 days out). The ZSET grows. Today's `initial_offset` is typically minutes; if someone schedules for days, it still works, the ZSET just holds it.
