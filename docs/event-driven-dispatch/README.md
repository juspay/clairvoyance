# Event-Driven Lead Dispatch

> **Status:** Design document. Not yet implemented. Replaces the current cron-based backlog picker (`process_backlog_leads` in `app/ai/voice/agents/breeze_buddy/managers/calls.py`) and the `/cron/initiate` trigger.

## What this is

A Redis-backed, event-driven replacement for the current polling-based lead dispatch in Breeze Buddy. Leads become events on a delayed queue; a small pool of promoter + worker processes consume them at the moment they are due, throttled only by telephony channel capacity.

## What it replaces

| Today | New |
|---|---|
| Cron pings `/cron/initiate` every 30s | No cron on the hot path |
| `process_backlog_leads` does `SELECT ... WHERE status=BACKLOG AND next_attempt_at <= NOW()` on every tick | No periodic SELECT on the hot path |
| Iterates leads serially; a slow batch overlaps with the next tick | Work is parallel, bounded by channel capacity |
| Retries = new BACKLOG rows picked up by the next scan | Retries = `ZADD` back to the delayed queue |
| Earliest dispatch latency: 0 - 30s | Earliest dispatch latency: ~200ms - 500ms |

The existing `BackgroundTaskScheduler` (`app/core/background_tasks/`) is **kept** and repurposed for slow control-plane chores (reconcilers, janitors). It is not used for dispatch.

## Key properties

- **Throughput ceiling** = sum of `maximum_channels` across active outbound numbers. Not cron frequency, not worker count.
- **Dispatch latency** ≈ promoter tick (configurable, default 200ms) + worker pickup + provider round-trip.
- **No hot-path DB polling.** Ingest writes an event; promoter reads from Redis; workers read from Redis. DB is touched per-lead, not per-tick.
- **Source of truth stays in Postgres.** Redis is a dispatch fabric. Every Redis structure has a DB-driven reconciler behind it.
- **Horizontally scalable.** Add pods → more workers → more throughput, up to the telephony ceiling. No single lock serializes the work.
- **Safe to lose Redis.** A reconciler on pod boot rebuilds the ZSET from the `lead_call_tracker` table. No data loss, just a brief scheduling gap.

## Reading order

| Doc | What you'll find |
|---|---|
| [01-motivation.md](01-motivation.md) | Exactly why the current cron breaks at scale, with numbers. Read this first if you want the "why". |
| [02-architecture.md](02-architecture.md) | The five planes (Ingest, Schedule, Promote, Dispatch, Control), ASCII diagrams, component responsibilities. |
| [03-data-model.md](03-data-model.md) | Every Redis key, its type, TTL, producer, consumer. DB column additions if any. Lead state machine. |
| [04-reliability.md](04-reliability.md) | Failure modes and recovery. What happens when a worker dies, when Redis restarts, when a webhook is lost. |
| [05-migration.md](05-migration.md) | Phased dual-write rollout. No flag day. Per-reseller flag. Rollback path. |
| [06-operations.md](06-operations.md) | Tuning knobs, metrics, alerts, runbooks. |

## One-paragraph summary

`POST /leads` writes to Postgres and `ZADD`s the lead id into `schedule:leads` with score = `next_attempt_at`. A leader-elected promoter every 200ms runs `ZRANGEBYSCORE 0 now`, `ZREM`s due leads, and `LPUSH`es them onto sharded `ready:leads:{shard}` lists. Worker tasks in every pod `BLPOP` from their shard's ready list, acquire a DB row lock, pass pre-checks, and `BLPOP` a token from `channel:{outbound_number_id}` before dialing. On call-end webhook, the token is `LPUSH`ed back. Retries re-enter the ZSET. The existing `BackgroundTaskScheduler` runs the reconcilers that heal Redis-vs-DB drift.
