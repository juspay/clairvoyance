# Evaluator Actions

Auto-correct call outcomes when Langfuse evaluators detect issues.

## Problem

When a call completes, the voice agent records an outcome (e.g., `BUSY`, `NO_ANSWER`). Sometimes this is incorrect:
- Voicemail misdetection: Recorded as `BUSY`, actually `VOICEMAIL`
- Outcome mismatch: Recorded as `CANCEL`, actually `CONFIRM`

This causes:
- Unnecessary retries (calling a customer who already confirmed)
- Wrong Shopify tags
- Poor customer experience

## What It Does

When an evaluator score is below threshold:
1. **Update DB** - Correct the outcome in `lead_call_tracker`
2. **Cancel Retries** - Stop pending calls for this request_id (any outcome change)
3. **Send Webhook** - Notify Nautilus to update Shopify tags
4. **Send Slack Alert** - Notify team with detailed action status

## Flow

```
Call completes → Voice Agent records outcome → Webhook to Nautilus (initial)
                    ↓
              Langfuse evaluates transcript
                    ↓
              Score < threshold?
                    ↓ Yes
              Check EVALUATOR_ACTIONS config
                    ↓
              Execute OutcomeUpdateAction:
                1. Update DB outcome
                2. Cancel pending retries (if request_id exists)
                3. Webhook to Nautilus (correction)
                    ↓
              Send Slack alert with status
```

## Configuration

### Redis: `EVALUATOR_ACTIONS`

The config is split into two parts:
- **`action_config`**: Controls behavior (what outcome to set, extraction rules)
- **`action_steps`**: Controls which steps to execute (flags for each step)

```json
{
  "VOICEMAIL DETECTOR": {
    "action_type": "outcome_update",
    "action_config": {
      "outcome": "VOICEMAIL",
      "disallowed_outcome_changes": {"*": ["BUSY"]}
    },
    "action_steps": {
      "update_in_db": true,
      "send_reporting_webhook": true,
      "cancel_retries": true
    }
  },
  "OUTCOME CORRECTNESS": {
    "action_type": "outcome_update",
    "action_config": {
      "outcome_key": "$.actual_outcome"
    },
    "action_steps": {
      "update_in_db": true,
      "send_reporting_webhook": true,
      "cancel_retries": true
    }
  }
}
```

### Redis: `LANGFUSE_EVALUATORS`
```json
{"VOICEMAIL DETECTOR": 5, "OUTCOME CORRECTNESS": 5}
```

### Action Config Options

| Section | Option | Description |
|---------|--------|-------------|
| `action_config` | `outcome` | Direct outcome value (e.g., `"VOICEMAIL"`) |
| `action_config` | `outcome_key` | JSON path to extract from comment (e.g., `"$.actual_outcome"`) |
| `action_config` | `allowed_outcome_changes` | Permit transitions. `"*": ["CONFIRM"]` allows any→CONFIRM |
| `action_config` | `disallowed_outcome_changes` | Block transitions. `"*": ["BUSY"]` blocks any change TO BUSY |
| `action_steps` | `update_in_db` | Update outcome in database (default: true) |
| `action_steps` | `cancel_retries` | Cancel pending retries for this lead (default: true) |
| `action_steps` | `send_reporting_webhook` | Send webhook to Nautilus (default: true) |

### Lead Payload Requirement

Lead must have `reporting_webhook_url` in payload:
```json
{
  "reporting_webhook_url": "https://nautilus.example.com/apps/breeze-buddy/webhooks/clairvoyance",
  "customer_mobile_number": "+919876543210"
}
```

## Configuration Details

### Step Names Used in `action_steps`

The step names from `action_steps` keys are used directly in Slack alerts:
- `update_in_db` - Updates the outcome in database
- `send_reporting_webhook` - Sends webhook to Nautilus
- `cancel_retries` - Cancels pending retry calls

### Outcome Extraction from Comment

When using `outcome_key`, the system:
1. Gets `score["comment"]` (full text from evaluator)
2. Finds JSON object `{...}` at the end using regex
3. Parses JSON and extracts the field specified in `outcome_key`

**Example evaluator response:**
```
The recorded outcome was CANCEL but based on the conversation analysis, the customer actually confirmed the order. {"recorded_outcome": "CANCEL","actual_outcome":"CONFIRM"}
```

**Config to extract `actual_outcome`:**
```json
{
  "action_config": {
    "outcome_key": "$.actual_outcome"
  }
}
```

## Files Changed

| File | Changes |
|------|---------|
| `tasks/actions/actions.py` | `OutcomeUpdateAction`, `ActionResult`, `ActionExecutor` with step tracking |
| `tasks/actions/utils.py` | JSON extraction and parsing helpers |
| `score_monitor/score.py` | Integrated action execution into ScoreMonitor, Slack alert with action_status |
| `slack/alert.py` | Added `action_status` parameter for full-width step status display |
| `lead_call_tracker.py` (queries) | `cancel_pending_retries_by_request_id_query` |
| `lead_call_tracker.py` (accessor) | `cancel_pending_retries_by_request_id` |
| `dynamic.py` | `EVALUATOR_ACTIONS()` config getter |

### Log Context Usage

Actions use `app/core/logger/context.py` for automatic context injection:

```python
from app.core.logger.context import set_log_context, update_log_context, clear_log_context

# At start of action
set_log_context(call_sid=call_sid)

# When lead is found
update_log_context(lead_id=str(lead.id), request_id=lead.request_id, current_outcome=lead.outcome)

# At end of action
clear_log_context()
```

All logs within the action automatically include these fields in JSON output.

## ActionResult Status

Each step returns: `SUCCESS` | `SKIPPED` | `FAILED` | `ERROR`

### Status Icons

| Status | Icon | Used For |
|--------|------|----------|
| `SUCCESS` | ✅ | Step completed successfully |
| `SKIPPED` | ⏭️ | Step not executed (disabled or precondition failed) |
| `FAILED` | ❌ | Step failed with error |
| `ERROR` | ⚠️ | Unexpected error occurred |

### ActionResult Fields

```python
@dataclass
class ActionResult:
    success: bool                    # Overall action success
    db_update: Optional[str]         # DB step status
    cancel_retries: Optional[str]    # Retry cancellation status
    reporting_webhook: Optional[str]  # Webhook step status
    error_message: Optional[str]     # Error details if any
    outcome_change: Optional[str]    # "OLD -> NEW" format
    canceled_count: Optional[int]    # Number of retries cancelled
    lead_id: Optional[str]           # Lead ID for alerting
    step_results: Optional[Dict[str, str]]  # Raw step statuses keyed by step name
```

### Step Status Legend

| Step | SUCCESS | SKIPPED | FAILED/ERROR |
|------|---------|---------|--------------|
| `update_in_db` | DB updated | Precondition failed or disabled | Exception |
| `cancel_retries` | Cancelled or none existed | No request_id or disabled | Exception |
| `send_reporting_webhook` | 200 OK | No URL or disabled | Non-200 or exception |

## Webhook Details

- **URL**: From `lead.payload.reporting_webhook_url` (no new env vars)
- **HMAC**: Uses existing `ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY`
- **Signature**: Base64-encoded SHA256 HMAC

### Webhook Payload
```json
{
  "callSid": "CAabc123",
  "outcome": "VOICEMAIL",
  "orderId": "ORDER-123",
  "attemptCount": 1,
  "evaluatorName": "VOICEMAIL DETECTOR",
  "correctedBy": "evaluator_action",
  "previousOutcome": "BUSY"
}
```

## Slack Alert Format

```
🔴 Breeze Buddy - VOICEMAIL DETECTOR

Score: 0 (BELOW THRESHOLD)    Timestamp: 2026-02-27 10:30:00
Lead ID: lead-abc123          Trace ID: `trace-xyz`
                              Call SID: `CAabc123...`
                              Recording: Listen

Action Status:
update_in_db: ✅
send_reporting_webhook: ✅
cancel_retries: ✅
Outcome: BUSY -> VOICEMAIL
```

### Alert Layout Details

1. **Header**: Red circle emoji + evaluator name
2. **Fields** (2-column layout):
   - Left: Score, Lead ID
   - Right: Timestamp, Trace ID, Call SID, Recording link
3. **Action Status** (full-width section below fields):
   - Each step on a new line with status icon
   - Uses raw `action_steps` keys (snake_case)
   - Shows outcome change at bottom
4. **User Mentions**: Configurable via `SLACK_TAG_USERS` env variable

### `ActionResult.to_slack_status()` Method

```python
def to_slack_status(self) -> str:
    """Generate formatted status for Slack alerts"""
    parts = []
    if self.step_results:
        for step_name, status in self.step_results.items():
            icon = self.STATUS_ICONS.get(status, "?")
            parts.append(f"{step_name}: {icon}")
    status_str = "\n".join(parts) if parts else "No actions"
    if self.outcome_change:
        status_str += f"\n*Outcome:* {self.outcome_change}"
    return status_str
```

## Logging

Uses log context (`app/core/logger/context.py`) - all logs automatically include:
- `call_sid` - Call identifier
- `lead_id` - Lead database ID
- `request_id` - Order/request ID
- `conversation_id` - Conversation/trace ID (when available)
- `current_outcome` - Original outcome
- `new_outcome` - Corrected outcome (when available)

```bash
# View all evaluator action logs
grep "call_sid" /var/log/clairvoyance/app.log | grep "EVALUATOR_ACTION"

# Or filter by conversation_id
grep "conversation_id.*Customer-Shop" /var/log/clairvoyance/app.log
```

JSON log output example:
```json
{
  "timestamp": "2026-02-26T10:30:00Z",
  "level": "INFO",
  "message": "[EVALUATOR_ACTION] DB_UPDATE SUCCESS",
  "call_sid": "CAabc123",
  "lead_id": "uuid-xxx",
  "request_id": "ORDER-123",
  "conversation_id": "Customer-Shop-2026-02-26_10-30-00",
  "current_outcome": "BUSY",
  "new_outcome": "VOICEMAIL"
}
```

Context is managed with `try/finally`:
```python
set_log_context(call_sid=call_sid)
update_log_context(lead_id=..., request_id=..., current_outcome=...)
if conversation_id:
    update_log_context(conversation_id=conversation_id)
# ... action logic ...
finally:
    clear_log_context()  # ALWAYS called
```

## Nautilus Integration

Nautilus handles evaluator correction webhooks at `src/routes/apps/breeze-buddy/webhooks/clairvoyance/+server.ts`.

When `correctedBy === 'evaluator_action'` is present in the webhook payload, Nautilus:
1. Updates the workflow outcome and metadata (regardless of workflow status — works on completed workflows too)
2. Replaces Shopify tags (removes old outcome tag, adds new one)
3. Adds an order note about the correction
4. Returns early (no re-billing)

## Example Scenarios

### Voicemail Detection
- **Initial**: Call recorded as `BUSY`, 3 retries scheduled
- **Evaluator**: Score 0, detects voicemail
- **Action**: DB updated to `VOICEMAIL`, 3 retries cancelled, webhook sent

### Outcome Mismatch
- **Initial**: Call recorded as `CANCEL`, 2 retries scheduled
- **Evaluator**: Score 2, comment says `{"actual_outcome": "CONFIRM"}`
- **Action**: DB updated to `CONFIRM`, 2 retries cancelled, webhook sent

## Test Calls

Evaluator actions are automatically skipped for test calls (`TELEPHONY_TEST`, `DAILY_TEST`).

- `execution_mode` is set as an OTEL span attribute during the call
- The score monitor reads it from the Langfuse trace metadata before processing actions
- If the trace belongs to a test call, the entire action (DB update, retry cancellation, webhook) is skipped
- Slack alerts and score storage still fire for test calls (monitoring visibility)

## Rollout Checklist

### Clairvoyance ✅
- [x] Implement `OutcomeUpdateAction` with log context
- [x] Add `ActionResult` dataclass with `step_results` dict
- [x] Add `STATUS_ICONS` mapping (✅, ⏭️, ❌, ⚠️)
- [x] Add `to_slack_status()` method for formatted output
- [x] Add `lead_id` field to ActionResult for alerting
- [x] Integrate into ScoreMonitor with action execution
- [x] Add `action_status` parameter to Slack alert
- [x] Implement 2-column fields + full-width Action Status layout
- [x] Use raw `action_steps` keys (snake_case) in alerts
- [x] Add Redis config with `action_config` + `action_steps` format
- [ ] Commit and deploy
- [ ] Verify logs via `grep "evaluator_action" /var/log/clairvoyance/app.log`

### Nautilus ✅
- [x] Add handler for `correctedBy === 'evaluator_action'`
- [x] Implement tag replacement logic
- [x] Add order note about correction
- [x] Test end-to-end

## Testing

### Test Scripts

| Script | Purpose |
|--------|---------|
| `scripts/test/demo_langfuse_server.py` | Mock Langfuse server for testing (port 8072) |
| `scripts/test/evaluator_actions_test_data.sql` | Insert test leads for E2E testing |
| `scripts/test/setup_evaluator_test_redis.sh` | Configure Redis for testing |

### Running E2E Test

```bash
# 1. Start demo Langfuse server
python scripts/test/demo_langfuse_server.py

# 2. Configure Redis (in another terminal)
./scripts/test/setup_evaluator_test_redis.sh

# 3. Insert test data
psql -d clairvoyance -f scripts/test/evaluator_actions_test_data.sql

# 4. Start the application
uvicorn app.main:app --reload

# 5. Verify results:
# - Lead outcome: CANCEL -> CONFIRM
# - Retry leads: Cancelled with CANCELLED_BY_OUTCOME_CORRECTION
# - Webhook: Received at demo server /webhook endpoint
# - Slack alert: Sent with Lead ID and Action Status
```

## Commit

```bash
git commit -m "feat: add evaluator-triggered actions for outcome corrections

- Add OutcomeUpdateAction handler with DB update, retry cancellation, webhook
- Add ActionResult dataclass with step_results dict for detailed tracking
- Add STATUS_ICONS mapping (✅, ⏭️, ❌, ⚠️) and to_slack_status() method
- Add lead_id field to ActionResult for Slack alert inclusion
- Split config into action_config (behavior) and action_steps (execution flags)
- Integrate action execution into ScoreMonitor polling loop
- Add action_status parameter to Slack alert (full-width section below fields)
- Use raw action_steps keys (snake_case) in alert display
- Add cancel_pending_retries_by_request_id query and accessor
- Add EVALUATOR_ACTIONS dynamic config from Redis"
```
