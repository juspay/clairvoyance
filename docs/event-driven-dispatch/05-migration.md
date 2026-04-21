# 05 — Migration Plan

No flag day. Per-reseller rollout with rollback at every phase. The cron continues running as the canonical path until we've proven the event path in production.

## Guiding principles

1. **DB is source of truth throughout.** Both paths read and write the same `lead_call_tracker` rows. They cannot diverge.
2. **Dual-write is cheap.** `ZADD` on ingest is a single Redis op; adding it doesn't break anything.
3. **Feature flag per reseller.** Dispatch path selection is a per-reseller flag. Rollback == flip one flag.
4. **Cron is the safety net.** In early phases, if the event path misses a lead, cron picks it up 30s later. Workers in the event path must not cause double-dispatch (they won't — see [04-reliability.md](04-reliability.md)).

## Phase 0 — Preparation (no behavior change)

**Duration:** ~1 sprint.

**Actions:**

- Add Redis client wiring for the new keys (already have `get_redis_service`).
- Implement the dispatch module (`app/ai/voice/agents/breeze_buddy/dispatch/`).
- Add the optional `lead_call_tracker.dispatched_at` and `shard` columns as migration `026_event_dispatch_columns.sql`.
- Register control-plane reconcilers on `BackgroundTaskScheduler` but have them run in **read-only mode** (compute what they'd do, log it, don't act). This validates the queries on real data.
- Add metrics: ZSET size, ready-list size per shard, processing-list size per worker, channel-token `LLEN` per number, promoter tick duration, leader identity.
- **Do not** start the promoter. **Do not** start workers. Event path is dark.

**Exit criteria:** all code merged, metrics visible in the dashboard, reconcilers running in read-only mode with zero errors.

## Phase 1 — Dual-write (ingest only)

**Duration:** 2-3 days in staging, 1 week in prod.

**Actions:**

- `POST /leads` does `INSERT + ZADD` (new). Cron still does the dispatch.
- Start the promoter and workers, but behind a global kill-switch flag `bb:dispatch:enabled = false`. If set, workers `BLPOP` but immediately re-ZADD the lead (no-op consumption). This lets us validate promoter latency, worker pickup time, shard distribution without actually dispatching.
- Reconcilers move from read-only to active.

**Observability focus:**

- `ZADD` error rate on ingest (should be < 0.01%).
- Promoter tick duration and lag (leads with score < now in ZSET).
- Reconciler `reconcile_backlog_to_zset` re-enqueue count — should drop to near-zero after Phase 0's drift is cleared.

**Exit criteria:** promoter lag < 500ms at p99 under production traffic. No reconciler-driven re-enqueues outside known crash windows.

## Phase 2 — Shadow dispatch (one reseller, low volume)

**Duration:** 1 week.

**Actions:**

- Pick one low-risk reseller (internal test account or the smallest production tenant).
- Set their feature flag `bb:dispatch:use_event_path:{reseller_id} = true`.
- Cron's `process_backlog_leads` now **skips leads from resellers with this flag set**. The event path owns them.
- Workers dispatch normally for this reseller.

**What we're validating:**

- End-to-end dispatch via the event path matches cron's behavior.
- Call outcomes, recording, webhooks, retries all work.
- No drift between DB state and Redis state.

**Rollback:** flip the flag to false. Cron resumes picking up those leads on its next tick. Any lead already mid-dispatch on the event path completes normally (it's already PROCESSING).

**Exit criteria:** one week of zero event-path-specific incidents for this reseller. Dispatch latency p50 < 500ms. Call outcome rates within 2% of the cron baseline.

## Phase 3 — Graduated rollout (1% -> 10% -> 50%)

**Duration:** 2-3 weeks.

**Actions:**

- Enable the flag for progressively larger slices of resellers, sorted by volume so low-volume goes first.
- After each bump, watch for a week. Key metrics:
  - Dispatch latency percentiles.
  - Reconciler-driven re-enqueues (should stay near zero).
  - Channel-token `LLEN` vs expected (divergence indicates webhook issues).
  - Per-shard ready-list size (hot-shard detection).

**Rollback:** per-reseller flag flip. Cron takes over.

**Exit criteria:** 50% of resellers on event path for at least 1 week with no path-specific regressions.

## Phase 4 — Global flip

**Duration:** 1 day + 1 week soak.

**Actions:**

- Enable the event path for the remaining 50%.
- Cron's `process_backlog_leads` now always returns an empty result set (all resellers flagged on).
- Leave cron running as a no-op for one week so rollback is trivial.

**Rollback:** global flag flip returns all resellers to cron. Cron resumes.

**Exit criteria:** one week of 100% event-path dispatch with no regression.

## Phase 5 — Remove cron

**Duration:** 1 PR.

**Actions:**

- Delete `process_backlog_leads` in `app/ai/voice/agents/breeze_buddy/managers/calls.py`.
- Delete the `/cron/initiate` route and the `cron_router` include.
- Delete the per-reseller flag (no longer branching).
- Update CLAUDE.md, remove references to the cron.

**Retain:**

- The `BackgroundTaskScheduler` — now hosting only control-plane reconcilers.
- The `lead_call_tracker.is_locked` column — used by workers.
- The `increment_outbound_number_channels` DB function — used by the channel reconciler to top up tokens.

**Exit criteria:** the word "cron" appears in zero places outside docs that describe historical behavior.

## Rollback decision tree

At any phase, if something looks wrong:

```
        metric anomaly detected
                  │
                  ▼
       Is it event-path-specific?   ── no ──► investigate unrelated cause
                  │
                 yes
                  ▼
       Is the reseller flag scoped?  ── yes ──► flip one reseller's flag
                  │
                  no
                  ▼
       Flip global dispatch flag     (Phase 1-3: falls back to cron
                                      Phase 4+:  requires cron re-enable)
```

## Schema migration caveats

- `026_event_dispatch_columns.sql` adds `dispatched_at` and `shard` columns. Both nullable to avoid backfill. New rows get them populated; old rows leave them NULL.
- Do not drop `is_locked`. Workers still use it.
- Do not drop `attempt_count`. Retries still increment it.

## Feature flag plumbing

Use the existing DevCycle dynamic config (`app/core/config/dynamic.py`). Flag names:

- `bb_dispatch_enabled` (global, default false)
- `bb_dispatch_use_event_path_for_reseller_{reseller_id}` (per-reseller, default false)

Check the per-reseller flag in ingest (to decide whether to ZADD) and in cron (to decide whether to skip). Check the global flag in the worker to no-op if dispatching is paused.

## What can go wrong during migration

- **Dual-picking during flag transition.** When a reseller's flag flips from false to true, a lead ingested just before the flip has a DB row but no ZSET entry. Cron, seeing the flag now true, skips it. Lead is stuck until the reconciler picks it up.
  - **Mitigation:** the `reconcile_backlog_to_zset` reconciler runs every 60s. Stuck window is bounded.
  - **Better mitigation:** on flag flip, trigger an immediate one-shot reconciler run for that reseller.
- **Orphaned cron scans.** During Phase 1-3, cron still runs the scan but filters out flagged resellers. The scan cost doesn't disappear; it just returns fewer rows. This is fine, it's temporary.
- **Channel token double-init.** If the channel init code runs twice (e.g. restart during init), we'd have 2M tokens instead of M. The reconciler detects and trims within 60s. For belt-and-suspenders, init uses `DEL` before `RPUSH`.
