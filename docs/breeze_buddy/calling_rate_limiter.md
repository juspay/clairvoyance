# Redis Sliding Window Rate Limiter

Implementation guide for outbound call frequency tracking.

## Status

Phase 1 — **alert only**. Calls are tracked and Slack alerts fire when the limit is exceeded, but no calls are blocked. Rate limiting (blocking) is not yet implemented.

## Overview

Tracks outbound calls per destination phone number using Redis sorted sets within a configurable rolling window. Default: 7 calls per 3600 seconds.

## Configuration

Environment variables (global, applies to all resellers/templates):

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTBOUND_RATE_LIMIT_MAX_CALLS` | `7` | Max calls per window before alerting |
| `OUTBOUND_RATE_LIMIT_WINDOW_SECONDS` | `3600` | Sliding window duration in seconds |

## Data Structure

Each phone number gets a Redis sorted set. Member = `{timestamp}:{lead_id}` (avoids collisions), Score = timestamp float. Redis keeps members sorted by score, enabling fast range queries by time.

```text
Key:    breeze_buddy:outbound_rate_limit:+919876543210
Type:   Sorted Set (ZSET)

Members (score = timestamp):
  "1711441200.45:lead_abc"  →  score: 1711441200.45
  "1711442800.12:lead_def"  →  score: 1711442800.12
```

## Lua Script

Runs atomically inside Redis — no race conditions across multiple pods/workers. Four operations in one atomic step:

| Command | What it does | Complexity |
|---------|-------------|------------|
| ZREMRANGEBYSCORE | Remove entries older than `now - window`. Slides the window. | O(log n + k) |
| ZCARD | Count remaining entries (calls in current window). | O(1) |
| ZADD | Insert this call attempt (score = now). | O(log n) |
| EXPIRE | Set TTL to window seconds — auto-cleans key if number is never called again. | O(1) |

The script always inserts the call (we're tracking, not blocking) and returns the count *before* insertion. If `count >= limit`, the limit is exceeded.

```lua
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)
return count
```

## Integration

The check runs in `process_backlog_leads()` just before `make_call()`, after all pre-call validations (blacklist, calling hours, pre-checks, phone validation) have passed. On limit exceeded: logs a warning and sends a Slack alert. The call proceeds regardless.

## Key Decisions

### Window

`ZREMRANGEBYSCORE` trims entries older than `now - OUTBOUND_RATE_LIMIT_WINDOW_SECONDS`. Configurable via env var.

### Limit

`count >= OUTBOUND_RATE_LIMIT_MAX_CALLS` triggers an alert. Default 7 means the 8th call in the window triggers the alert. Configurable via env var.

### TTL

EXPIRE resets on every call. Key survives for `window` seconds after last activity, then auto-deletes.

### Why Lua over Pipeline

The app runs on multiple pods. A pipeline is not atomic — two workers checking the same phone number simultaneously can both read `count=6` and both allow. The Lua script runs atomically inside Redis, eliminating this race.

### Fail-open

If Redis is down, the check is skipped. No alert, call proceeds.

## Pipeline vs Lua — when to use which

- Single calling process or low concurrency → use pipeline
- Multiple parallel workers calling the same contacts → use Lua
- Need maximum throughput with acceptable tiny race risk → use pipeline
- Need strict correctness at any scale → use Lua

## Redis Docs Reference

- Sorted sets overview: https://redis.io/docs/latest/develop/data-types/sorted-sets/
- ZREMRANGEBYSCORE: https://redis.io/docs/latest/commands/zremrangebyscore/
- Pipeline: https://redis.io/docs/latest/develop/use/pipelining/
- Lua scripting: https://redis.io/docs/latest/develop/interact/programmability/eval-intro/
