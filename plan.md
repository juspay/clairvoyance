# Blacklist Phone Numbers Feature — Implementation Plan

## Context

Breeze Buddy makes outbound calls to customers via a cron-driven backlog system (`process_backlog_leads` in `managers/calls.py`). Leads are pushed via `push_lead_handler` and later picked up by the cron job which initiates calls. The customer phone number lives inside `lead.payload["customer_mobile_number"]`.

There are **two interception points** where blacklisting must be enforced:
1. **Lead Push time** (`push_lead_handler`) — reject the lead upfront if the number is blacklisted
2. **Call Pick time** (`process_backlog_leads`) — skip the lead if the number was blacklisted after push

There is also the **retry flow** (`_retry_call`) which creates a new lead for retry — this inherits the same phone number, so it will be caught by check #2.

---

## Recommended Approach: Dedicated `blacklisted_numbers` DB Table + Redis Cache

### Why this approach?

- **DB table** gives durability, auditability (who added it, when, reason), and queryability
- **Redis cache** gives O(1) lookup speed during the hot path (cron processing thousands of leads)
- Follows the same architectural patterns already used in this codebase (PostgreSQL + Redis, accessor/query pattern, Pydantic schemas)
- Scoping by `merchant_id` allows per-merchant blacklists (a number blacklisted for merchant A can still be called by merchant B)
- Global blacklist support (merchant_id = NULL) for security/compliance blocks that apply to everyone

---

## Implementation Steps

### Step 1: Database Migration

**File:** `app/database/migrations/017_create_blacklisted_numbers_table.sql`

```sql
CREATE TABLE IF NOT EXISTS blacklisted_numbers (
    id VARCHAR(255) PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    merchant_id VARCHAR(255),              -- NULL = global blacklist
    reason VARCHAR(500),                   -- e.g., "customer request", "DND", "fraud"
    created_by VARCHAR(255),               -- who added it
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    UNIQUE(phone_number, merchant_id)      -- same number can be blacklisted per-merchant
);

-- For fast lookups during call processing
CREATE INDEX IF NOT EXISTS idx_blacklisted_numbers_phone
    ON blacklisted_numbers (phone_number);
CREATE INDEX IF NOT EXISTS idx_blacklisted_numbers_merchant
    ON blacklisted_numbers (merchant_id);
CREATE INDEX IF NOT EXISTS idx_blacklisted_numbers_phone_merchant
    ON blacklisted_numbers (phone_number, merchant_id);
```

### Step 2: Pydantic Schema

**File:** `app/schemas/breeze_buddy/core.py` (add to existing file)

```python
class BlacklistedNumber(BaseModel):
    """Blacklisted phone number model"""
    id: str
    phone_number: str
    merchant_id: Optional[str] = None  # NULL = global blacklist
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class BlacklistNumberRequest(BaseModel):
    """Request to blacklist a phone number"""
    phone_number: str
    merchant_id: Optional[str] = None
    reason: Optional[str] = None
```

### Step 3: Database Queries

**File:** `app/database/queries/breeze_buddy/blacklisted_numbers.py`

Queries needed:
- `insert_blacklisted_number_query` — add a number to the blacklist
- `delete_blacklisted_number_query` — remove a number from the blacklist
- `is_number_blacklisted_query` — check if a number is blacklisted (for a merchant OR globally)
- `get_all_blacklisted_numbers_query` — list all blacklisted numbers (with optional merchant filter)
- `get_blacklisted_number_by_phone_query` — get blacklist entry by phone number

The **`is_number_blacklisted_query`** is the critical one:
```sql
SELECT EXISTS(
    SELECT 1 FROM blacklisted_numbers
    WHERE phone_number = $1
    AND (merchant_id = $2 OR merchant_id IS NULL)
) AS is_blacklisted;
```

### Step 4: Database Decoder

**File:** `app/database/decoder/breeze_buddy/blacklisted_numbers.py`

Decode DB records into `BlacklistedNumber` Pydantic models (following the existing pattern in `lead_call_tracker` decoder).

### Step 5: Database Accessor

**File:** `app/database/accessor/breeze_buddy/blacklisted_numbers.py`

Functions:
- `add_to_blacklist(...)` — insert + invalidate Redis cache
- `remove_from_blacklist(...)` — delete + invalidate Redis cache
- `is_number_blacklisted(phone_number, merchant_id)` — check Redis first, fallback to DB
- `get_all_blacklisted_numbers(merchant_id=None)` — list all

Register these in `app/database/accessor/__init__.py`.

### Step 6: Redis Cache Layer

In the accessor's `is_number_blacklisted` function:
- **Cache key pattern:** `blacklist:{normalized_phone_number}`
- **Cache value:** JSON set of merchant_ids (including `"__global__"` for NULL merchant entries)
- **TTL:** 5 minutes (balance between freshness and performance)
- On cache miss: query DB, populate cache
- On blacklist add/remove: invalidate the cache key for that phone number

This keeps the hot-path (cron loop) fast — Redis SET membership check is O(1).

### Step 7: Enforce at Lead Push Time

**File:** `app/api/routers/breeze_buddy/leads/handlers.py` — `push_lead_handler`

Add check early in the function (after template validation, before creating the lead):
```python
customer_mobile = req.payload.get("customer_mobile_number")
if customer_mobile:
    if await is_number_blacklisted(customer_mobile, req.merchant):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Phone number is blacklisted and cannot be called",
        )
```

### Step 8: Enforce at Call Pick Time

**File:** `app/ai/voice/agents/breeze_buddy/managers/calls.py` — `process_backlog_leads`

Add check inside the lead processing loop (after acquiring the lock, before acquiring a number):
```python
customer_mobile = (locked_lead.payload or {}).get("customer_mobile_number")
if customer_mobile and await is_number_blacklisted(customer_mobile, locked_lead.merchant_id):
    logger.info(f"Skipping lead {locked_lead.id} - phone number is blacklisted")
    await update_lead_call_completion_details(
        id=locked_lead.id,
        status=LeadCallStatus.FINISHED,
        outcome="BLACKLISTED",
        meta_data={"reason": "Phone number is blacklisted"},
        call_end_time=datetime.now(timezone.utc),
    )
    await release_lock_on_lead_by_id(locked_lead.id)
    continue
```

### Step 9: API Router for Blacklist Management

**File:** `app/api/routers/breeze_buddy/blacklist/` (new directory)

- `__init__.py` — router setup
- `handlers.py` — business logic handlers
- `rbac.py` — access control (admin-only for add/remove)

Endpoints:
- `POST /blacklist` — add a number to the blacklist
- `DELETE /blacklist/{phone_number}` — remove a number from the blacklist
- `GET /blacklist` — list all blacklisted numbers (with optional `merchant_id` query param)
- `GET /blacklist/check/{phone_number}` — check if a number is blacklisted

### Step 10: Register the Router

**File:** `app/api/routers/breeze_buddy/__init__.py`

Add the blacklist router to the existing breeze_buddy router group.

---

## Architecture Diagram

```text
                    ┌─────────────────────┐
                    │   Push Lead API      │
                    │  (leads/handlers.py) │
                    └─────────┬───────────┘
                              │
                              ▼
                    ┌─────────────────────┐
              ┌────►│ is_number_blacklisted│◄────┐
              │     └─────────┬───────────┘     │
              │               │                  │
              │        ┌──────┴──────┐           │
              │        ▼             ▼           │
              │    ┌───────┐   ┌──────────┐     │
              │    │ Redis │   │ Postgres │     │
              │    │ Cache │   │   Table  │     │
              │    └───────┘   └──────────┘     │
              │                                  │
    ┌─────────┴───────────┐         ┌───────────┴──────────┐
    │  Cron: Process       │         │  Blacklist Mgmt API  │
    │  Backlog Leads       │         │  (add/remove/list)   │
    │  (managers/calls.py) │         │  Invalidates cache   │
    └─────────────────────┘         └──────────────────────┘
```

---

## Key Design Decisions

1. **Per-merchant + global scope:** A number can be blacklisted globally (compliance/security) or per-merchant (merchant-specific DND). The check queries both.

2. **Dual enforcement (push + pick):** Push-time rejection gives immediate feedback to the API caller. Pick-time check is the safety net for numbers blacklisted after the lead was already pushed.

3. **Redis cache with short TTL:** The cron can process hundreds of leads per cycle. Hitting the DB for each blacklist check would be expensive. Redis cache with 5-min TTL keeps it fast while ensuring newly blacklisted numbers take effect within minutes.

4. **BLACKLISTED outcome:** Adding "BLACKLISTED" as a lead outcome makes it visible in analytics and reporting. Webhook notifications inform the merchant.

5. **Phone number normalization:** Before any lookup, normalize the phone number (strip spaces, ensure country code format). This prevents bypassing the blacklist with formatting tricks.

6. **Following existing patterns:** The implementation follows the exact same layered architecture (queries → decoder → accessor → handler → router) used by every other feature in this codebase.
