# D/01 — Send pacing per merchant and channel (G11, W8)

**Track D · step 1** · **Kind**: feat · **PR title**: `feat(crm): per-merchant channel throughput budgets in the dispatcher (enh D/01)` · **Depends on**: nothing — rollout 19 is deferred, so this phase implements its own deferral (`queued` + `paced` reason + `retry_after_seconds` via `apply_outcome`), the same shape 19 will reuse for quiet hours · **Notes**: §16.3 G11, `channels.py` docstring

## Design
- `channels.Channel` gains `default_per_minute: int` (whatsapp: 60 — Meta's tier-1 conversation rate is far higher, this is OUR floor; tune per merchant via `crm_channel_binding.capabilities.per_minute`).
- Budget check in `dispatch._dispatch_one` after the gate and before `send()`: a Redis token bucket keyed `crm:pace:{merchant}:{channel}` (Redis is already the repo's distributed-lock store; `app/core/…redis` client). Over budget → **defer**: `apply_outcome(status=queued, reason=REASON_PACED, retry_after_seconds=<seconds to next token>)` — the same requeue shape as quiet hours (rollout 19). Redis unavailable → fail OPEN for pacing (it is not permission-adjacent; a burst is worse than a miss? **Decision: fail open** with an error log — pacing protects throughput, not people).
- Claim fairness: `claim_queued_messages_query` stays global FIFO; pacing defers the over-budget merchant's rows so others proceed.
- Metrics: log line per pass with deferred counts by merchant (bounded like `sample_ids`).

## Red tests
- Bucket arithmetic pure (`tokens_available(now, last, rate, capacity)`); over-budget → queued with `paced` and a positive delay; Redis error → send proceeds.

## Decisions already made
- Token bucket in Redis, not a DB counter. Fail open. `paced` is a REASON word added to `reasons.py`.
