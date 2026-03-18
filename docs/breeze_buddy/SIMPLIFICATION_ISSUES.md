# Breeze Buddy Code Simplification — Sub-Issues

Parent tracking: Simplify bloated code in Breeze Buddy without changing functionality.

Each issue below is scoped to be independently mergeable with low risk.

---

## Issue 1: Extract duplicate audio caching logic in `ivr.py`

**Priority:** P0 | **Estimated savings:** ~120 lines | **Risk:** Low

**Problem:**
Three functions in `app/ai/voice/agents/breeze_buddy/agent/ivr.py` follow the identical pattern:
- `prepare_ivr_menu_audio()`
- `prepare_goodbye_audio()`
- `prepare_block_audio()`

Each does: Redis cache lookup (MD5 key from text+voice) → TTS generation on miss → base64 encode → provider-specific audio conversion → return bytes. The logic is copy-pasted across all three.

**Task:**
1. Extract a generic `_prepare_cached_audio(cache_key_prefix, text, voice_name, provider, generate_fn)` helper
2. Refactor all three functions to call the shared helper
3. Verify no behavioral changes

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/agent/ivr.py`

---

## Issue 2: Extract duplicate release/cleanup pattern in `managers/calls.py`

**Priority:** P0 | **Estimated savings:** ~100 lines | **Risk:** Low

**Problem:**
In `app/ai/voice/agents/breeze_buddy/managers/calls.py` (989 lines), `_release_number(...)` appears 7 times and `release_lock_on_lead_by_id(...)` appears 16 times. Only 2 call sites use both together; the remaining sites are unlock-only paths where no number was acquired (early bailouts for config errors, blacklisting, outside hours, etc.).

**Task:**
1. Create `_cleanup_failed_lead(lead, number, reason, ...)` helper that encapsulates the release+unlock+update pattern for the sites that use both calls
2. Create `_release_lead_lock(lead, reason, ...)` helper for the unlock-only paths
3. Replace the ~23 sites with the appropriate helper call
4. Run existing tests to verify no behavioral change

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/managers/calls.py`

---

## Issue 3: Extract duplicate webhook failure handling in `managers/calls.py`

**Priority:** P0 | **Estimated savings:** ~50 lines | **Risk:** Low

**Problem:**
The webhook failure notification pattern (get URL → build payload → send with retry → log error) is repeated 3 times in `managers/calls.py` at different failure points.

**Task:**
1. Extract `_send_failure_webhook(config, lead, reason, ...)` helper
2. Replace 3 duplicate blocks with calls to the helper
3. Verify webhook payloads remain identical

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/managers/calls.py`

---

## Issue 4: Extract duplicate outbound number selection in `managers/calls.py`

**Priority:** P0 | **Estimated savings:** ~40 lines | **Risk:** Low

**Problem:**
Near-identical logic for finding available outbound numbers (filter by reseller/merchant/provider, check Exotel channel capacity) appears twice — once for initial call and once for retry with alternate provider.

The Exotel channel capacity check `num.channels is not None and num.maximum_channels is not None and num.channels < num.maximum_channels` is repeated 3 times.

**Task:**
1. Extract `_find_available_number(numbers, provider, status_filter)` helper
2. Extract `_has_available_capacity(number) -> bool` for the Exotel check
3. Replace all duplicates

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/managers/calls.py`

---

## Issue 5: Deduplicate provider string normalization

**Priority:** P1 | **Estimated savings:** ~15 lines | **Risk:** Low

**Problem:**
The pattern `provider.lower() if hasattr(provider, "lower") else str(provider).lower()` is repeated in 4+ files:
- `agent/ivr.py` (3 times)
- `agent/utils.py`
- `services/call_redirect.py`
- `handlers/internal/warm_transfer.py`

**Task:**
1. Add `normalize_provider_name(provider)` to `app/ai/voice/agents/breeze_buddy/utils/common.py`
2. Replace all occurrences across the 4 files

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/utils/common.py`
- `app/ai/voice/agents/breeze_buddy/agent/ivr.py`
- `app/ai/voice/agents/breeze_buddy/agent/utils.py`
- `app/ai/voice/agents/breeze_buddy/services/call_redirect.py`
- `app/ai/voice/agents/breeze_buddy/handlers/internal/warm_transfer.py`

---

## Issue 6: Deduplicate audio message building for providers

**Priority:** P1 | **Estimated savings:** ~30 lines | **Risk:** Low

**Problem:**
Provider-specific audio websocket message formatting (Plivo uses `playAudio`/`streamId`, Twilio/Exotel use `media`/`streamSid`) is duplicated 3+ times across:
- `agent/ivr.py` (multiple occurrences)
- `agent/utils.py`

**Task:**
1. Extract `_build_media_message(provider, stream_sid, payload) -> dict` utility
2. Replace all duplicate formatting blocks

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/agent/ivr.py`
- `app/ai/voice/agents/breeze_buddy/agent/utils.py`

---

## Issue 7: Move `download_call_recording` to telephony base provider

**Priority:** P1 | **Estimated savings:** ~90 lines | **Risk:** Low

**Problem:**
`download_call_recording()` is implemented nearly identically (~45 lines each) in all three telephony providers:
- `services/telephony/twilio/recording.py`
- `services/telephony/exotel/recording.py`
- `services/telephony/plivo/recording.py`

The providers only download audio to memory and return `BytesIO`. The GCS upload happens separately in `managers/calls.py` (`update_call_recording` / `process_call_recording`). The shared logic is: construct recording URL → download into memory → return `BytesIO`, with only the URL construction differing per provider.

**Task:**
1. Add shared `download_call_recording()` to `services/telephony/base_provider.py` that encapsulates the download-to-memory logic, with a provider-specific `_get_recording_url()` hook
2. Remove duplicate implementations from each provider's `recording.py`
3. Leave the existing GCS upload flow in `process_call_recording` unchanged
4. Verify recording download still works for each provider

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/services/telephony/base_provider.py`
- `app/ai/voice/agents/breeze_buddy/services/telephony/twilio/recording.py`
- `app/ai/voice/agents/breeze_buddy/services/telephony/exotel/recording.py`
- `app/ai/voice/agents/breeze_buddy/services/telephony/plivo/recording.py`

---

## Issue 8: Move `handle_websocket` to telephony base provider

**Priority:** P1 | **Estimated savings:** ~15 lines | **Risk:** Low

**Problem:**
`handle_websocket()` is overridden in each provider's main file (`twilio.py`, `exotel.py`, `plivo.py`) with identical logic. The base `VoiceCallProvider` already declares it as `@abstractmethod`. All three concrete implementations are identical — they delegate to `telephony_bot()` with the same arguments, differing only in the log message string.

**Task:**
1. Add a shared concrete implementation in `base_provider.py` (replacing the `@abstractmethod` decorator) that delegates to `telephony_bot()`
2. Remove the identical overrides from individual providers (or have them call `super()` if provider-specific logging is desired)
3. Verify WebSocket handling still works for each provider

**Files to modify:**
- `app/ai/voice/agents/breeze_buddy/services/telephony/base_provider.py`
- `app/ai/voice/agents/breeze_buddy/services/telephony/twilio/twilio.py`
- `app/ai/voice/agents/breeze_buddy/services/telephony/exotel/exotel.py`
- `app/ai/voice/agents/breeze_buddy/services/telephony/plivo/plivo.py`

---

## Issue 9: Extract shared DB accessor boilerplate into helpers

**Priority:** P1 | **Estimated savings:** ~300 lines | **Risk:** Low

**Problem:**
Every accessor function across all 9 accessor files follows the same pattern:
```python
async def get_X(pool, id):
    try:
        query = build_query(...)
        result = await pool.fetchrow(query, ...)
        if not result:
            return None
        return decode_X(result)
    except Exception as e:
        logger.error(f"Error getting X: {e}")
        return None
```

This try/except/log/decode boilerplate accounts for ~40-50% of each accessor file.

**Task:**
1. Create a shared helper in `app/database/accessor/breeze_buddy/common.py`:
   - `execute_and_decode(pool, query, params, decoder_fn)` — handles try/except/log/decode
   - `execute_and_decode_list(pool, query, params, decoder_fn)` — for list queries
2. Refactor all 9 accessor files to use the shared helpers

**Files to modify:**
- `app/database/accessor/breeze_buddy/common.py` (new)
- All 9 files in `app/database/accessor/breeze_buddy/`

---

## Issue 10: Consolidate `get_row_count()` duplication across accessors

**Priority:** P1 | **Estimated savings:** ~12 lines | **Risk:** Low

**Problem:**
`get_row_count()` is defined identically in 4 accessor files:
- `lead_call_tracker.py`
- `outbound_number.py`
- `call_execution_config.py`
- `template.py`

**Task:**
1. Move to the shared `common.py` helper (from Issue 9, or standalone if Issue 9 isn't done yet)
2. Import from the shared location in all 4 files

**Files to modify:**
- `app/database/accessor/breeze_buddy/common.py`
- 4 accessor files listed above

---

## Issue 11: Extract shared WHERE clause building and pagination in queries

**Priority:** P2 | **Estimated savings:** ~60 lines | **Risk:** Low

**Problem:**
Multiple query files build WHERE clauses, pagination (`LIMIT/OFFSET`), and sort validation using identical patterns:
- `queries/users.py`
- `queries/merchants.py`
- `queries/call_execution_config.py`

Sort field/order validation is also duplicated across these files.

**Task:**
1. Create `app/database/queries/breeze_buddy/common.py` with:
   - `build_where_clause(filters: dict) -> tuple[str, list]`
   - `build_pagination(page, limit) -> str`
   - `validate_sort_params(field, order, allowed_fields) -> tuple[str, str]`
2. Refactor query files to use shared helpers

**Files to modify:**
- `app/database/queries/breeze_buddy/common.py` (new)
- `app/database/queries/breeze_buddy/users.py`
- `app/database/queries/breeze_buddy/merchants.py`
- `app/database/queries/breeze_buddy/call_execution_config.py`

---

## Issue 12: Add error-handling decorator for API route handlers

**Priority:** P1 | **Estimated savings:** ~250 lines | **Risk:** Low

**Problem:**
83+ API handler functions use the same try/except pattern:
```python
async def handler(...):
    try:
        # actual logic
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in handler: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
```

**Task:**
1. Create a decorator `@handle_api_errors` in `app/api/routers/breeze_buddy/common.py`
2. Apply to all handler functions across the 13+ router modules
3. Keep the existing `HTTPException` re-raise behavior

**Files to modify:**
- `app/api/routers/breeze_buddy/common.py` (new)
- All handler files in `app/api/routers/breeze_buddy/`

---

## Issue 13: Extract `ensure_exists` and `resolve_scope` API helpers

**Priority:** P2 | **Estimated savings:** ~60 lines | **Risk:** Low

**Problem:**
- Resource existence check `if not resource: raise HTTPException(404, ...)` is repeated 15+ times
- Merchant/reseller scope resolution logic is repeated 8+ times across handler modules

**Task:**
1. Add to `app/api/routers/breeze_buddy/common.py`:
   - `ensure_exists(resource, resource_name) -> resource` (raises 404 if None)
   - `resolve_merchant_scope(user) -> merchant_id` (shared scope resolution)
2. Replace all duplicate patterns

**Files to modify:**
- `app/api/routers/breeze_buddy/common.py`
- Handler files in `merchants/`, `templates/`, `credentials/`, `numbers/`, `configurations/`, `users/`, `blacklist/`

---

## Issue 14: Deduplicate schema models across `auth.py` and `users.py`

**Priority:** P2 | **Estimated savings:** ~25 lines | **Risk:** Low

**Problem:**
- `UserCreate` and `UserUpdate` models are duplicated between `schemas/breeze_buddy/auth.py` and `schemas/breeze_buddy/users.py`
- `LoginRequest` is duplicated between `types/models.py` and `schemas/breeze_buddy/auth.py`
- Deprecated models (`LoginResponse`, `TokenData`) are still exported from `auth.py`

**Task:**
1. Keep canonical models in `users.py`, import in `auth.py` (or remove from `auth.py`)
2. Remove deprecated models if unused, or mark clearly
3. Remove duplicate `LoginRequest` from `types/models.py`
4. Update all imports

**Files to modify:**
- `app/schemas/breeze_buddy/auth.py`
- `app/schemas/breeze_buddy/users.py`
- `app/ai/voice/agents/breeze_buddy/types/models.py`

---

## Issue 15: Create generic `PaginatedListResponse[T]` schema

**Priority:** P2 | **Estimated savings:** ~20 lines | **Risk:** Low

**Problem:**
`UserListResponse` and `MerchantListResponse` share identical pagination fields (`total`, `page`, `limit`, `total_pages`). However, `TemplateListResponse` has a different shape — it only contains `templates` and `total` fields, with no pagination support.

**Task:**
1. Create generic `PaginatedListResponse[T]` in `schemas/breeze_buddy/core.py` using Pydantic generics
2. Replace `UserListResponse` and `MerchantListResponse` with the generic version (preserving existing field names `users`/`merchants` via aliases or subclassing)
3. Leave `TemplateListResponse` unchanged — it intentionally uses a simpler non-paginated shape

**Files to modify:**
- `app/schemas/breeze_buddy/core.py`
- `app/schemas/breeze_buddy/users.py`
- `app/schemas/breeze_buddy/merchants.py`

---

## Issue 16: Consolidate authorization helpers in security module

**Priority:** P2 | **Estimated savings:** ~100 lines | **Risk:** Low

**Problem:**
In `app/api/security/breeze_buddy/authorization.py`:
- `validate_shop_access()` and `validate_merchant_access()` are 95% identical
- `get_accessible_merchants()` and `get_accessible_shops()` are identical except for variable names
- Both pairs could be parameterized into a single function

**Task:**
1. Create `_validate_resource_access(resource_type, resource_id, user)` parameterized helper
2. Create `_get_accessible_resources(resource_type, user)` parameterized helper
3. Keep existing function signatures as thin wrappers for backward compatibility

**Files to modify:**
- `app/api/security/breeze_buddy/authorization.py`

---

## Issue 17: Consolidate `CreateCallExecutionConfigRequest` / `UpdateCallExecutionConfigRequest` validators

**Priority:** P3 | **Estimated savings:** ~20 lines | **Risk:** Medium

**Problem:**
In `schemas/breeze_buddy/core.py`, `CreateCallExecutionConfigRequest` and `UpdateCallExecutionConfigRequest` both define `validate_inbound_policy_consistency` with the same three checks (business hours, REDIRECT action, rate limit). However, the validators are **not identical** — they use different comparison semantics appropriate to each model:

| Check | Create | Update |
|-------|--------|--------|
| Business hours | `bool(start) != bool(end)` | `(start is None) != (end is None)` |
| REDIRECT requires number | `not self.inbound_redirect_number` (rejects `None` and `""`) | `self.inbound_redirect_number is None` (rejects only `None`) |
| Rate limit requires max_calls | `self.rate_limit_enabled` (truthiness) | `self.rate_limit_enabled is True` (exact match) |

These differences are intentional: Create fields have concrete defaults, while Update fields are all `Optional[...] = None` for partial updates.

**Task:**
1. Extract a shared `_validate_inbound_policy(start, end, block_action, redirect_number, rate_enabled, rate_max, *, partial_update: bool)` helper function (not a mixin — the parameterization is clearer as a function)
2. The `partial_update` flag controls whether to use `is None` checks (Update) or truthiness checks (Create)
3. Both models call the helper from their existing `@model_validator`, preserving validator names and error messages exactly
4. **Do not change validation behavior** — the helper must reproduce the current semantics for each model

**Files to modify:**
- `app/schemas/breeze_buddy/core.py`

---

## Issue 18: Decide REDIRECT policy when `inbound_redirect_number` is `None` on Update

**Priority:** P2 | **Estimated savings:** 0 lines (behavior fix) | **Risk:** Medium

**Problem:**
Both `CreateCallExecutionConfigRequest` and `UpdateCallExecutionConfigRequest` reject `inbound_block_action=REDIRECT` when `inbound_redirect_number` is `None`. However, the runtime code in the inbound call handler falls back to the template's `transfer_number` when no explicit redirect number is configured. This means:

- A user cannot set `inbound_block_action=REDIRECT` via the Update API without also supplying `inbound_redirect_number` in the same request, even if the template already has a `transfer_number` configured.
- The Update validator blocks a valid partial-update scenario: setting the action to REDIRECT and relying on the existing template fallback.

**Task:**
1. Investigate the runtime inbound call path to confirm the `transfer_number` fallback exists
2. If confirmed, relax the Update validator to allow `inbound_block_action=REDIRECT` without `inbound_redirect_number` (since it's a partial update — the number may already be set or the template provides a fallback)
3. Keep the Create validator strict (new configs should be explicit)
4. Add a comment documenting the fallback chain: explicit `inbound_redirect_number` → template `transfer_number`

**Files to modify:**
- `app/schemas/breeze_buddy/core.py`
- Possibly `app/ai/voice/agents/breeze_buddy/services/call_redirect.py` (to verify fallback)
