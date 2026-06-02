# Feature Flag Architecture

## Summary

Clairvoyance now uses a Redis-backed feature flag store for dynamic runtime
configuration. The old DevCycle CDN fetch and webhook flow has been removed
from the application code. Flags are written directly to Redis through the
feature-flags API and read at runtime through `get_config()`.

The Redis key remains:

```text
devcycle:flags
```

The value is a flat JSON object:

```json
{
  "FLAG_KEY": "simple-value",
  "TARGETED_FLAG": {
    "has_targeting": true,
    "value": "default-value",
    "targets": [],
    "variation_values": {}
  }
}
```

This design keeps runtime reads simple and fast while still allowing optional
customer-level targeting and deterministic traffic distribution.

## Current Flow

```text
Admin/frontend client
    |
    | POST /agent/voice/breeze-buddy/feature-flags
    v
Feature flags API
    |
    | merge/update JSON
    v
Redis key: devcycle:flags
    |
    | get_config(...)
    v
Runtime services and voice agents
```

There is no startup fetch from DevCycle, no `/webhooks/devcycle` router, and no
`DEVCYCLE_SERVER_KEY` or `DEVCYCLE_WEBHOOK_SECRET` dependency in the staged
implementation.

## Files

The main implementation files are:

```text
app/api/routers/feature_flags/__init__.py
app/api/routers/feature_flags/handlers.py
app/api/routers/feature_flags/rbac.py
app/schemas/feature_flags.py
app/services/live_config/store.py
app/services/live_config/utils.py
app/core/config/dynamic.py
app/main.py
```

The removed DevCycle-specific pieces are:

```text
app/api/routers/devcycle.py
DEVCYCLE_SERVER_KEY
DEVCYCLE_WEBHOOK_SECRET
run.py parent-process DevCycle initialization
```

## API

The feature flag routes are mounted under the Breeze Buddy prefix:

```text
GET    /agent/voice/breeze-buddy/feature-flags
POST   /agent/voice/breeze-buddy/feature-flags
DELETE /agent/voice/breeze-buddy/feature-flags/{flag_key}
```

Authentication uses Breeze Buddy RBAC tokens. Reading requires a valid token.
Writing and deleting require an admin role.

Example update payload:

```json
{
  "flags": {
    "BB_STT_SERVICE": "soniox",
    "BREEZE_BUDDY_ENABLE_VAD": false
  }
}
```

The handler loads the existing JSON object from `devcycle:flags`, merges the
incoming keys, and writes the full object back to Redis.

## Config Resolution

Runtime code reads flags through:

```python
await get_config("FLAG_KEY", default_value, return_type)
```

Resolution order is:

```text
Redis -> environment variable -> default value
```

If `ENABLE_REDIS_DYNAMIC_CONFIG=false`, Redis is skipped:

```text
environment variable -> default value
```

Simple flags return the stored Redis value after type conversion:

```json
{
  "BB_STT_SERVICE": "deepgram"
}
```

```python
service = await get_config("BB_STT_SERVICE", "soniox", str)
```

## Targeting Flags

Targeting flags are stored as objects with `has_targeting: true`.

Example:

```json
{
  "BB_STT_SERVICE": {
    "has_targeting": true,
    "value": "soniox",
    "targets": [
      {
        "name": "Premium customers",
        "audience": {
          "filters": {
            "operator": "and",
            "filters": [
              {
                "type": "customData",
                "subType": "customer_tier",
                "comparator": "=",
                "values": ["premium"]
              }
            ]
          }
        },
        "distribution": [
          {"_variation": "deepgram_var", "percentage": 20},
          {"_variation": "soniox_var", "percentage": 80}
        ]
      }
    ],
    "variation_values": {
      "deepgram_var": "deepgram",
      "soniox_var": "soniox"
    }
  }
}
```

To evaluate this flag, the call site must pass user context:

```python
await get_config(
    "BB_STT_SERVICE",
    "soniox",
    str,
    user_id=customer_id,
    user_email=customer_email,
    custom_data={
        "customer_tier": "premium",
        "reseller_id": reseller_id,
        "merchant_id": merchant_id,
    },
)
```

If no user context is supplied, targeting cannot bucket the user and the
top-level `value` is returned.

## Audience Filters

Supported filters are implemented in `app/services/live_config/store.py`.

User filters:

```json
{
  "type": "user",
  "subType": "email",
  "comparator": "=",
  "values": ["alice@example.com"]
}
```

```json
{
  "type": "user",
  "subType": "userId",
  "comparator": "=",
  "values": ["customer-123"]
}
```

Custom data filters:

```json
{
  "type": "customData",
  "subType": "merchant_id",
  "comparator": "=",
  "values": ["kerala-paints"]
}
```

Supported comparators:

```text
=
!=
contain
!contain
```

Filter groups support:

```text
and
or
```

Targets are evaluated in order. The first matching target controls the
variation assignment.

## Traffic Distribution

For targeted flags, distribution is deterministic. The evaluator computes a
bucket from:

```text
user_email or user_id + flag_key
```

It hashes that value with SHA-256 and maps it to a bucket from `0` to `99`.
The distribution percentages then decide the variation.

For example:

```json
"distribution": [
  {"_variation": "control", "percentage": 50},
  {"_variation": "treatment", "percentage": 50}
]
```

Buckets `0-49` receive `control`; buckets `50-99` receive `treatment`.

Because assignment is deterministic, the same customer receives the same
variation for the same flag across calls, as long as the same `user_email` or
`user_id` is passed.

## Dynamic Config Wrappers

Most application code should keep using wrappers in `app/core/config/dynamic.py`.
For simple global flags, wrappers can stay context-free:

```python
async def BB_STT_SERVICE() -> str:
    return await get_config("BB_STT_SERVICE", "soniox", str)
```

For customer-level targeting, the wrapper needs optional context parameters:

```python
async def BB_STT_SERVICE(
    user_id: str | None = None,
    user_email: str | None = None,
    custom_data: dict | None = None,
) -> str:
    return await get_config(
        "BB_STT_SERVICE",
        "soniox",
        str,
        user_id=user_id,
        user_email=user_email,
        custom_data=custom_data,
    )
```

Call sites that do not need targeting can continue calling:

```python
await BB_STT_SERVICE()
```

Call sites that do need targeting must pass context explicitly.

## Testing

Static validation:

```bash
uv run black --check .
uv run isort . --profile black --check-only
uv run pyrefly check
```

Start the server:

```bash
ENABLE_REDIS_DYNAMIC_CONFIG=true uv run python run.py
```

Mint an admin token:

```bash
TOKEN=$(uv run python -c 'from datetime import timedelta; from app.api.security.breeze_buddy.rbac_token import rbac_token_manager; from app.schemas import UserRole; print(rbac_token_manager.create_access_token_with_rbac("test-admin","test-admin",UserRole.ADMIN,["*"],["*"],expires_delta=timedelta(hours=1)))')
```

Write flags through the API:

```bash
curl -s -X POST "http://127.0.0.1:8000/agent/voice/breeze-buddy/feature-flags" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "flags": {
      "BB_STT_SERVICE": "soniox",
      "TEST_AB_STT": {
        "has_targeting": true,
        "value": "soniox",
        "targets": [
          {
            "name": "Alice gets Deepgram",
            "distribution": [
              {"_variation": "deepgram_var", "percentage": 100}
            ],
            "audience": {
              "filters": {
                "operator": "and",
                "filters": [
                  {
                    "type": "user",
                    "subType": "email",
                    "comparator": "=",
                    "values": ["alice@example.com"]
                  }
                ]
              }
            }
          }
        ],
        "variation_values": {
          "deepgram_var": "deepgram"
        }
      }
    }
  }'
```

Verify runtime resolution:

```bash
uv run python -c 'import asyncio
from app.services.live_config.store import get_config

async def main():
    print("simple:", await get_config("BB_STT_SERVICE", "missing", str))
    print("alice:", await get_config("TEST_AB_STT", "missing", str, user_email="alice@example.com"))
    print("bob:", await get_config("TEST_AB_STT", "missing", str, user_email="bob@example.com"))

asyncio.run(main())'
```

Expected:

```text
simple: soniox
alice: deepgram
bob: soniox
```

## Operational Notes

Redis availability is required for dynamic flag reads. If Redis lookup fails,
`get_config()` logs a warning and falls back to environment variables and then
the supplied default.

The single-key storage model makes updates simple, but concurrent writes can
overwrite each other if two admins update flags at exactly the same time. If
that becomes a real workflow, add optimistic locking with Redis `WATCH`/`MULTI`
or move to per-flag keys.

Targeted flags only work when runtime call sites pass stable user context.
Without `user_id` or `user_email`, the evaluator returns the flag's default
`value`.
