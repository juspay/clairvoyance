# DevCycle Feature Flags

## Overview

Redis-backed feature flag system powered by DevCycle:
- **One API call** to DevCycle CDN at startup to fetch all flags
- **Redis storage** for fast, cluster-safe flag access
- **Real-time updates** via authenticated webhooks
- **Fallback chain**: Redis → Environment variable → Default value

## Architecture

### Core Components

1. **Feature Flag Store** (`app/services/live_config/store.py`)
   - Fetches flags from DevCycle CDN and stores them in Redis as a single JSON blob
   - Key: `devcycle:flags`
   - Async Redis operations (cluster-safe)
   - Environment variable fallback when Redis is unavailable

2. **DevCycle Router** (`app/api/routers/devcycle.py`)
   - Webhook endpoint (`POST /webhooks/devcycle`) with shared secret query parameter (`?secret=...`) verification
   - On webhook, re-fetches full config from DevCycle CDN and updates Redis

3. **Utilities** (`app/services/live_config/utils.py`)
   - Variable mapping, type conversion, key normalization
   - Environment variable resolution helpers

### Data Flow

```
1. Server starts → initialize_feature_flags()
2. Fetches config from DevCycle CDN (one HTTP call)
3. Processes features/variations → extracts flag key-value pairs
4. Stores all flags as JSON in Redis (key: devcycle:flags)
5. Application reads flags via get_config() → Redis → Env → Default
6. Webhooks trigger full re-fetch from CDN → update Redis
```

## Configuration

### Environment Variables

```bash
# Required for DevCycle integration
DEVCYCLE_SERVER_KEY=your_server_key_here

# Required for webhook authentication
DEVCYCLE_WEBHOOK_SECRET=your_webhook_secret

# Optional - disable Redis-based dynamic config (falls back to env vars only)
ENABLE_REDIS_DYNAMIC_CONFIG=true
```

### Webhook Setup

1. Go to DevCycle dashboard → Webhooks
2. Add webhook URL: `https://your-app.com/webhooks/devcycle?secret=YOUR_SECRET`
3. Select events: `modifiedVariation`

## Usage

### Reading Flags

```python
from app.services.live_config.store import get_config

# Async flag access with type conversion
api_url = await get_config("API_URL", "https://default.com", str)
max_retries = await get_config("MAX_RETRIES", 3, int)
timeout = await get_config("TIMEOUT_SECONDS", 30.0, float)
enabled = await get_config("FEATURE_ENABLED", False, bool)
```

### Resolution Order

1. **Redis** (`devcycle:flags` JSON blob) — skipped if `ENABLE_REDIS_DYNAMIC_CONFIG=false`
2. **Environment variable** (normalized key lookup)
3. **Default value** (provided by caller)

## Disabling DevCycle

Remove or unset `DEVCYCLE_SERVER_KEY`:
- DevCycle API calls are skipped entirely
- System falls back to environment variables only
- No impact on application functionality

## File Structure

```
app/
├── services/
│   └── live_config/
│       ├── store.py           # Core: Redis-based flag store
│       └── utils.py           # Variable mapping, type conversion
├── api/
│   └── routers/
│       └── devcycle.py        # Webhook endpoint
└── core/
    └── config/
        └── static.py          # DEVCYCLE_SERVER_KEY, DEVCYCLE_WEBHOOK_SECRET
```
