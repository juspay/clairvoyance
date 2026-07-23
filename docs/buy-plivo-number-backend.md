# Buy Plivo Number — Backend

Provider-agnostic "search & buy phone number" capability for Breeze Buddy. An
admin or reseller searches a telephony provider's inventory (Plivo today),
purchases a number, and the system registers it in the `telephony_numbers`
table so it can be assigned to templates and used for outbound calls.

The design is provider-agnostic: a `NumberProvider` ABC + factory means
Twilio/Exotel buy support drops in without touching the API or schema layer.

---

## 1. End-to-End Flow

```
Admin/Reseller UI
  │  GET  /agent/voice/breeze-buddy/numbers/PLIVO/search?country_iso=IN&type=fixed&pattern=022
  ▼
search_provider_numbers_endpoint
  → parse_call_provider() → require_admin_or_reseller_access()
  → search_provider_numbers_handler()
      → get_number_provider(PLIVO) → PlivoNumberProvider.search_numbers()
          → asyncio.to_thread(client.numbers.search, ...)
  ◀ TelephonyNumberSearchResponse { numbers[], meta{ inr_conversion_rate } }

Caller picks a number, submits buy
  │  POST /agent/voice/breeze-buddy/numbers/PLIVO/buy
  │       { number, reseller_id?, merchant_id?, maximum_channels }
  ▼
buy_provider_number_endpoint
  → parse_call_provider() → require_admin_or_reseller_access()
  → buy_provider_number_handler()
      0. resolve_buy_scope()                 ── whose reseller_id/merchant_id this
                                                 lands under; a rejection here means
                                                 zero provider calls
      ── async with RedisLock("numbers:buy:{provider}:{number}") ──
      1. check_number_purchase_conflict()    ── duplicate pre-check (409 if an
                                                 ACTIVE row already exists, 503 if
                                                 the DB cannot be reached)
      2. get_number_provider()               ── resolve the provider adapter
      3. provider.buy_number()               ── Plivo purchase (502 if not
                                                 "fulfilled")
      4. create_telephony_number()           ── DB insert (status=AVAILABLE,
                                                 channels=0); any failure here
                                                 releases the number back to
                                                 the provider
      ── lock released ──
      LockAcquireError (lost the race for this number) → 409
  ◀ TelephonyNumberBuyResponse { provider_status, provider_api_id,
                                 telephony_number, message }
```

Mount point: `breeze_buddy.router` is mounted at `/agent/voice/breeze-buddy`
(`main.py`).

---

## 2. API Endpoints

Both endpoints live in `app/api/routers/breeze_buddy/numbers/__init__.py`.

### 2.1 Search — `GET /numbers/{provider_name}/search`
- **Handler:** `search_provider_numbers_handler`
- **Auth:** `get_current_user_with_rbac` + `require_admin_or_reseller_access` —
  admin or reseller only. Buying spends real money, so this is deliberately
  narrower than everything `resolve_buy_scope` (below) is otherwise able to
  handle; merchant/user self-service buy was never a confirmed product
  decision. Widening later is a one-line change in `rbac.py`.
- **Provider parsing:** `parse_call_provider()` upper-cases and validates
  against the `CallProvider` enum; unknown provider → **400** with the list
  of supported providers.
- **Query params** (→ `TelephonyNumberSearchParams`): `country_iso` (default
  `IN`), `type`, `pattern`, `services`, `region`, `limit` (1–20, default 20),
  `offset` (≥0).
- **Response:** `TelephonyNumberSearchResponse` → `numbers[]` + `meta`.
- **Errors:** `ValueError` from provider config → **503**; any other
  provider exception → **502**.

### 2.2 Buy — `POST /numbers/{provider_name}/buy`
- **Handler:** `buy_provider_number_handler`
- **Auth:** admin or reseller only (`require_admin_or_reseller_access`).
  `resolve_buy_scope` additionally restricts a reseller to their own umbrella
  (and merchants under it) — never another tenant's.
- **Status code:** **201 Created**
- **Body** (`TelephonyNumberBuyRequest`): `number` (required), `reseller_id`
  (optional for non-admins — derived from the caller's own scope), `merchant_id`
  (optional), `maximum_channels` (1–100, default 10).
- **Ownership resolution:** `resolve_buy_scope` — admin is trusted as given
  (with an unknown `merchant_id` still 400ing); a reseller's `reseller_id` is
  always their own umbrella, never a client-supplied value; merchant/user
  roles are handled by the same function but are not currently reachable
  (see auth above).
- **Response** (`TelephonyNumberBuyResponse`): `provider_status`,
  `provider_api_id`, `telephony_number` (the created record, JSON-dumped),
  `message`.

### 2.3 Route ordering (important)
The specific routes are registered **before** the `/numbers/{number_id}`
catch-all so the GET/POST paths are not swallowed by the id param route:
```
POST /numbers                        legacy/manual create (admin-only)
GET  /numbers                        list
GET  /numbers/{provider_name}/search ← specific, registered first
POST /numbers/{provider_name}/buy    ← specific, registered first
GET  /numbers/{number_id}            catch-all
PATCH/DELETE /numbers/{number_id}
```

---

## 3. Provider Abstraction Layer

Package: `app/services/telephony/numbers/`

### 3.1 `NumberProvider` ABC — `provider.py`
Abstract async interface:
- `search_numbers(params: TelephonyNumberSearchParams) -> TelephonyNumberSearchResponse`
- `buy_number(request: TelephonyNumberBuyRequest) -> ProviderBuyResult`
- `unrent_number(number: str) -> bool` (rollback release; must not raise,
  returns a success bool)

### 3.2 Factory — `factory.py`
`get_number_provider(provider_name: CallProvider) -> NumberProvider`. Returns
`PlivoNumberProvider()` for `CallProvider.PLIVO`; raises `ValueError` for
anything else. Twilio/Exotel slot in here.

### 3.3 Plivo implementation — `providers/plivo.py`
- Constructor validates `PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN`; missing →
  `ValueError` (surfaces as **503** at the API).
- The Plivo SDK is **synchronous** — every call is wrapped in
  `asyncio.to_thread()` so the event loop is never blocked.
- `get_response_value()` helper reads both dict-like and object-like SDK
  responses uniformly.
- **search:** maps Plivo `objects[]` → `AvailableTelephonyNumber[]`, reads
  `meta` for pagination; `ResourceNotFoundError` → empty result set (not an
  error). Stamps `inr_conversion_rate` (from `dynamic.py`, see §6) on the meta.
- **buy:** calls `client.numbers.buy`, normalizes to `ProviderBuyResult`.
  Handler treats `status != "fulfilled"` as a failure. Verified live: Plivo
  itself rejects a buy call for a number that's already rented (returns a
  "not found" style error) — this is a second, independent backstop on top
  of the RedisLock, not something the buy flow depends on for correctness.
- **unrent:** `client.numbers.delete`; catches all exceptions and returns
  `False` (rollback is best-effort, never raises).

---

## 4. Schemas

File: `app/schemas/breeze_buddy/telephony_numbers.py`. Names are
**provider-agnostic** so future providers reuse them.

| Model | Purpose | Key fields |
|-------|---------|-----------|
| `TelephonyNumberSearchParams` | search query | `country_iso`, `type`, `pattern`, `services`, `region`, `limit`, `offset` |
| `AvailableTelephonyNumber` | one search result | `number`, rates, `voice_enabled`, `sms_enabled`, `region`, `type`, `resource_uri`, … |
| `TelephonyNumberSearchMeta` | pagination | `limit`, `offset`, `total_count`, `inr_conversion_rate` |
| `TelephonyNumberSearchResponse` | search response | `numbers[]`, `meta` |
| `TelephonyNumberBuyRequest` | buy body | `number`, `reseller_id?`, `merchant_id?`, `maximum_channels` |
| `ProviderBuyResult` | normalized provider response | `status`, `api_id`, `message`, `numbers[]`, `is_fulfilled` |
| `TelephonyNumberBuyResponse` | buy response | `provider_status`, `provider_api_id`, `telephony_number` (Dict), `message` |

---

## 5. Database Layer

### 5.1 Accessor — `app/database/accessor/breeze_buddy/telephony_number.py`
- **`create_telephony_number(...)`** — inserts the row and decodes to
  `TelephonyNumber`. Re-raises `UniqueViolationError` specifically (in case a
  future constraint or an id/UUID collision ever produces one); swallows
  every other exception to `None`, unchanged, since manual provisioning
  shares this accessor and expects that contract.
- **`check_number_purchase_conflict(number)`** — the buy flow's (and manual
  provisioning's) duplicate pre-check. Returns `None` if the number is free
  to register (no row, or the existing row is `DISABLED`), otherwise the
  status of the row blocking it. *Propagates* DB errors rather than
  reporting them as "not found", so a database outage returns 503 instead of
  being mistaken for "this number is free to buy".
- **`get_telephony_number_by_number(number)`** — the original **lenient**
  lookup, which swallows errors and returns `None`. Used by inbound call
  routing (`agent/inbound.py`, `telephony/answer/handlers.py`), which needs
  fail-soft behavior and must resolve a number regardless of status. Not
  used by the buy flow.

### 5.2 Migration — `042_add_telephony_number_lookup_index.sql`
Adds a single plain index, `idx_telephony_numbers_number ON
telephony_numbers(number)`. `number` has had zero index coverage since
migration 009 dropped the table's original `UNIQUE(number)` constraint (and
its backing index) — every lookup by number, including this feature's own
pre-check and the pre-existing inbound-call-routing lookup, was a sequential
scan before this.

**No unique constraint.** An earlier version of this migration also added a
partial unique index (`WHERE status <> 'DISABLED'`) to enforce "at most one
active row per number" at the DB level. It was removed after review:
- The buy flow's actual concurrency risk (two requests racing to buy the
  same not-yet-rented number) is handled by the RedisLock (§5.3) — by the
  time either request reaches Plivo, the other has already been rejected at
  the lock, so they never race Plivo concurrently.
- Plivo itself rejects buying an already-rented number (verified live),
  covering the sequential re-buy case independently of anything on our side.
- The one path the constraint uniquely covered — manual provisioning
  (`POST /numbers`) racing the buy flow, since it shares the same table but
  takes no lock — doesn't call any provider and isn't money-at-risk in the
  same way; `check_number_purchase_conflict` (§5.3) already gates it.

### 5.3 Concurrency model
- **RedisLock** (`numbers:buy:{provider}:{number}`, 30s TTL) wraps the
  pre-check through registration in `buy_provider_number_handler`. This is
  the actual mutual-exclusion primitive: a concurrent request for the same
  number gets `LockAcquireError` → 409 *before* it ever calls the provider.
- **`check_number_purchase_conflict`** — application-level pre-check, used
  by both the buy flow and manual provisioning (`create_number_handler`).
  Its job is cost/latency avoidance (reject a known duplicate before
  spending money or making a real provider call) and closing the one gap
  the lock doesn't cover — manual provisioning writes directly to the same
  table without ever taking the lock.
- **Plivo's own rejection** of buying an already-rented number is a third,
  independent layer, not something the design depends on for correctness.

---

## 6. Configuration

- `app/core/config/static.py`: `PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN` —
  credentials (already existed for telephony).
- `app/core/config/dynamic.py`: `PLIVO_INR_CONVERSION_RATE()` (async,
  Redis-backed, default `80.0`) — used to annotate search results for INR
  display. Moved here from static config because a conversion rate drifts
  with the market and shouldn't need a pod restart to update.

---

## 7. Auth, Error Handling, Safety

### RBAC — who can buy, and for whom
Two separate questions, answered by two separate functions in
`numbers/rbac.py`:

1. **May you buy/search at all?** `require_admin_or_reseller_access` — admin
   or reseller only, checked in the endpoint before the handler runs.
2. **Who may you buy *for*?** `resolve_buy_scope` — resolves and validates
   the requested `reseller_id`/`merchant_id` against the caller's own scope.
   Admin is trusted as given (unknown `merchant_id` still 400s). A reseller's
   `reseller_id` is always their own umbrella; a `merchant_id` they send must
   be one of the merchants currently under it. Runs *before* anything is
   purchased — a rejection here means zero provider calls.

Reads are scoped too: `filter_numbers_by_rbac` restricts `GET /numbers` to
the caller's tenant, and `GET /numbers/{id}` returns **404** rather than
confirming another tenant's number exists.

- **Provider validation:** `parse_call_provider` rejects unknown providers
  with 400.
- **Status-code contract:** 400 bad params/provider/ownership · 403
  non-admin/reseller or cross-tenant · 409 duplicate or lock contention · 502
  provider/purchase failure · 503 provider misconfigured/DB unreachable · 500
  registration failure after a successful purchase.
- **Rollback:** any failure to register after a successful purchase routes
  through `_release_number()`, which calls `unrent_number` and, if that also
  fails, logs `critical` with the provider `api_id` for manual recovery.
- **SQL safety:** all queries use asyncpg `$N` placeholders — no
  interpolation.

---

## 8. Tests

- `tests/test_buy_provider_number.py` — the buy flow: happy path, duplicate
  pre-check, DB error during pre-check (must not buy), provider raise,
  provider not-fulfilled, DB insert failure → release, insert failure *and*
  release failure → orphan surfaced, lock contention → 409 with zero
  provider calls, scope rejection → zero provider calls, merchant_id
  resolution.
- `tests/test_create_number_handler.py` — manual provisioning's own duplicate
  check (`check_number_purchase_conflict`), now that it has no DB constraint
  to lean on.
- `tests/test_create_telephony_number_accessor.py` — `create_telephony_number`'s
  exception contract, `check_number_purchase_conflict`'s DISABLED-exclusion
  logic and DB-error propagation, and the by-number lookup query shape.
- `tests/test_numbers_rbac.py` — `resolve_buy_scope` for every role, and
  `require_admin_or_reseller_access`.

All DB-free — accessors and the provider are patched, so nothing touches
Postgres or Plivo. The RedisLock is also patched to a succeeding stand-in by
default, with a dedicated contended-lock test for the 409 path; the actual
lock behavior was additionally verified live against a running instance with
real Redis (two concurrent requests for the same number: one proceeds, the
other gets 409 with zero provider calls).

---

## 9. Verification

```bash
uv run pytest tests -q
uv run black . --check
uv run isort . --profile black --check
uv run pyrefly check
```

> **pyrefly + git worktrees:** pyrefly's default `project-excludes` contains
> `**/.[!/.]*/**/*`, which matches any path under a dot-directory. A worktree
> checked out under a dot-directory (e.g. `.worktrees/`) is therefore
> silently excluded and pyrefly checks *nothing* (exit 0/1, no files). CI
> checks out normally, so it is unaffected — this only matters when running
> locally from such a worktree.
