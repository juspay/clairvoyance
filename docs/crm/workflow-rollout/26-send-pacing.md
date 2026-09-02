# Phase 26 — Send pacing per merchant and channel (G11, W8)

**Kind**: feat · **PR title**: `feat(crm): per-merchant channel throughput budgets in the dispatcher` · **Depends on**: 19 (so deferrals share the `next_attempt_at` path) · **Notes**: §16.3 G11, `app/crm/connectivity/channels.py` docstring ("W8's pacing and quality-tier defaults join as fields on Channel") · **Wave 7**

## Design
- `channels.Channel` gains `default_per_minute: int` (whatsapp: 60 — Meta's tier-1 conversation rate is far higher, this is OUR floor; tune per merchant via `crm_channel_binding.capabilities.per_minute`).
- Budget check in `dispatch._dispatch_one` after the gate and before `send()`: a Redis token bucket keyed `crm:pace:{merchant}:{channel}` (Redis is already the repo's distributed-lock store; `app/core/…redis` client). Over budget → **defer**: `apply_outcome(status=queued, reason=REASON_PACED, retry_after_seconds=<seconds to next token>)` — the same requeue shape as quiet hours (phase 19). Redis unavailable → fail OPEN for pacing (it is not permission-adjacent; a burst is worse than a miss? **Decision: fail open** with an error log — pacing protects throughput, not people).
- Claim fairness: `claim_queued_messages_query` stays global FIFO; pacing defers the over-budget merchant's rows so others proceed.
- Metrics: log line per pass with deferred counts by merchant (bounded like `sample_ids`).

## Red tests
- Bucket arithmetic pure (`tokens_available(now, last, rate, capacity)`); over-budget → queued with `paced` and a positive delay; Redis error → send proceeds.

## Decisions already made
- Token bucket in Redis, not a DB counter. Fail open. `paced` is a REASON word added to `reasons.py`.
