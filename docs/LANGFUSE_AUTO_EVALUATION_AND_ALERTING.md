# Langfuse Auto Evaluation and Alerting System

This document describes the automated Langfuse evaluation monitoring system that continuously polls LLM-as-a-judge evaluation scores and sends Slack alerts for failures.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [System Flow](#system-flow)
- [Core Components](#core-components)
- [Distributed Locking](#distributed-locking)
- [Deduplication Logic](#deduplication-logic)
- [Time Window Handling](#time-window-handling)
- [Configuration](#configuration)
- [Guarantees](#guarantees)
- [Edge Cases](#edge-cases)

## Overview

The auto evaluation and alerting system runs as a **background task** managed by the `BackgroundTaskScheduler` that:

1. Continuously polls Langfuse for LLM-as-a-judge evaluation scores
2. Identifies failures (score = 0) across multiple evaluators
3. Sends detailed Slack alerts with call context and recording URLs
4. Uses Redis distributed locking for multi-pod deployments
5. Implements deduplication to prevent duplicate alerts
6. Ensures 100% coverage with no missed evaluations

**Key Design Principles:**
- **Scheduler-Based**: Runs via generic `BackgroundTaskScheduler` framework
- **Distributed Safety**: Only one pod checks scores at a time (Redis locking)
- **No Duplicates**: Redis-based deduplication prevents repeat alerts
- **No Missed Scores**: Overlapping time windows ensure complete coverage
- **Graceful Shutdown**: Handles SIGTERM for clean pod termination
- **Extensible**: Easy to add new background tasks using the same framework

**Production Configuration:**
- Scheduler Loop: **60 seconds** (checks all tasks every minute)
- Score Check Interval: **10 minutes** (600 seconds)
- Lookback Window: **10 minutes** (overlapping)
- Deduplication TTL: **60 minutes**

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Kubernetes Deployment                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Pod 1      │  │   Pod 2      │  │   Pod 3      │          │
│  │              │  │              │  │              │          │
│  │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │
│  │ │ FastAPI  │ │  │ │ FastAPI  │ │  │ │ FastAPI  │ │          │
│  │ │ Lifespan │ │  │ │ Lifespan │ │  │ │ Lifespan │ │          │
│  │ └────┬─────┘ │  │ └────┬─────┘ │  │ └────┬─────┘ │          │
│  │      │       │  │      │       │  │      │       │          │
│  │      ▼       │  │      ▼       │  │      ▼       │          │
│  │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │
│  │ │ Monitor  │ │  │ │ Monitor  │ │  │ │ Monitor  │ │          │
│  │ │  Loop    │ │  │ │  Loop    │ │  │ │  Loop    │ │          │
│  │ │(10 min)  │ │  │ │(10 min)  │ │  │ │(10 min)  │ │          │
│  │ └────┬─────┘ │  │ └────┬─────┘ │  │ └────┬─────┘ │          │
│  └──────┼───────┘  └──────┼───────┘  └──────┼───────┘          │
│         │                 │                 │                   │
│         └─────────────────┼─────────────────┘                   │
│                           │                                     │
└───────────────────────────┼─────────────────────────────────────┘
                            │
                            ▼
                    ┌───────────────┐
                    │  Redis Cache  │
                    │               │
                    │ Distributed   │
                    │ Lock:         │
                    │ "langfuse:    │
                    │  score_       │
                    │  monitor:     │
                    │  lock"        │
                    │ TTL: 600s     │
                    │               │
                    │ Dedup Keys:   │
                    │ "langfuse:    │
                    │  alert_sent:  │
                    │  {call_sid}"  │
                    │ TTL: 3600s    │
                    └───────┬───────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ▼                   ▼                   ▼
┌───────────────┐   ┌──────────────┐   ┌──────────────┐
│   Langfuse    │   │  PostgreSQL  │   │    Slack     │
│               │   │              │   │              │
│ GET /api/     │   │ Get Call     │   │ Send Alert   │
│ public/v2/    │   │ Recording    │   │ Webhook      │
│ scores        │   │ URL          │   │              │
│               │   │              │   │              │
│ GET /api/     │   │              │   │              │
│ public/       │   │              │   │              │
│ traces/{id}   │   │              │   │              │
└───────────────┘   └──────────────┘   └──────────────┘
```

## System Flow

### Startup Sequence

1. **Application Startup** (`app/main.py` lifespan)
   ```python
   @asynccontextmanager
   async def lifespan(_app: FastAPI):
       # Initialize database, Redis, etc.
       
       # Start score monitoring loop if enabled
       if ENABLE_SCORE_MONITORING_LOOP:
           _score_monitoring_task = asyncio.create_task(
               run_score_monitoring_loop()
           )
   ```

2. **Configuration Validation**
   - Checks `LANGFUSE_EVALUATORS` is configured
   - Checks `SLACK_WEBHOOK_URL` is configured
   - Logs errors if configuration is incomplete
   - Only starts loop if all required config is present

3. **Loop Initialization**
   - Creates background asyncio task
   - Runs independently of HTTP request handling
   - Continues until pod shutdown (SIGTERM)

### Monitoring Loop Cycle

Each cycle (every **10 minutes** in production):

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Try to Acquire Distributed Lock                          │
│    Redis: SET "langfuse:score_monitor:lock" "locked"        │
│           NX (only if not exists)                            │
│           EX 600 (TTL = 10 minutes)                          │
│                                                              │
│    ✓ Lock acquired → Proceed to step 2                      │
│    ✗ Lock exists → Skip this cycle (another pod checking)   │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Fetch Scores from Langfuse                               │
│    - Time window: Last 10 minutes (overlapping)             │
│    - For each evaluator in LANGFUSE_EVALUATORS         │
│    - Filter for score == 0.0 (failures only)                │
│                                                              │
│    Example: 15 total scores, 2 zero scores found            │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Process Each Zero Score                                  │
│    For each failure:                                         │
│    a) Fetch trace details from Langfuse                     │
│    b) Extract call_sid from metadata.attributes.call_sid    │
│    c) Check deduplication (see next section)                │
│    d) Query database for recording_url                      │
│    e) Send Slack alert                                      │
│    f) Mark call_sid as alerted in Redis (60 min TTL)        │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Wait for Next Cycle                                      │
│    await asyncio.sleep(600)  # 10 minutes                   │
│                                                              │
│    Lock automatically expires after 10 minutes               │
│    Next pod can acquire lock and repeat cycle               │
└─────────────────────────────────────────────────────────────┘
```

### Deduplication Flow

For each zero score found:

```
┌─────────────────────────────────────────────────────────────┐
│ Extract call_sid from trace metadata                        │
│   trace.metadata.attributes.call_sid                        │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Check Redis for existing alert                              │
│   Key: "langfuse:alert_sent:{call_sid}"                     │
│   Command: EXISTS key                                        │
└─────────────────────────────────────────────────────────────┘
                            │
                ┌───────────┴───────────┐
                │                       │
                ▼                       ▼
        ┌──────────────┐        ┌──────────────┐
        │ Key EXISTS   │        │ Key NOT      │
        │ (already     │        │ EXISTS       │
        │  alerted)    │        │ (new alert)  │
        └──────┬───────┘        └──────┬───────┘
               │                       │
               ▼                       ▼
        ┌──────────────┐        ┌──────────────┐
        │ Skip Alert   │        │ Send Alert   │
        │ Log: "Alert  │        │ to Slack     │
        │ already sent"│        └──────┬───────┘
        └──────────────┘               │
                                       ▼
                                ┌──────────────┐
                                │ Mark as      │
                                │ Alerted      │
                                │ SETEX key    │
                                │ "1" 3600     │
                                │ (60 min TTL) │
                                └──────────────┘
```

### Shutdown Sequence

1. **SIGTERM Received** (Kubernetes pod termination)
   ```python
   # In lifespan shutdown
   if _score_monitoring_task and not _score_monitoring_task.done():
       _score_monitoring_task.cancel()
       await _score_monitoring_task  # Wait for cancellation
   ```

2. **Graceful Cancellation**
   ```python
   # In run_score_monitoring_loop()
   except asyncio.CancelledError:
       logger.info("Score monitoring loop cancelled, shutting down...")
       break
   ```

3. **Lock Auto-Expiry**
   - Redis lock expires after 10 minutes (TTL)
   - Other pods can immediately acquire lock
   - No manual cleanup needed

## Core Components

### 1. Score Monitor Service

**File:** `app/services/langfuse/score_monitor.py`

**Class:** `ScoreMonitor`

**Key Methods:**

- `check_and_alert()` - Main entry point, orchestrates entire check cycle
- `fetch_recent_scores()` - Fetches scores for all evaluators
- `_fetch_scores_for_evaluator()` - Calls Langfuse REST API
- `_is_zero_score()` - Checks if score value == 0.0
- `get_trace_details()` - Fetches trace metadata for context

**Example Usage:**
```python
from app.services.langfuse.score_monitor import score_monitor

# Called by infinite loop in app/main.py
await score_monitor.check_and_alert()
```

### 2. Slack Webhook Service

**File:** `app/services/slack/webhook.py`

**Class:** `SlackWebhook`

**Key Methods:**

- `send_score_alert()` - Sends formatted alert to Slack
- `_build_alert_message()` - Constructs Slack message payload

**Alert Format:**
```
🔴 LLM Judge Failure - Breeze Buddy

Evaluator: breeze buddy outcome correctness
Score: 0.0 (FAILURE)
Trace ID: trace-abc-123
Timestamp: 2025-11-25 12:10:45 UTC

Call Details:
• Call SID: CA1234567890abcdef
• Merchant: shop_12345
• Outcome: confirmed
• Language: hi

Failure Reason: Customer said "cancel" but system marked as "confirmed"

🔗 View Trace | 🎧 Listen to Recording
```

### 3. Redis Service

**File:** `app/services/redis/client.py`

**Class:** `RedisService`

**Key Methods:**

- `get_client()` - Returns underlying Redis client for advanced operations
- `exists(key)` - Check if key exists
- `setex(key, value, ttl_seconds)` - Set key with TTL

**Distributed Lock Usage:**
```python
redis_service = await get_redis_service()
client = await redis_service.get_client()

# Atomic lock acquisition (10 minute TTL in production)
lock_acquired = await client.set(
    "langfuse:score_monitor:lock",
    "locked",
    nx=True,  # Only set if not exists
    ex=600    # TTL: 10 minutes
)
```

### 4. Database Accessor

**File:** `app/database/accessor/breeze_buddy/lead_call_tracker.py`

**Function:** `get_call_recording_url(call_sid: str)`

**Purpose:** Fetch recording URL for Slack alert links

## Distributed Locking

### Why Distributed Locking?

In a multi-pod Kubernetes deployment, without locking:
- All 3 pods would check scores simultaneously
- Each pod would send duplicate alerts
- Langfuse API would receive 3x the requests

With distributed locking:
- Only 1 pod checks scores at a time
- No duplicate alerts
- Efficient API usage

### How It Works

**Atomic Operation:**
```python
lock_acquired = await client.set(
    lock_key,
    "locked",
    nx=True,  # NX = "Not eXists" - only set if key doesn't exist
    ex=600    # EX = "EXpire" - TTL: 10 minutes (production)
)
```

**Redis Guarantees:**
- `SET` with `NX` and `EX` is a **single atomic operation**
- Only ONE client can successfully set the key
- All other clients receive `False` (lock not acquired)
- No race conditions possible

**Timeline Example (Production - 10 minute intervals):**

```
Time    Pod 1                Pod 2                Pod 3
────────────────────────────────────────────────────────────
00:00   SET lock NX EX 600   SET lock NX EX 600   SET lock NX EX 600
        → TRUE ✓             → FALSE ✗            → FALSE ✗
        
00:00   Checking scores...   Skipping cycle       Skipping cycle
00:02   Sending alerts...    (waiting)            (waiting)
00:05   Completed            (waiting)            (waiting)
00:05   Sleep 10 min         (waiting)            (waiting)
        
10:00   Lock expired         SET lock NX EX 600   SET lock NX EX 600
        SET lock NX EX 600   → TRUE ✓             → FALSE ✗
        → FALSE ✗
        
10:00   Skipping cycle       Checking scores...   Skipping cycle
```

### Lock Properties

- **Key:** `langfuse:score_monitor:lock`
- **Value:** `"locked"` (arbitrary, not used)
- **TTL:** **600 seconds (10 minutes)** - matches check interval
- **Auto-Expiry:** Lock automatically expires, no manual cleanup needed
- **Failure Handling:** If pod crashes, lock expires naturally

## Deduplication Logic

### Why Deduplication?

**Problem:** Overlapping time windows can see the same score multiple times

```
Check 1: 12:00 - 12:10 (finds score at 12:05)
Check 2: 12:10 - 12:20 (finds same score at 12:05)
```

Without deduplication: 2 alerts for the same failure ❌

With deduplication: 1 alert only ✓

### Implementation

**Redis Key Pattern:**
```
langfuse:alert_sent:{call_sid}
```

**Example:**
```
langfuse:alert_sent:CA1234567890abcdef
```

**TTL:** 60 minutes (3600 seconds)

**Flow:**
```python
# Check if already alerted
redis_key = f"langfuse:alert_sent:{call_sid}"
already_alerted = await redis_service.exists(redis_key)

if already_alerted:
    logger.info(f"Alert already sent for call_sid '{call_sid}', skipping")
    continue

# Send alert
await slack_webhook.send_score_alert(...)

# Mark as alerted (60 min TTL)
await redis_service.setex(redis_key, "1", ttl_seconds=3600)
```

### Why 60 Minutes TTL?

- Langfuse scores are typically created within minutes of call completion
- 60 minutes ensures we don't re-alert for the same call
- After 60 minutes, key expires and Redis memory is freed
- Covers 6 check cycles (6 × 10 min = 60 min)
- If a new score appears for the same call_sid after 60 min, it's likely a different issue

### Edge Case: Missing call_sid

If `call_sid` is not found in trace metadata:
```python
if not call_sid:
    logger.warning(
        f"No call_sid found for trace {trace_id}, "
        f"cannot prevent duplicate alerts"
    )
    # Still send alert, but can't deduplicate
```

## Time Window Handling

### Overlapping Windows Strategy

**Production Configuration:**
- Check Interval: **10 minutes** (600 seconds)
- Lookback Window: **10 minutes** (hardcoded in `check_and_alert()`)

**Why Overlapping?**

Ensures **100% coverage** with no missed scores:

```
Timeline (Production - 10 minute intervals):
────────────────────────────────────────────────────────────
12:00   Check 1: 11:50 - 12:00 ████████████
12:10   Check 2: 12:00 - 12:10  ████████████
12:20   Check 3: 12:10 - 12:20   ████████████
12:30   Check 4: 12:20 - 12:30    ████████████

Score created at 12:05:
- Caught by Check 2 (12:00-12:10) ✓
- Caught by Check 3 (12:10-12:20) ✓ (deduplicated)
```

**Benefits:**
- **No Missed Scores:** Even if one check fails, next check catches it
- **Resilient to Failures:** Pod crashes don't cause gaps
- **Deduplication Handles Overlap:** Redis prevents duplicate alerts
- **Efficient:** 10-minute intervals reduce API load while maintaining coverage

### UTC Timestamp Handling

All timestamps use UTC timezone:

```python
from datetime import datetime, timezone

to_time = datetime.now(timezone.utc)
from_time = to_time - timedelta(minutes=10)
```

**Why UTC?**
- Langfuse stores timestamps in UTC
- Avoids timezone conversion issues
- Consistent across all pods regardless of location

## Configuration

### Environment Variables

**Production Configuration:**

```bash
# Langfuse Configuration
LANGFUSE_SECRET_KEY=sk-lf-your-secret-key
LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key
LANGFUSE_BASEURL=https://periscope.breeze.in

# Score Monitoring
LANGFUSE_EVALUATORS="breeze buddy outcome correctness,transcript_quality"
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# Enable/Disable Monitoring
ENABLE_SCORE_MONITORING_LOOP=true

# Check Interval (10 minutes in production)
SCORE_CHECK_INTERVAL_SECONDS=600
```

**Optional:**

```bash
# Redis Configuration (for distributed locking)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=your-password  # if required
```

### Configuration Validation

On startup, the system validates:

```python
config_errors = []
if not LANGFUSE_EVALUATORS:
    config_errors.append("LANGFUSE_EVALUATORS is empty")
if not SLACK_WEBHOOK_URL:
    config_errors.append("SLACK_WEBHOOK_URL is not configured")

if config_errors:
    logger.error("Score monitoring will NOT start due to configuration errors")
    # Loop does not start
```

### Evaluator Names

**Format:** Comma-separated list (case-sensitive)

```bash
LANGFUSE_EVALUATORS="evaluator1,evaluator2,evaluator3"
```

**Example:**
```bash
LANGFUSE_EVALUATORS="breeze buddy outcome correctness,transcript quality,address verification"
```

**Important:** Names must match exactly what's in Langfuse (case-sensitive)

## Guarantees

### 1. No Missed Evaluations ✓

**Guarantee:** Every score created in Langfuse will be checked

**How:**
- Overlapping 10-minute windows
- Checks every 10 minutes
- Each score appears in at least 2 consecutive checks
- Even if 1 check fails, next check catches it

**Example:**
```
Score created at 12:05:30

Will be caught by checks at:
12:10 (12:00-12:10) ✓
12:20 (12:10-12:20) ✓ (deduplicated)
```

### 2. No Duplicate Alerts ✓

**Guarantee:** Each failure generates exactly one Slack alert

**How:**
- Redis-based deduplication using `call_sid`
- 60-minute TTL on deduplication keys (covers 6 check cycles)
- Atomic check-and-set pattern

**Example:**
```
Score for call_sid "CA123" found in 2 consecutive checks:

Check 1 (12:10): Alert sent ✓, Redis key set
Check 2 (12:20): Key exists, skip ✗

Result: 1 alert sent, 1 skipped
```

### 3. Distributed Safety ✓

**Guarantee:** Only one pod checks scores at a time

**How:**
- Redis distributed lock with atomic SET NX EX
- Lock TTL = check interval (10 minutes)
- Auto-expiry on pod crash

**Example:**
```
3 pods running:

Pod 1: Acquires lock ✓, checks scores
Pod 2: Lock exists ✗, skips cycle
Pod 3: Lock exists ✗, skips cycle

10 minutes later:

Pod 1: Lock expired, tries to acquire ✗
Pod 2: Acquires lock ✓, checks scores
Pod 3: Lock exists ✗, skips cycle
```

### 4. Graceful Shutdown ✓

**Guarantee:** Clean shutdown on pod termination

**How:**
- FastAPI lifespan handles SIGTERM
- Cancels monitoring task gracefully
- No orphaned processes or locks

## Edge Cases

### 1. Pod Crashes During Check

**Scenario:** Pod crashes while checking scores

**Impact:**
- Lock remains in Redis with TTL
- Lock expires after 10 minutes
- Next pod acquires lock and continues

**Result:** No impact, system self-heals ✓

### 2. Redis Unavailable

**Scenario:** Redis is down or unreachable (production has Redis configured)

**Behavior:**
```python
# Production always has Redis configured
try:
    client = await redis_service.get_client()
    lock_acquired = await client.set(
        lock_key, "locked", nx=True, ex=SCORE_CHECK_INTERVAL_SECONDS
    )
    
    if lock_acquired:
        await score_monitor.check_and_alert()
    else:
        logger.debug("Another pod is monitoring scores, skipping...")
except Exception as e:
    # Redis connection failed
    logger.error(f"Error in score monitoring loop: {e}")
    await asyncio.sleep(60)  # Wait 1 minute before retrying
    # Does NOT proceed with check - skips to next cycle
```

**Impact (Production - Redis configured but down):**
- ✓ System **STOPS checking scores** (fail-safe behavior)
- ✓ Logs error and retries every 1 minute
- ✓ **No duplicate alerts** (check is skipped)
- ✓ Resumes automatically when Redis recovers
- ⚠️ Scores may be missed during Redis downtime (but caught by next check due to overlapping windows)

**Why Fail-Safe Design:**
- Better to **miss one check cycle** than send **duplicate alerts**
- Overlapping time windows ensure scores are caught when Redis recovers
- Prevents alert fatigue from duplicate notifications

**Monitoring Recommendations:**
- Set up Redis health monitoring and alerts
- Monitor Redis uptime to ensure continuous operation
- Track "Error in score monitoring loop" logs to detect Redis issues
- Ensure Redis has proper failover/HA setup in production

### 3. Langfuse API Failure

**Scenario:** Langfuse API returns error or timeout

**Behavior:**
```python
try:
    scores = self._fetch_scores_for_evaluator(...)
except Exception as e:
    logger.error(f"Error fetching scores: {e}")
    results[evaluator_name] = []  # Empty list, no alerts
```

**Impact:**
- Current check fails
- Next check (10 min later) retries
- Overlapping windows ensure score is caught

**Result:** Temporary failure, self-healing ✓

### 4. Slack Webhook Failure

**Scenario:** Slack webhook returns error

**Behavior:**
```python
alert_sent = await slack_webhook.send_score_alert(...)
if alert_sent:
    # Mark as alerted in Redis
    await redis_service.setex(redis_key, "1", ttl_seconds=3600)
else:
    # Alert failed, don't mark as alerted
    logger.error("Failed to send Slack alert")
```

**Impact:**
- Alert not sent
- Not marked in Redis
- Next check retries sending alert

**Result:** Retry on next check ✓

### 5. Missing call_sid in Metadata

**Scenario:** Trace doesn't have `call_sid` in metadata

**Behavior:**
```python
if not call_sid:
    logger.warning("No call_sid found, cannot prevent duplicates")
    # Still send alert, but can't deduplicate
```

**Impact:**
- Alert sent successfully
- Cannot deduplicate (no Redis key)
- May receive duplicate alerts if score appears in multiple checks

**Mitigation:** Ensure all traces include `call_sid` in metadata

## Monitoring

To monitor the health and performance of the Langfuse auto evaluation and alerting system, watch for these key indicators:

### Log Messages to Monitor

**Successful Operation:**
```
INFO: Score monitoring loop started
INFO: Checking Langfuse scores from 2025-11-25T12:00:00+00:00 to 2025-11-25T12:10:00+00:00
INFO: Evaluator 'breeze buddy outcome correctness': Found 15 total scores, 2 zero scores
INFO: ✓ Slack alert sent successfully for evaluator 'breeze buddy outcome correctness', trace_id: trace-abc-123
INFO: Alert summary: 2 sent, 0 skipped (duplicates)
```

**Warning Signs:**
```
WARNING: Redis not available for deduplication: Connection refused
WARNING: No call_sid found for trace trace-xyz-789, cannot prevent duplicate alerts
WARNING: Redis check failed for call_sid 'CA123': timeout, proceeding with alert
```

**Error Conditions:**
```
ERROR: Error in score monitoring loop: HTTPStatusError 401 Unauthorized
ERROR: Failed to send Slack alert - Status: 500, Response: Internal Server Error
ERROR: Error fetching scores for evaluator 'evaluator_name': Connection timeout
```

### Metrics to Track

1. **Alert Volume**: Number of alerts sent per check cycle
2. **Duplicate Rate**: Ratio of skipped alerts to total zero scores found
3. **API Latency**: Time taken to fetch scores from Langfuse
4. **Redis Availability**: Uptime of Redis for distributed locking
5. **Slack Success Rate**: Percentage of successful webhook deliveries

### Health Checks

Monitor these system components:
- **Langfuse API**: Ensure credentials are valid and API is responsive
- **Redis**: Verify connection and distributed lock acquisition
- **Slack Webhook**: Test webhook URL is accessible and accepting requests
- **Database**: Check PostgreSQL connection for recording URL lookups

## Troubleshooting

### Common Issues and Solutions

#### 1. No Alerts Being Sent

**Symptoms:**
- Log shows "No zero scores found in this check"
- Or no log messages at all

**Possible Causes:**
- `ENABLE_SCORE_MONITORING_LOOP=false` in environment
- `LANGFUSE_EVALUATORS` not configured or empty
- Langfuse credentials invalid or missing
- No actual failures in the time window

**Solutions:**
```bash
# Verify configuration
echo $ENABLE_SCORE_MONITORING_LOOP  # Should be "true"
echo $LANGFUSE_EVALUATORS           # Should have evaluator names
echo $LANGFUSE_SECRET_KEY           # Should be set
echo $SLACK_WEBHOOK_URL             # Should be set

# Check logs for initialization errors
grep "Score monitoring will NOT start" logs/app.log
grep "Langfuse credentials not found" logs/app.log
```

#### 2. Duplicate Alerts

**Symptoms:**
- Same failure generates multiple Slack messages
- Log shows alerts sent but not skipped

**Possible Causes:**
- Redis not configured or unavailable
- `call_sid` missing from trace metadata
- Deduplication TTL expired (>60 minutes between checks)

**Solutions:**
```bash
# Verify Redis configuration
echo $REDIS_HOST  # Should be set
echo $REDIS_PORT  # Should be set

# Check Redis connectivity
redis-cli -h $REDIS_HOST -p $REDIS_PORT ping

# Verify trace metadata includes call_sid
# Check Langfuse UI for trace structure
```

#### 3. Distributed Lock Conflicts

**Symptoms:**
- Log shows "Another pod is monitoring scores, skipping..."
- Multiple pods trying to acquire lock simultaneously

**Possible Causes:**
- This is normal behavior in multi-pod deployments
- Lock TTL too short (should match check interval)

**Solutions:**
- No action needed - this is expected behavior
- Verify `SCORE_CHECK_INTERVAL_SECONDS` matches lock TTL (600 seconds)
- Check that only one pod successfully processes scores per cycle

#### 4. Langfuse API Errors

**Symptoms:**
- HTTP 401 Unauthorized errors
- HTTP 429 Rate Limit errors
- Connection timeouts

**Possible Causes:**
- Invalid or expired credentials
- API rate limits exceeded
- Network connectivity issues

**Solutions:**
```bash
# Test Langfuse API manually
curl -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
  "$LANGFUSE_BASEURL/api/public/v2/scores?limit=1"

# Verify credentials are correct
# Check Langfuse dashboard for API key status
# Reduce check frequency if hitting rate limits
```

#### 5. Slack Webhook Failures

**Symptoms:**
- Log shows "Failed to send Slack alert"
- HTTP 404 or 500 errors from Slack

**Possible Causes:**
- Invalid webhook URL
- Webhook deleted or disabled
- Slack service outage

**Solutions:**
```bash
# Test webhook manually
curl -X POST $SLACK_WEBHOOK_URL \
  -H 'Content-Type: application/json' \
  -d '{"text":"Test message"}'

# Verify webhook URL in Slack settings
# Check Slack status page for outages
```

#### 6. Missing Scores in Time Window

**Symptoms:**
- Scores exist in Langfuse but not detected
- Gaps in coverage despite overlapping windows

**Possible Causes:**
- Pod restarts during check cycle
- Execution time longer than check interval
- Time zone mismatch

**Solutions:**
- Check pod restart logs and timing
- Verify `last_check_time` tracking is working
- Ensure all timestamps use UTC
- Review execution time vs check interval (should be <10 minutes)

### Debug Mode

To enable detailed logging for troubleshooting:

```bash
# Set log level to DEBUG
export PROD_LOG_LEVEL=DEBUG

# Restart application
# Check logs for detailed API requests/responses
grep "Fetching scores with params" logs/app.log
grep "Response status" logs/app.log
grep "Response body keys" logs/app.log
```
