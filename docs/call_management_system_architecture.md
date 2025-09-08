# Call Management System Architecture

## Executive Summary

This system manages outbound confirmation calls with retries and concurrency control.
It is designed to be **stateless between retries** by delegating scheduling to an **external process tracker (PT)**.

Key points:

* **Outbound numbers pool**: Each number supports multiple concurrent channels.
* **Lead process**: Represents the lifecycle of contacting a lead, including retries and outcomes.
* **External process tracker**: Handles job scheduling.
* **Synchronous completion handling**: No webhook required; status updates and retry rescheduling occur immediately after call completion.

---

## Data Model

### 1. Outbound Numbers (`outbound_numbers`)

**Purpose**: Manages the pool of available phone numbers for outbound calls.

| Column             | Type        | Constraints      | Description                           |
| ------------------ | ----------- | ---------------- | ------------------------------------- |
| `id`               | UUID        | Primary Key      | Unique identifier                     |
| `number`           | VARCHAR(20) | NOT NULL, UNIQUE | Phone number in E.164 format          |
| `provider`         | VARCHAR(50) | NOT NULL         | Provider name (Twilio, Exotel, etc.)  |
| `status`           | ENUM        | NOT NULL         | `AVAILABLE` \| `IN_USE` \| `DISABLED` |
| `channels`         | INTEGER     | NULL             | Currently used channels               |
| `maximum_channels` | INTEGER     | NULL             | Maximum concurrent channels supported |
| `created_at`       | TIMESTAMP   | DEFAULT NOW()    | Record creation time                  |
| `updated_at`       | TIMESTAMP   | DEFAULT NOW()    | Last modification time                |

**Indexes**:

* `idx_outbound_numbers_status` on (`status`)
* `idx_outbound_numbers_provider_status` on (`provider`, `status`)

---

### 2. Lead Process (`lead_process`)

**Purpose**: Tracks the lifecycle of contacting a lead.

| Column               | Type         | Constraints   | Description                                                 |
| -------------------- | ------------ | ------------- | ----------------------------------------------------------- |
| `id`                 | UUID         | Primary Key   | Unique identifier                                           |
| `outbound_number_id` | UUID         | NULLABLE      | Assigned outbound number                                    |
| `workflow_name`      | VARCHAR(100) | NOT NULL      | Workflow type identifier (e.g., `order_confirmation`)       |
| `attempt_count`      | INTEGER      | DEFAULT 0     | Current attempt number                                      |
| `max_retries`        | INTEGER      | DEFAULT 3     | Maximum retry attempts                                      |
| `retry_window_sec`   | INTEGER      | DEFAULT 300   | Seconds between attempts                                    |
| `next_attempt_at`    | TIMESTAMP    | DEFAULT NOW() | Earliest next attempt time                                  |
| `payload`            | JSONB        | NULLABLE      | Call payload (order/customer info)                          |
| `recording_url`      | VARCHAR(500) | NULLABLE      | Call recording URL                                          |
| `outcome`            | ENUM         | NULLABLE      | `NO_ANSWER` \| `BUSY` \| `CANCEL` \| `CONFIRM` \| `UNKNOWN` |
| `call_id`            | VARCHAR(100) | NULLABLE      | Provider’s call identifier (SID)                            |
| `created_at`         | TIMESTAMP    | DEFAULT NOW() | Process start time                                          |
| `updated_at`         | TIMESTAMP    | DEFAULT NOW() | Last state change                                           |

**Indexes**:

* `idx_lead_process_status_next_attempt` on (`status`, `next_attempt_at`)
* `idx_lead_process_lead_id` on (`id`)

---

## Workflow Detail

The workflow has **two phases**:

---

### Phase 1: Job Creation

**Flow**:

```
Client → API (/order-confirmation)
        |
        v
Insert lead_process (attempt_count=0, status=PENDING)
        |
        v
Schedule job NOW in external tracker
```

**Steps**:

1. Client sends API request with call payload.
2. System creates a `lead_process` row with:

   * `attempt_count = 0`
   * `status = PENDING`
   * `payload` stored for later use
3. Immediately call **external process tracker (PT)** to schedule the first execution at `now()`.

---

### Phase 2: Job Processing

**Flow**:

```
External Tracker triggers
        |
        v
initiate_call()
   |
   +--> allocate_number (check channels)
   |
   +--> Place call (wait for completion)
   |
   +--> Update lead_process with outcome
   |
   +--> release_number
   |
   +--> handle_completion()
           |
           +---> CONFIRM → COMPLETED
           |
           +---> NO_ANSWER / BUSY → retry (if attempts left)
           |
           +---> CANCEL / ERROR → FAILED
```

**Steps**:

1. PT triggers `initiate_call(lead_id)`.
2. System selects an available outbound number (respecting `maximum_channels`).

   * If no number available → reschedule job after 60 seconds.
3. Call is placed synchronously via provider.
4. On completion:

   * Update `lead_process` with `outcome`, `call_id`, `recording_url`.
   * Release the outbound number channel.
5. Based on outcome:

   * **CONFIRM** → Mark as `COMPLETED`.
   * **NO\_ANSWER / BUSY** → Increment `attempt_count`.

     * If attempts remain → reschedule in PT after `retry_window_sec`.
     * Else mark as `RETRY_LIMIT_REACHED`.
   * **CANCEL / ERROR / UNKNOWN** → Mark as `FAILED`.

---

## Pseudocode

### API Trigger

```python
def order_confirmation_api(payload):
    lead = create_lead_process(
        workflow_name="order_confirmation",
        payload=payload,
        attempt_count=0,
        max_retries=3,
        retry_window_sec=300,
        outcome=None
    )

    external_process_tracker.schedule(
        call_id=lead.id,
        run_at=now()
    )
```

---

### Call Execution

```python
def initiate_call(lead_id):
    lead = get_lead_process(lead_id)

    number = allocate_number()
    if not number:
        # No capacity, retry later
        external_process_tracker.schedule(lead.id, run_at=now() + timedelta(seconds=60))
        return

    outcome, call_id, recording_url = provider.start_and_wait_for_call(
        lead.payload, number
    )

    # Update lead process
    lead.outbound_number_id = number.id
    lead.call_id = call_id
    lead.recording_url = recording_url
    lead.outcome = outcome

    release_number(number)

    handle_completion(lead)
```

---

### Outcome Handling

```python
def handle_completion(lead):
    if lead.outcome in ["CONFIRM"]:
        lead.status = "COMPLETED"

    elif lead.outcome in ["NO_ANSWER", "BUSY"]:
        if lead.attempt_count + 1 < lead.max_retries:
            lead.attempt_count += 1
            lead.status = "PENDING"
            lead.next_attempt_at = now() + timedelta(seconds=lead.retry_window_sec)
            external_process_tracker.schedule(
                call_id=lead.id,
                run_at=lead.next_attempt_at
            )
        else:
            lead.status = "RETRY_LIMIT_REACHED"

    elif lead.outcome in ["CANCEL"]:
        lead.status = "FAILED"

    else:  # UNKNOWN / ERROR
        lead.status = "FAILED"

    save(lead)
```

---

## Implementation Considerations

* **Channels control**: outbound number can handle multiple concurrent calls up to `maximum_channels`.
* **Retry scheduling**: delegated to PT, no in-system cron needed.
* **Synchronous completion**: no webhook; provider call response directly updates DB and reschedules PT if needed.
* **Audit trail**: `lead_process` itself holds all outcomes; optional `lead_attempts` table can be added for deeper logging if required.
* **Scalability**: external PT decouples retry scheduling load from core system.

---
