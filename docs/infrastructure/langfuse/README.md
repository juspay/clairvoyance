# Langfuse Auto Evaluation and Alerting System

This document describes the automated Langfuse evaluation monitoring system that continuously polls LLM-as-a-judge evaluation scores and sends Slack alerts for failures.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [System Flow](#system-flow)
- [Core Components](#core-components)
- [Distributed Locking](#distributed-locking)
- [Duplicate Prevention](#duplicate-prevention)
- [Time Window Handling](#time-window-handling)
- [Configuration](#configuration)
- [Guarantees](#guarantees)
- [Edge Cases](#edge-cases)

## Overview

The auto evaluation and alerting system runs as a **background task** managed by the `BackgroundTaskScheduler` that:

1. Continuously polls Langfuse for LLM-as-a-judge evaluation scores
2. Identifies failures (scores below configurable thresholds on a 1-10 scale) across multiple evaluators
3. Sends detailed Slack alerts with call context and recording URLs
4. Uses Redis distributed locking for multi-pod deployments
5. Stores scores in the database for each call
6. Sends a daily summary with alert counts and call/lead analytics
7. Ensures continuous coverage via last-check-time tracking in Redis

**Key Design Principles:**
- **Scheduler-Based**: Runs via generic `BackgroundTaskScheduler` framework
- **Distributed Safety**: Only one pod checks scores at a time (Redis locking)
- **Last-Check-Time Tracking**: Redis stores the last check timestamp so subsequent runs pick up exactly where the previous run left off, preventing gaps or overlaps
- **Graceful Shutdown**: Handles SIGTERM for clean pod termination
- **Extensible**: Easy to add new background tasks using the same framework
- **Daily Summary**: Sends a daily Slack summary with alert counts, call stats, and lead analytics

**Production Configuration:**
- Scheduler Loop: **60 seconds** (checks all tasks every minute)
- Score Check Interval: **10 minutes** (600 seconds)
- First run lookback: **10 minutes** (subsequent runs use last-check-time from Redis)
- Daily Summary Hour: configurable via `DAILY_SUMMARY_HOUR` (default: 21, i.e. 9 PM)

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
                    │ "background:  │
                    │  task:        │
                    │  langfuse_    │
                    │  score_       │
                    │  monitor:     │
                    │  lock"        │
                    │ TTL: 600s     │
                    │               │
                    │ Last Check:   │
                    │ "langfuse:    │
                    │  score_       │
                    │  monitor:     │
                    │  last_check_  │
                    │  time"        │
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

   The score monitor is registered as a background task via the `BackgroundTaskScheduler` framework.
   The initialization logic lives in `app/services/langfuse/tasks/task.py`:

   ```python
   async def initialize_langfuse_tasks(scheduler) -> bool:
       evaluators = await LANGFUSE_EVALUATORS()
       if not (ENABLE_BB_LANGFUSE_MONITORING_LOOP and evaluators and SLACK_WEBHOOK_URL):
           return False

       scheduler.register_task(
           name="langfuse_score_monitor",
           func=score_monitor.check_and_alert,
           interval_seconds=SCORE_CHECK_INTERVAL_SECONDS,
       )
       return True
   ```

2. **Configuration Validation**
   - Checks `ENABLE_BB_LANGFUSE_MONITORING_LOOP` is `true`
   - Checks `LANGFUSE_EVALUATORS` is configured (dynamic config from Redis, `name:threshold` pairs)
   - Checks `SLACK_WEBHOOK_URL` is configured
   - Logs errors if configuration is incomplete
   - Only registers the task if all required config is present

3. **Scheduler Startup**
   - `BackgroundTaskScheduler` runs a main loop every 60 seconds
   - Each loop iteration attempts to acquire a distributed lock per registered task
   - Lock TTL equals the task's `interval_seconds` (600s for score monitor)
   - Only the pod that acquires the lock executes the task

### Monitoring Loop Cycle

Each cycle (every **10 minutes** in production, controlled by lock TTL):

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Try to Acquire Distributed Lock                          │
│    Redis: SET "background:task:langfuse_score_monitor:lock" │
│           "locked" NX (only if not exists)                   │
│           EX 600 (TTL = 10 minutes)                          │
│                                                              │
│    ✓ Lock acquired → Proceed to step 2                      │
│    ✗ Lock exists → Skip this cycle (another pod checking)   │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Check if daily summary is due                            │
│    - If current hour == DAILY_SUMMARY_HOUR, send summary    │
│    - Summary includes alert counts, call stats, lead stats  │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Determine Time Window                                    │
│    - Get last check time from Redis key                     │
│      "langfuse:score_monitor:last_check_time"               │
│    - If found: from_time = last_check_time (no gaps)        │
│    - If not found (first run): from_time = now - 10 min     │
│    - to_time = now (UTC)                                     │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. Fetch Scores from Langfuse                               │
│    - For each evaluator in LANGFUSE_EVALUATORS (with its    │
│      threshold from dynamic Redis config)                    │
│    - Filter for scores below threshold (1-10 scale)         │
│      using _is_below_threshold(score, threshold)            │
│                                                              │
│    Example: 15 total scores, 2 failing scores found         │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. Process Scores                                           │
│    a) Group all scores by traceId                           │
│    b) Fetch trace details ONCE per unique trace             │
│    c) Extract call_sid from metadata.attributes.call_sid    │
│    d) Store ALL scores in database (update_langfuse_scores) │
│    e) Update last check time in Redis                       │
│    f) For each failing score:                               │
│       - Query database for recording_url via                │
│         get_lead_by_call_id(call_sid) → lead.recording_url  │
│       - Send Slack alert                                    │
│       - Track alert count in Redis for daily summary        │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. Lock Expiry                                              │
│    Lock automatically expires after 10 minutes               │
│    Next scheduler loop iteration, a pod can acquire lock    │
└─────────────────────────────────────────────────────────────┘
```

### Last-Check-Time Tracking

Instead of per-call deduplication, the system uses **last-check-time tracking** in Redis to ensure each score is only seen once:

```
┌─────────────────────────────────────────────────────────────┐
│ Get last check time from Redis                              │
│   Key: "langfuse:score_monitor:last_check_time"             │
│   Value: ISO 8601 timestamp of previous run's end time      │
└─────────────────────────────────────────────────────────────┘
                            │
                ┌───────────┴───────────┐
                │                       │
                ▼                       ▼
        ┌──────────────┐        ┌──────────────┐
        │ Key EXISTS   │        │ Key NOT      │
        │ (continuing  │        │ EXISTS       │
        │  from last)  │        │ (first run)  │
        └──────┬───────┘        └──────┬───────┘
               │                       │
               ▼                       ▼
        ┌──────────────┐        ┌──────────────┐
        │ from_time =  │        │ from_time =  │
        │ last check   │        │ now - 10 min │
        │ time         │        │ (default)    │
        └──────┬───────┘        └──────┬───────┘
               │                       │
               └───────────┬───────────┘
                           ▼
                    ┌──────────────┐
                    │ Fetch scores │
                    │ from_time →  │
                    │ to_time=now  │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │ Update Redis │
                    │ last check   │
                    │ time = now   │
                    └──────────────┘
```

This approach ensures **no gaps and no overlaps** between check cycles. Each score window starts exactly where the previous one ended.

### Shutdown Sequence

1. **SIGTERM Received** (Kubernetes pod termination)
   ```python
   # In lifespan shutdown
   await scheduler.stop()
   ```

2. **Graceful Cancellation**
   ```python
   # In BackgroundTaskScheduler.stop()
   self._running = False
   if self._task and not self._task.done():
       self._task.cancel()
       await self._task
   ```

3. **Lock Auto-Expiry**
   - Redis lock expires after 10 minutes (TTL)
   - Other pods can immediately acquire lock
   - No manual cleanup needed

## Core Components

### 1. Score Monitor Service

**File:** `app/services/langfuse/tasks/score_monitor/score.py`

**Class:** `ScoreMonitor`

**Key Methods:**

- `check_and_alert()` - Main entry point, orchestrates entire check cycle
- `_fetch_scores_for_evaluator()` - Calls Langfuse REST API for a single evaluator
- `_is_below_threshold(score, threshold)` - Checks if score value is below threshold (1-10 scale)
- `get_trace_details()` - Fetches trace metadata for context
- `send_score_alert()` - Sends a Slack alert for a failing score
- `send_daily_summary_if_time()` - Sends daily summary at `DAILY_SUMMARY_HOUR`
- `_store_scores()` - Stores scores in database via `update_langfuse_scores()`
- `_get_last_check_time()` / `_set_last_check_time()` - Redis-based time tracking
- `get_alert_counts_for_date()` - Retrieves per-evaluator alert counts for daily summary

**Task Registration** (`app/services/langfuse/tasks/task.py`):
```python
from app.services.langfuse.tasks.task import initialize_langfuse_tasks

# Called during app startup
await initialize_langfuse_tasks(scheduler)
```

### 2. Slack Alert Service

**File:** `app/services/slack/alert.py`

**Class:** `Alert`

**Key Methods:**

- `send()` - Generic method to send formatted Slack alerts with customizable title, fields, sections, and links
- Supports `SLACK_TAG_USERS` to automatically mention configured users/groups in alerts

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

**Distributed Lock Usage (handled by BackgroundTaskScheduler):**
```python
redis_service = await get_redis_service()

# Atomic lock acquisition (10 minute TTL in production)
lock_acquired = await redis_service.set(
    key="background:task:langfuse_score_monitor:lock",
    value="locked",
    nx=True,  # Only set if not exists
    ex=600    # TTL: 10 minutes
)
```

### 4. Database Accessor

**File:** `app/database/accessor/breeze_buddy/lead_call_tracker.py`

**Key Functions:**

- `get_lead_by_call_id(call_sid)` - Fetches the lead record; recording URL accessed via `lead.recording_url`
- `update_langfuse_scores(call_sid, langfuse_data)` - Stores fetched Langfuse scores in the database
- `get_all_lead_call_trackers(start_date, end_date)` - Used by daily summary for call stats
- `get_lead_based_analytics(start_date, end_date)` - Used by daily summary for lead stats

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

**Atomic Operation (in `BackgroundTaskScheduler._execute_task()`):**
```python
lock_key = f"background:task:{task.name.lower().replace(' ', '_')}:lock"

lock_acquired = await redis_service.set(
    key=lock_key,
    value="locked",
    nx=True,  # NX = "Not eXists" - only set if key doesn't exist
    ex=task.interval_seconds    # EX = "EXpire" - TTL matches task interval
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
00:00   SET lock NX EX=600   SET lock NX EX=600   SET lock NX EX=600
        → TRUE ✓             → FALSE ✗            → FALSE ✗
        
00:00   Checking scores...   Skipping cycle       Skipping cycle
00:02   Sending alerts...    (waiting)            (waiting)
00:05   Completed            (waiting)            (waiting)
00:05   Sleep 10 min         (waiting)            (waiting)
        
10:00   Lock expired         SET lock NX EX=600   SET lock NX EX=600
        SET lock NX EX=600   → TRUE ✓             → FALSE ✗
        → FALSE ✗
        
10:00   Skipping cycle       Checking scores...   Skipping cycle
```

### Lock Properties

- **Key:** `background:task:langfuse_score_monitor:lock`
- **Value:** `"locked"` (arbitrary, not used)
- **TTL:** **600 seconds (10 minutes)** - matches check interval
- **Auto-Expiry:** Lock automatically expires, no manual cleanup needed
- **Failure Handling:** If pod crashes, lock expires naturally

## Duplicate Prevention

### How Duplicates Are Prevented

The system avoids duplicate alerts through **last-check-time tracking** rather than per-call deduplication keys:

1. **Redis stores the last check timestamp** (`langfuse:score_monitor:last_check_time`)
2. Each run queries Langfuse from `last_check_time` to `now` -- no overlap
3. After fetching scores, the system updates `last_check_time = now` **before** processing alerts
4. This ensures even if a pod crashes mid-alerting, the next run won't re-fetch the same scores

**Implementation:**
```python
# Get last check time from Redis (shared across all pods)
last_check_time = await self._get_last_check_time()

if last_check_time:
    from_time = last_check_time  # Continue from where we left off
else:
    from_time = to_time - timedelta(minutes=10)  # First run default

# ... fetch and process scores ...

# Update last check time BEFORE processing alerts
await self._set_last_check_time(to_time)
```

### Edge Case: Missing call_sid

If `call_sid` is not found in trace metadata:
- Scores without a `call_sid` are skipped for database storage
- Alerts are still sent for failing scores even without `call_sid`

## Time Window Handling

### Last-Check-Time Strategy

**Production Configuration:**
- Check Interval: **10 minutes** (600 seconds, controlled by lock TTL)
- Time window: **from last check time to now** (stored in Redis)
- First run fallback: **10 minutes** lookback

**How It Works:**

Each run picks up exactly where the previous one left off:

```
Timeline (Production):
────────────────────────────────────────────────────────────
12:00   Check 1: 11:50 - 12:00 ████████████
        (first run, no last_check_time, defaults to -10min)
        → saves last_check_time = 12:00

12:10   Check 2: 12:00 - 12:10  ████████████
        (reads last_check_time = 12:00)
        → saves last_check_time = 12:10

12:20   Check 3: 12:10 - 12:20   ████████████
        (reads last_check_time = 12:10)
        → saves last_check_time = 12:20
```

**Benefits:**
- **No Gaps:** Each window starts where the previous one ended
- **No Overlaps:** No duplicate score processing
- **Shared Across Pods:** Redis key is shared, so whichever pod wins the lock continues from the right time
- **Resilient:** If Redis is unavailable, falls back to 10-minute lookback

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

**Static Configuration (environment variables):**

```bash
# Langfuse Configuration
LANGFUSE_SECRET_KEY=sk-lf-your-secret-key
LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key
LANGFUSE_BASEURL=https://periscope.breeze.in

# Score Monitoring
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
SLACK_TAG_USERS=narsimha.reddy   # Comma-separated Slack usernames or mention formats to tag in alerts

# Enable/Disable Monitoring
ENABLE_BB_LANGFUSE_MONITORING_LOOP=true

# Check Interval (10 minutes in production)
SCORE_CHECK_INTERVAL_SECONDS=600
```

**Dynamic Configuration (stored in Redis, changeable at runtime):**

```bash
# Evaluators with thresholds (format: "name:threshold,name:threshold")
# Thresholds are on a 1-10 scale. Scores below threshold trigger alerts.
# Default threshold is 5 if not specified.
LANGFUSE_EVALUATORS="OUTCOME MISMATCH:5,HIGH LATENCY:7,transcript_quality"

# Daily summary hour (24-hour format, default: 21 = 9 PM)
DAILY_SUMMARY_HOUR=21
```

**Optional:**

```bash
# Redis Configuration (for distributed locking and dynamic config)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=your-password  # if required
```

### Configuration Validation

On startup, `initialize_langfuse_tasks()` validates:

```python
evaluators = await LANGFUSE_EVALUATORS()
if not (ENABLE_BB_LANGFUSE_MONITORING_LOOP and evaluators and SLACK_WEBHOOK_URL):
    logger.debug("Langfuse tasks skipped - missing required configuration")
    return False
```

### Evaluator Names and Thresholds

**Format:** Comma-separated `name:threshold` pairs (stored in Redis, dynamic)

```bash
LANGFUSE_EVALUATORS="evaluator1:5,evaluator2:7,evaluator3"
```

**Example:**
```bash
LANGFUSE_EVALUATORS="OUTCOME MISMATCH:5,HIGH LATENCY:7,transcript_quality"
```

- Thresholds are on a **1-10 scale**; scores below the threshold trigger alerts
- If threshold is omitted, defaults to **5**
- Names must match exactly what's in Langfuse (case-sensitive)
- Config is fetched from Redis on each check cycle, so changes take effect without restart

## Guarantees

### 1. No Missed Evaluations ✓

**Guarantee:** Every score created in Langfuse will be checked

**How:**
- Last-check-time tracking ensures continuous, gap-free windows
- Each run starts from where the previous one ended
- Fallback to 10-minute lookback if Redis is unavailable

**Example:**
```
Score created at 12:05:30

Check at 12:10 (window: 12:00-12:10):
- Score at 12:05:30 is caught ✓
- last_check_time updated to 12:10

Check at 12:20 (window: 12:10-12:20):
- Only checks new scores after 12:10
```

### 2. No Duplicate Alerts ✓

**Guarantee:** Each failure generates exactly one Slack alert

**How:**
- Non-overlapping time windows via last-check-time tracking
- Each score only appears in exactly one check window
- `last_check_time` is updated **before** alert processing to prevent re-processing on crash

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
# In BackgroundTaskScheduler._execute_task()
try:
    redis_service = await get_redis_service()
    lock_acquired = await redis_service.set(
        key=lock_key, value="locked", nx=True, ex=task.interval_seconds
    )
except Exception as e:
    logger.error(f"Redis error for task '{task.name}': {e}. Skipping task.")
    return
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
success = await slack_alert.send(
    title=f"🔴 Breeze Buddy - {evaluator_name}",
    fields=fields,
    sections=sections,
    links=links,
    fallback_text=f"LLM Judge Failure: {evaluator_name} - Score {score_value}",
)

# Alert count tracking only happens on successful send
if success:
    await track_evaluator_alert(evaluator_name)
```

**Impact:**
- Alert not sent
- Alert count not tracked
- Since `last_check_time` was already updated, the score will NOT be re-fetched
- Failed alerts are logged for monitoring

**Result:** Score window moves forward; failed alerts are lost (by design, to prevent duplicate processing) ✓

### 5. Missing call_sid in Metadata

**Scenario:** Trace doesn't have `call_sid` in metadata

**Behavior:**
- Scores without `call_sid` are skipped for database storage
- Alerts are still sent for failing scores even without `call_sid`

**Impact:**
- Alert sent successfully
- Score not stored in database (no call record to associate with)
- Recording URL will be `N/A` in the alert

**Mitigation:** Ensure all traces include `call_sid` in metadata

## Monitoring

To monitor the health and performance of the Langfuse auto evaluation and alerting system, watch for these key indicators:

### Log Messages to Monitor

**Successful Operation:**
```
INFO: Background task scheduler started (loop interval: 60s, registered tasks: 1)
INFO: Acquired lock for task 'langfuse_score_monitor', executing...
INFO: Checking Langfuse scores from 2025-11-25T12:00:00+00:00 to 2025-11-25T12:10:00+00:00
INFO: Evaluator 'OUTCOME MISMATCH' (threshold=5): Found 15 total scores, 2 failing scores
INFO: Slack alert sent successfully: 🔴 Breeze Buddy - OUTCOME MISMATCH
INFO: Alert tracked successfully. New count for 'OUTCOME MISMATCH' on 2025-11-25: 3
INFO: Task 'langfuse_score_monitor' completed successfully
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
- Log shows "No failing scores found in this check"
- Or no log messages at all

**Possible Causes:**
- `ENABLE_BB_LANGFUSE_MONITORING_LOOP=false` in environment
- `LANGFUSE_EVALUATORS` not configured or empty in Redis
- Langfuse credentials invalid or missing
- No actual failures in the time window

**Solutions:**
```bash
# Verify configuration
echo $ENABLE_BB_LANGFUSE_MONITORING_LOOP  # Should be "true"
# LANGFUSE_EVALUATORS is in Redis, check via redis-cli
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
- Redis not configured or unavailable (last_check_time cannot be stored)
- `last_check_time` Redis key lost (causes fallback to 10-minute lookback which may overlap with previous check)

**Solutions:**
```bash
# Verify Redis configuration
echo $REDIS_HOST  # Should be set
echo $REDIS_PORT  # Should be set

# Check Redis connectivity
redis-cli -h $REDIS_HOST -p $REDIS_PORT ping

# Check last_check_time in Redis
redis-cli -h $REDIS_HOST -p $REDIS_PORT GET "langfuse:score_monitor:last_check_time"
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
