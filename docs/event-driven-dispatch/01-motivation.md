# 01 — Motivation

## What the current system does

1. `POST /leads` (`app/api/routers/breeze_buddy/leads/__init__.py:42`) inserts a row into `lead_call_tracker` with `status=BACKLOG` and `next_attempt_at = now() + initial_offset`.
2. An external cron pings `/cron/initiate` (`app/api/routers/breeze_buddy/cron.py:16`) every 30 seconds.
3. That handler calls `process_backlog_leads` (`app/ai/voice/agents/breeze_buddy/managers/calls.py:439`), which runs:

   ```sql
   SELECT * FROM lead_call_tracker
   WHERE status = 'BACKLOG'
     AND next_attempt_at <= NOW()
     AND is_locked = FALSE
     AND execution_mode IN ('TELEPHONY', 'TELEPHONY_TEST');
   ```

4. It iterates the result set. For each row it tries to atomically acquire a per-row lock (`is_locked=TRUE` with `expected_status=BACKLOG`), runs pre-checks, acquires a telephony number (atomically incrementing `outbound_number.channels`), and calls `provider.make_call(...)`. On failure it may create a new BACKLOG row with `attempt_count+1`.

## The concrete failure mode

Consider a volume day: 500 eligible leads at t=0, serial per-lead processing takes ~350ms each (pre-checks + provider call setup), so the full batch takes ~3 minutes.

```
t=0s    cron fires  -> SELECT returns 500 rows -> iterating starts
t=30s   cron fires  -> SELECT returns ~450 rows (50 now PROCESSING or locked)
                       -> skips all locked rows, but the scan still happened
t=60s   cron fires  -> SELECT returns ~400 rows -> same scan, same skip
t=90s   cron fires  -> SELECT returns ~350 rows -> same scan, same skip
t=120s  cron fires  -> SELECT returns ~300 rows -> same scan, same skip
t=150s  cron fires  -> SELECT returns ~250 rows -> same scan, same skip
t=180s  original batch finishes; new leads have arrived in the meantime
```

**Six full-table index scans have happened during a single batch.** Each scan walks the `(status, next_attempt_at)` index across every BACKLOG row. The scans themselves aren't wasted (they correctly skip locked rows), but they are not free:

- Index walks and row visibility checks on an ever-growing table.
- Postgres lock contention rises as every scan re-visits the same rows.
- `pg_stat_activity` shows multiple long-running transactions from the overlapping cron handlers.
- If multiple pods run the cron, the serialization is worse — one pod wins the Redis lock; the other pods' HTTP timeouts cascade.
- As ingestion outpaces processing, the scan set grows, each scan takes longer, processing falls further behind. Unbounded.

This is the textbook polling-at-the-limit problem. The cron is both the clock and the dispatcher; as the dispatcher slows, the clock keeps ticking, and the scans pile up.

## Secondary pain points

### Latency floor is 30s, ceiling is unbounded

A lead scheduled for `t=T+1s` waits up to 29s for the next tick, then queues behind whatever the batch is chewing on. SLA-sensitive campaigns (order-confirmation, intent-to-buy follow-up) suffer visibly when the backlog grows.

### Rate-limited leads re-picked forever

Today, when a customer hits the rate-limit in `call_limiter.py`, the lead stays BACKLOG. Every subsequent cron tick picks it up, fails again, alerts again. The code explicitly acknowledges this (TODO at `calls.py:584`).

### All-or-nothing batch

`process_backlog_leads` has no batch cap. It pulls every eligible row and iterates serially. A one-time surge becomes a request-timing-out HTTP call and a 5-minute outage window while the first batch drains.

### Channel-capacity logic is DB-coupled

Each call does an atomic `increment_outbound_number_channels` at dispatch time and a corresponding decrement on call-end. This is correct, but it means channel-capacity enforcement is not decoupled from the dispatcher — there is no way to "wait for a channel" other than to pick the lead, fail to acquire, and try again on the next tick.

### Horizontal scale is a lie

Running more pods does not make the cron faster. The scheduler's Redis lock means exactly one pod runs `process_backlog_leads` per tick. Adding pods adds idle capacity, not throughput.

### Retries don't know about recent attempts

Because retries are new BACKLOG rows, the only knobs are `next_attempt_at` and `attempt_count`. There is no per-customer or per-number throttle on the retry path — if a number is currently failing on every call, retries blindly pile back into the same number.

## Requirements for the replacement

Derived directly from the problems above:

1. **No hot-path polling.** Lead dispatch must not do a full-table or even index-bounded SELECT on every tick.
2. **Sub-second dispatch latency** from `next_attempt_at` to provider `make_call`.
3. **The telephony channel count is the only throughput limiter.** Not cron frequency, not worker count, not DB contention.
4. **Horizontally scalable.** Adding pods must add throughput up to the channel ceiling.
5. **Rate-limited leads must not busy-loop** — they must be rescheduled to a time when they can actually run.
6. **Bounded per-tick work.** A surge of 100k leads must not break the system; it must drain at channel capacity.
7. **Correct under failure.** Redis crashes, worker crashes, provider webhook losses must not create stuck leads or duplicate calls.
8. **DB remains source of truth.** Redis is a cache + dispatch fabric, never authoritative for lead state.
9. **Migration must be non-disruptive.** No flag day. Per-reseller cutover with rollback.

Every design decision in the following documents maps back to one of these nine requirements.
