# Call Pre-Checks

> **Status:** Fully **implemented and in production**. This document describes the architecture and design of the pre-check system.

## Overview

A pluggable pre-check system that runs before a call is initiated from the backlog. Pre-checks validate whether a call should proceed, supporting multiple check types (starting with external API checks). Pre-checks are configurable **per merchant per template** (and optionally per shop).

---

## Current Call Flow (Relevant Section)

In `app/ai/voice/agents/breeze_buddy/managers/calls.py` → `process_backlog_leads()`:

```
1. Cleanup stuck leads
2. Fetch BACKLOG leads
3. For each lead:
   a. Acquire lock
   b. Get call_execution_config → check enable_calling
   c. Check calling hours
   d. Get template
   e. Prepare initial greeting
   f. Get available outbound number
   g. Acquire number
   h. make_call()
```

**Pre-checks will be inserted between step (d) and step (e)** — after we have both the config and the template, but before we commit resources (greeting synthesis, number acquisition).

---

## Architecture Decision

**Store pre-checks on `call_execution_config` table** as a new `pre_checks` JSONB column.

Rationale:
- `call_execution_config` is the "should we call?" decision layer (already has `enable_calling`, calling hours, retry policy)
- Pre-checks are another "should we call?" decision — they belong here semantically
- Already scoped per merchant + template + optional shop
- Template secrets remain accessible at runtime (template is fetched before pre-checks run) for placeholder resolution in HTTP requests

---

## Pre-Check Configuration Schema

The `pre_checks` column stores a JSON array of pre-check configurations:

```json
{
  "pre_checks": [
    {
      "type": "external_api",
      "name": "ncpr_check",
      "enabled": true,
      "http_request": {
        "url": "https://api.merchant.com/can-call",
        "method": "POST",
        "headers": {
          "X-Api-Key": "{api_key}"
        },
        "body": {
          "phone_number": "{customer_mobile_number}",
          "order_id": "{request_id}"
        },
        "auth": {
          "type": "bearer",
          "token": "{pre_check_api_token}"
        },
        "timeout": 5,
        "max_retries": 2
      },
      "response_config": {
        "response_field": "can_call",
        "response_field_value":true 
      },
      "default_on_failure": "proceed"
    }
  ]
}
```

### Pre-Check Types

| Type | Description | Status |
|------|-------------|--------|
| `external_api` | Hits an external HTTP API to get a go/no-go decision | Implementing now |
| _(future)_ | Rate limiting, DNC list, custom logic, etc. | Extensible via `type` field |

### External API Pre-Check Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | `"external_api"` |
| `name` | string | Yes | Human-readable name for logging (e.g., `"ncpr_check"`) |
| `enabled` | bool | Yes | Toggle this pre-check on/off without removing config |
| `http_request` | HttpRequestConfig | Yes | HTTP request configuration (reuses existing `HttpRequestConfig` model) |
| `response_config.response_field` | string | Yes | JSON field name in response that contains value |
| `response_config.response_field_value` value of the response_field |
| `default_on_failure` | string | Yes | What to assume if the API call fails/times out. `"proceed"` = fail-open, `"skip"` = fail-closed |

### Expected External API Response Contract

The external service should return a JSON response like:

```json
{
  "can_proceed": true,
  "reason": "Customer is eligible for call"
}
```

Or to block the call:

```json
{
  "can_proceed": false,
  "reason": "Customer on DNC list"
}
```

The field names (`can_proceed`, `reason`) are configurable via `response_config.response_field` and `response_config.response_field_value`.

### Placeholder Resolution

Placeholders in `{curly_braces}` are resolved from two sources (merged):
1. **Lead payload** (`lead.payload`) — e.g., `{customer_mobile_number}`, `{request_id}`
2. **Template secrets** (`template.secrets`) — e.g., `{api_key}`, `{pre_check_api_token}`

This follows the exact same pattern as hooks and global HTTP functions in the existing codebase.

---

## File Changes

### 1. Database Migration

**New file:** `app/database/migrations/014_add_pre_checks_column.sql`

```sql
ALTER TABLE call_execution_config
    ADD COLUMN IF NOT EXISTS pre_checks JSONB DEFAULT NULL;
```

### 2. Pydantic Schema Models

**Modified file:** `app/schemas/breeze_buddy/core.py`

Add new models:
- `PreCheckResponseConfig` — defines expected response field mapping
- `ExternalApiPreCheck` — full config for an external API pre-check
- `PreCheckConfig` — discriminated union for all pre-check types

Update `CallExecutionConfig` model to include `pre_checks: Optional[List[PreCheckConfig]]`.

Update `CreateCallExecutionConfigRequest` and `UpdateCallExecutionConfigRequest` to accept `pre_checks`.

### 3. New Pre-Check Executor Module

**New file:** `app/ai/voice/agents/breeze_buddy/managers/pre_checks.py`

Contains:
- `run_pre_checks()` — orchestrator that runs all enabled pre-checks for a lead
- `_execute_external_api_pre_check()` — executes a single external API pre-check
- `_resolve_pre_check_fields()` — resolves placeholders from lead payload + template secrets
- `PreCheckResult` dataclass — result of a pre-check (passed, reason, name)

### 4. Integration into Backlog Processing

**Modified file:** `app/ai/voice/agents/breeze_buddy/managers/calls.py`

In `process_backlog_leads()`, add pre-check execution between template retrieval and greeting preparation:

```python
# After template retrieval (existing line ~372-380)
# NEW: Run pre-checks
if config.pre_checks:
    pre_check_passed = await run_pre_checks(
        pre_checks=config.pre_checks,
        lead=locked_lead,
        template=template,
    )
    if not pre_check_passed:
        logger.info(f"Pre-checks failed for lead {locked_lead.id}, skipping.")
        await release_lock_on_lead_by_id(locked_lead.id)
        continue
```

### 5. Database Layer Updates

**Modified file:** `app/database/queries/breeze_buddy/call_execution_config.py`
- Update `insert_call_execution_config_query()` to include `pre_checks` column
- Update `update_call_execution_config_query()` to support updating `pre_checks`

**Modified file:** `app/database/accessor/breeze_buddy/call_execution_config.py`
- Update `create_call_execution_config()` to accept `pre_checks` parameter
- Update `update_call_execution_config()` to accept `pre_checks` parameter

**Modified file:** `app/database/decoder/breeze_buddy/call_execution_config.py`
- Update decoder to parse `pre_checks` from JSONB

### 6. API Layer Updates

**Modified file:** `app/api/routers/breeze_buddy/configurations/handlers.py`
- Update create/update handlers to pass `pre_checks` through

### 7. Lead Status for Pre-Check Failure (Optional Enhancement)
When pre-checks fail:
- The pre-check result is logged
- Updates the outcome and marks as `FINISHED`
However, we should update the lead's `meta_data` with the pre-check failure reason for observability.

---

## Execution Flow (Detailed)

```
process_backlog_leads()
  │
  ├── For each BACKLOG lead:
  │     ├── Acquire lock
  │     ├── Get call_execution_config
  │     ├── Check enable_calling
  │     ├── Check calling hours
  │     ├── Get template
  │     │
  │     ├── ★ NEW: Run pre-checks ★
  │     │     ├── For each enabled pre-check in config.pre_checks:
  │     │     │     ├── If type == "external_api":
  │     │     │     │     ├── Merge lead.payload + template.secrets into resolution context
  │     │     │     │     ├── Resolve placeholders in HTTP request config
  │     │     │     │     ├── Execute HTTP request (with retry, timeout)
  │     │     │     │     ├── On success: parse response, check should_proceed_field
  │     │     │     │     │     ├── If true → pre-check PASSED
  │     │     │     │     │     └── If false → pre-check FAILED (log reason)
  │     │     │     │     └── On failure: use default_on_failure value
  │     │     │     └── (future types handled here)
  │     │     │
  │     │     └── If ANY pre-check fails → skip lead, release lock, continue
  │     │
  │     ├── Prepare initial greeting (existing)
  │     ├── Get outbound number (existing)
  │     ├── Acquire number (existing)
  │     └── make_call() (existing)
```

---

## Key Design Decisions

1. **Pre-checks are ALL-AND logic**: All enabled pre-checks must pass for the call to proceed. If any one fails, the call is skipped.

2. **Fail-open vs fail-closed is per-check**: Each pre-check has its own `default_on_failure` flag. Critical checks (e.g., DNC) should be `false` (fail-closed). Advisory checks can be `true` (fail-open).

3. **Reuses existing HTTP infrastructure**: `HttpRequestConfig`, `HttpAuthConfig`, `HttpRequestExecutor`, placeholder resolution — all existing patterns are reused.

4. **No new database table**: Pre-checks are a JSONB column on `call_execution_config`, keeping the config hierarchy simple.

5. **Backward compatible**: `pre_checks` defaults to NULL. Existing configs with no pre-checks continue to work unchanged.

---

## Files Summary

| File | Action | Description |
|------|--------|-------------|
| `app/database/migrations/014_add_pre_checks_column.sql` | **NEW** | Add `pre_checks` JSONB column |
| `app/schemas/breeze_buddy/core.py` | **MODIFY** | Add pre-check Pydantic models, update config schemas |
| `app/ai/voice/agents/breeze_buddy/managers/pre_checks.py` | **NEW** | Pre-check executor module |
| `app/ai/voice/agents/breeze_buddy/managers/calls.py` | **MODIFY** | Integrate pre-checks into backlog processing |
| `app/database/queries/breeze_buddy/call_execution_config.py` | **MODIFY** | Add `pre_checks` to insert/update queries |
| `app/database/accessor/breeze_buddy/call_execution_config.py` | **MODIFY** | Add `pre_checks` param to create/update functions |
| `app/database/decoder/breeze_buddy/call_execution_config.py` | **MODIFY** | Parse `pre_checks` JSONB |
| `app/api/routers/breeze_buddy/configurations/handlers.py` | **MODIFY** | Pass `pre_checks` in API create/update |
