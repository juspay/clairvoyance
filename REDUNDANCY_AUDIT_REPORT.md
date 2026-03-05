# Breeze Buddy Code Redundancy Audit Report

**Date:** 2026-03-05
**Scope:** Breeze Buddy codebase — code, schemas, API routers, DB layers, agent services

---

## Table of Contents

1. [Safe to Remove (Dead Code)](#1-safe-to-remove-dead-code)
2. [Code Duplication (Consolidation Opportunities)](#2-code-duplication-consolidation-opportunities)
3. [Database Schema Redundancies](#3-database-schema-redundancies)
4. [Backward-Compatibility Code (Needs Your Input)](#4-backward-compatibility-code-needs-your-input)

---

## 1. Safe to Remove (Dead Code)

These items are confirmed unused via grep — no imports, no references outside their own definition.

### 1.1 `LoginRequest` in `types/models.py` (DEAD)
- **File:** `app/ai/voice/agents/breeze_buddy/types/models.py:20-22`
- **Why:** Identical `LoginRequest` exists in `app/schemas/breeze_buddy/auth.py:60-63` and is the one used everywhere. The `models.py` version has zero imports.

### 1.2 `LoginResponse` in `auth.py` (DEAD + DEPRECATED)
- **File:** `app/schemas/breeze_buddy/auth.py:67-71`
- **Why:** Docstring says `"deprecated - use TokenResponse"`. Never imported by any router or handler. Only re-exported through `__init__.py` files.

### 1.3 `LeadStatusCountResult` in `analytics.py` (DEAD)
- **File:** `app/schemas/breeze_buddy/analytics.py:181-199`
- **Why:** Not exported from any `__init__.py`, not imported anywhere in the codebase.

### 1.4 `get_template_handler()` in `templates/handlers.py` (DEAD)
- **File:** `app/api/routers/breeze_buddy/templates/handlers.py:152-200`
- **Why:** Never imported by the template router. The router uses `get_template_by_id_handler` and `list_templates_handler` instead. ~50 lines of dead code.

### 1.5 `filter_templates_by_rbac()` in `templates/rbac.py` (DEAD)
- **File:** `app/api/routers/breeze_buddy/templates/rbac.py:66-106`
- **Why:** Never imported or called. Templates use `apply_hierarchical_template_filters()` instead. ~40 lines.

### 1.6 `filter_numbers_by_rbac()` — No-Op Function (DEAD LOGIC)
- **File:** `app/api/routers/breeze_buddy/numbers/rbac.py:40-56`
- **Why:** Called in numbers router but does nothing — unconditionally returns input. The function body is just `return numbers`.

### 1.7 Unused `conference_result.get("conference_id")` expression (DEAD)
- **File:** `app/ai/voice/agents/breeze_buddy/handlers/internal/warm_transfer.py:161`
- **Why:** Bare expression — result is never assigned. Value already captured in `transfer_meta` on line 165.

### 1.8 `GET /logout` endpoint (DEPRECATED)
- **File:** `app/api/routers/breeze_buddy/auth/__init__.py:193-211`
- **Why:** Docstring explicitly says `[DEPRECATED] for backward compatibility with old session-based auth`. Modern `POST /auth/logout` exists. **Condition to remove:** Confirm no clients still use session-based auth.

---

## 2. Code Duplication (Consolidation Opportunities)

### 2.A — RBAC / Security Layer (~400+ lines removable)

**Root cause:** A centralized security module exists at `app/api/security/breeze_buddy/authorization.py` exporting `validate_merchant_access`, `validate_shop_access`, `apply_merchant_shop_filter`, `filter_by_shop_access` — but **no router uses it**. Every router independently reimplements the same patterns in local `rbac.py` files.

#### 2.A.1 — 5x Duplicate `validate_*_access()` functions (HIGH)

| File | Function |
|------|----------|
| `app/api/routers/breeze_buddy/configurations/rbac.py:14-63` | `validate_config_access()` |
| `app/api/routers/breeze_buddy/leads/rbac.py:14-63` | `validate_lead_access()` |
| `app/api/routers/breeze_buddy/leads/rbac.py:66-109` | `validate_lead_read_access()` |
| `app/api/routers/breeze_buddy/leads/rbac.py:112-163` | `validate_recording_access()` |
| `app/api/routers/breeze_buddy/templates/rbac.py:14-63` | `validate_template_access()` |

All follow identical logic: check admin role → check merchant_ids → check shop_identifiers. Only the log message noun differs.

**Fix:** Replace all with a single `validate_resource_access(current_user, merchant_id, shop_identifier, resource_name)` in the centralized security module.

#### 2.A.2 — 2x Duplicate `apply_hierarchical_*_filters()` (~110 lines)

| File | Function |
|------|----------|
| `app/api/routers/breeze_buddy/analytics/rbac.py:60-169` | `apply_hierarchical_filters()` |
| `app/api/routers/breeze_buddy/templates/rbac.py:143-248` | `apply_hierarchical_template_filters()` |

Same algorithm: extract accessible merchants/shops from JWT, validate, inject into filters.

**Fix:** Consolidate into the centralized `apply_merchant_shop_filter()` already in `authorization.py`.

#### 2.A.3 — 2x Duplicate `require_admin_access()` (~25 lines)

| File | Function |
|------|----------|
| `app/api/routers/breeze_buddy/numbers/rbac.py:14-37` | `require_admin_access()` |
| `app/api/routers/breeze_buddy/merchants/rbac.py:12-35` | `require_admin_access()` |

The blacklist router cross-imports from numbers, creating an awkward dependency.

**Fix:** Move to centralized security module.

#### 2.A.4 — 5x Inline RBAC in Credentials Router (~30 lines)

- `app/api/routers/breeze_buddy/credentials/__init__.py` — lines 56-71, 90-106, 126-135, 158-172, 185-189

Same merchant-validation pattern repeated in every endpoint. No `rbac.py` file exists for credentials.

**Fix:** Use centralized `validate_merchant_access()`.

#### 2.A.5 — Inline RBAC duplicating own module in configurations

- `app/api/routers/breeze_buddy/configurations/__init__.py:122-130`

`list_configurations()` uses inline RBAC check while `create_configuration()` and `get_configuration()` properly use `validate_config_access()` from the same module's `rbac.py`.

---

### 2.B — Database Layer Duplication (~300+ lines removable)

#### 2.B.1 — 4x Identical `get_row_count()` helper (HIGH)

| File | Lines |
|------|-------|
| `app/database/accessor/breeze_buddy/call_execution_config.py:30-34` | |
| `app/database/accessor/breeze_buddy/lead_call_tracker.py:40-44` | |
| `app/database/accessor/breeze_buddy/outbound_number.py:31-35` | |
| `app/database/accessor/breeze_buddy/template.py:33-37` | |

**Fix:** Extract to a shared `app/database/accessor/utils.py`.

#### 2.B.2 — ~22 Accessor functions with identical boilerplate pattern (HIGH)

Every accessor follows:
```python
async def operation(...):
    logger.info(...)
    try:
        query_text, values = query_function(...)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            return decode_function(result)
        return None
    except Exception as e:
        logger.error(...)
        return None
```

**Fix:** Generic `run_and_decode(query_fn, decode_fn, *args)` helper could eliminate ~200+ lines.

#### 2.B.3 — 3x Duplicate table name constants

`LEAD_CALL_TRACKER_TABLE` defined in 2 files. `OUTBOUND_NUMBER_TABLE` defined in 3 files.

**Fix:** Shared constants module.

#### 2.B.4 — Inconsistent decoder signatures

Half take `asyncpg.Record`, half take `List[asyncpg.Record]`. Forces callers to adapt with `result[0]` vs passing full list.

**Fix:** Standardize to single-row input, callers pass `result[0]`.

#### 2.B.5 — Duplicate `OutboundNumber(...)` construction in decoder

`decode_outbound_number` (line 20-31) and `decode_outbound_number_list` (line 42-53) both manually construct `OutboundNumber`. The list version should reuse the single-row decoder (like `blacklisted_numbers.py` correctly does).

#### 2.B.6 — 2x `TemplateMetadata` construction in accessor

`get_templates_list` (lines 251-259) and `delete_template_if_not_referenced` (lines 481-488) both manually construct `TemplateMetadata` with identical field mapping. Should be a `decode_template_metadata()` function.

#### 2.B.7 — 2x Template JSON serialization block

`create_template` (lines 108-126) and `replace_template` (lines 339-357) have identical 5-field JSON serialization blocks. Should be a helper.

#### 2.B.8 — `users.py` violates 3-layer separation

`app/database/queries/breeze_buddy/users.py` combines query + accessor + decoder in one file (has `get_db_connection()` calls and inline `UserInDB` construction). Every other entity properly separates these.

#### 2.B.9 — Duplicate analytics queries across files

| Legacy (in `queries/lead_call_tracker.py`) | Generic (in `queries/analytics.py`) |
|---|---|
| `get_lead_based_analytics_query` (hardcoded outcome names) | `get_analytics_lead_based_query` (dynamic `jsonb_object_agg`) |
| `get_all_lead_call_trackers_query` (inline filters) | `get_analytics_call_details_query` (generic filter builder) |
| `get_lead_call_trackers_count_query` | `get_analytics_count_query` |
| `get_all_outbound_numbers_with_call_count_query` (in outbound_number.py) | `get_analytics_outbound_numbers_query` |

The analytics versions are more generic/featureful. The legacy versions in `lead_call_tracker.py` and `outbound_number.py` appear to be older implementations.

#### 2.B.10 — 5x Repeated JOIN clause in `analytics.py`

Same `LEFT JOIN outbound_number` conditional logic repeated in 5 query functions. Should be extracted to `_build_join_clause(filters)`.

---

### 2.C — Agent/Services Layer Duplication

#### 2.C.1 — 3x Identical `handle_websocket` across telephony providers (HIGH)

| File | Lines |
|------|-------|
| `services/telephony/twilio/twilio.py:67-75` | |
| `services/telephony/exotel/exotel.py:50-58` | |
| `services/telephony/plivo/plivo.py:41-49` | |

Character-for-character identical (except log message). Should be in `VoiceCallProvider` base class.

#### 2.C.2 — 4x Identical transport params in `transport.py` (HIGH)

`app/ai/voice/agents/breeze_buddy/agent/transport.py:96-128` — `twilio`, `exotel`, `telnyx`, `plivo` entries are completely identical `FastAPIWebsocketParams`. Only `daily` differs. Should collapse to 2 entries.

#### 2.C.3 — 3x Failure-webhook blocks in `process_backlog_leads` (HIGH)

`app/ai/voice/agents/breeze_buddy/managers/calls.py` — lines 562-585, 611-633, 741-764. Same webhook-sending block copy-pasted. Extract `_send_failure_webhook(session, lead, reason)`.

#### 2.C.4 — 3x Exotel channel-capacity check (MEDIUM)

`managers/calls.py` — lines 188-193, 219-225, 660-666. Same capacity check. Extract `has_capacity()` helper.

#### 2.C.5 — 2x Outbound number release pattern (MEDIUM)

`managers/calls.py` — `handle_call_completion` (lines 787-797) and `handle_unanswered_calls` (lines 866-875). Same release + error log. Extract `_release_outbound_number(lead)`.

#### 2.C.6 — `_resolve_dict_templates` vs `_resolve_recursive` overlap (LOW)

`handlers/transport/http_requester.py` — `_resolve_dict_templates` (lines 472-497) is a subset of `_resolve_recursive` (lines 499-523). The recursive version handles dicts, lists, and None.

---

### 2.D — Schema/Model Duplication

#### 2.D.1 — `PreCheckHttpRequest` vs `HttpRequestConfig` near-duplicate

| File | Class | Default method |
|------|-------|---------------|
| `schemas/breeze_buddy/core.py:64-76` | `PreCheckHttpRequest` | `"GET"` |
| `template/types.py:313-328` | `HttpRequestConfig` | `HttpMethod.POST` |

Same fields, same purpose. `PreCheckHttpRequest` docstring says "Matches HttpRequestConfig structure for compatibility."

#### 2.D.2 — `TemplateMetadata` vs `TemplateModel` overlap

| File | Class |
|------|-------|
| `schemas/breeze_buddy/template.py:9-22` | `TemplateMetadata` (7 fields) |
| `template/types.py:424-442` | `TemplateModel` (superset, 12+ fields) |

`TemplateMetadata` is a subset of `TemplateModel`. Could use inheritance. Also type mismatch: `datetime` vs `Optional[Any]` for timestamps.

#### 2.D.3 — `User` / `UserInDB` / `UserInfo` field overlap

`app/schemas/breeze_buddy/auth.py` — Three models share 6+ identical fields. Should use a `UserBase` with inheritance.

#### 2.D.4 — `TokenData` (legacy) vs `AuthTokenData`

`auth.py:10-16` has `TokenData` marked "(legacy)". The replacement `AuthTokenData` (lines 167-178) has RBAC fields. `TokenData` is only used in cron endpoint and core JWT module.

---

### 2.E — Analytics Handler Duplication

#### 2.E.1 — 3x No-Answer string matching

`app/api/routers/breeze_buddy/analytics/handlers.py` — lines 273-281, 434-442, 558-569. Same `"no_answer"` / `"no answer"` / `"noanswer"` matching. Extract `count_no_answer(outcome_breakdown)`.

#### 2.E.2 — 2x Time-bucket formatting

Same file — lines 100-113 and 293-305. Identical day/week/month formatting logic. Extract `apply_time_bucket_labels(data_point, time_bucket, granularity)`.

---

## 3. Database Schema Redundancies

### 3.1 `template` (VARCHAR) column — Superseded by `template_id` (UUID FK)

**Tables affected:** `lead_call_tracker`, `call_execution_config`

- Migration 001 created `workflow` → Migration 003 renamed to `template` → Migration 008 added `template_id` as proper UUID FK.
- Migration 008 comment: *"alongside existing template name column for backward compatibility during transition period"*
- Both columns are still written on every insert. The `call_execution_config` UPDATE query still uses `WHERE "template" = $N` (string name, not UUID).

**Blocker:** `call_execution_config` update queries use string-based lookup. Must migrate to `template_id`-based lookups first.

### 3.2 `calling_provider` on `call_execution_config` — Derivable from outbound_number

- `call_execution_config.calling_provider` duplicates `outbound_number.provider`.
- The `get_all_lead_call_trackers_query` already derives provider from `outbound_number` via JOIN.
- Direct reads/writes still exist in `call_execution_config` queries.

### 3.3 `template.secrets` (JSONB) vs `credentials` table

- Migration 014 created a proper `credentials` table (typed, encrypted, merchant-scoped).
- Migration 015 added `secrets` JSONB directly on `template` (quick per-template secret store).
- `template.secrets` is actively used in all template queries. The `credentials` table may be used by a separate flow.

**Needs clarification:** Are both actively needed, or can one subsume the other?

### 3.4 Duplicate merchant/shop scoping across tables

Both `call_execution_config` and `template` have their own `merchant_id` and `shop_identifier`. Since `call_execution_config` has `template_id` FK, the scoping is redundant with the template's scoping.

### 3.5 CHECK constraints following dropped pattern

Migrations 011, 012, 013 added CHECK constraints on `execution_mode`, `call_direction`, `provider` — the same rigid pattern that was explicitly dropped for `outcome`/`workflow` in migration 004. Each new enum value requires a migration.

---

## 4. Backward-Compatibility Code (Needs Your Input)

These items are kept for backward compatibility. We need your help to determine if the conditions for removal are met.

### 4.1 `template` VARCHAR column (BC)
- **Question:** Are all consumers now using `template_id` (UUID) to identify templates? Can we migrate the `call_execution_config` UPDATE query to use `template_id` in the WHERE clause?
- **If yes:** Drop `template` VARCHAR from both `lead_call_tracker` and `call_execution_config`. Remove dual-write logic.

### 4.2 `schemas.py` backward-compat re-export shim (BC)
- **File:** `app/schemas.py`
- **Question:** Are there any external consumers importing from `app.schemas` (flat) instead of `app.schemas.breeze_buddy`? This file is marked DEPRECATED and has incomplete exports (missing 15+ symbols that `app.schemas.breeze_buddy` exports).
- **If no external consumers:** Delete `app/schemas.py` entirely.

### 4.3 `TokenData` legacy model + cron auth bypass (BC)
- **File:** `app/schemas/breeze_buddy/auth.py:10-16`
- **Question:** The cron endpoint (`cron.py`) uses the legacy `get_current_user` (returns `TokenData`) instead of `get_current_user_with_rbac` (returns `UserInfo`). This means any valid JWT can trigger lead processing regardless of role. Should cron use RBAC too?
- **If yes:** Switch cron to `get_current_user_with_rbac`, then remove `TokenData`.

### 4.4 `function_name` → `name` transform in template builder (BC)
- **File:** `app/ai/voice/agents/breeze_buddy/template/builder.py:101-106`
- **Question:** Are all templates now using `"name"` instead of `"function_name"` in their function definitions?
- **If yes:** Remove the backward-compat transform.

### 4.5 `GET /logout` session-based endpoint (BC)
- **File:** `app/api/routers/breeze_buddy/auth/__init__.py:193-211`
- **Question:** Are there still clients using session-based auth that redirect to this endpoint?
- **If no:** Remove the deprecated `GET /logout` endpoint.

### 4.6 Legacy analytics queries in `lead_call_tracker.py` (BC)
- **Question:** Is anything still consuming the legacy `get_lead_based_analytics` (which hardcodes outcome names like CONFIRM, CANCEL, ABORT) vs. the generic analytics module version?
- **If no:** Remove `get_lead_based_analytics_query` and `get_lead_based_analytics` from the lead_call_tracker files.

### 4.7 `calling_provider` on `call_execution_config` (BC)
- **Question:** Can the provider always be derived from `outbound_number.provider` via the template's `outbound_number_id`? Or are there cases where `call_execution_config.calling_provider` differs from the outbound number's provider?
- **If always derivable:** Drop `calling_provider` from `call_execution_config` and derive via JOIN.

### 4.8 `template.secrets` vs `credentials` table (BC)
- **Question:** Are both mechanisms actively used? Should we consolidate to one approach?
- **If `credentials` table can replace `template.secrets`:** Migrate secrets to `credentials` table and drop the column.

---

## Estimated Impact Summary

| Category | Est. Lines Removable | Effort |
|----------|---------------------|--------|
| Dead code removal (Section 1) | ~200 lines | Low — safe to remove now |
| RBAC consolidation (Section 2.A) | ~400+ lines | Medium — wire routers to existing centralized module |
| DB layer dedup (Section 2.B) | ~300+ lines | Medium — extract shared helpers |
| Agent/services dedup (Section 2.C) | ~150+ lines | Medium — base class + helpers |
| Schema dedup (Section 2.D) | ~80 lines | Low — inheritance + remove duplicates |
| Analytics handler dedup (Section 2.E) | ~35 lines | Low — extract helpers |
| DB schema cleanup (Section 3, pending answers) | Multiple columns/constraints | High — requires migrations |
| **Total** | **~1,200+ lines** | |
