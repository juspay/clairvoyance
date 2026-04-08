# Redis Integration

Basic async Redis client used for caching, feature flag storage, rate limiting, and distributed locking.

## Architecture

```
┌──────────────────────────────┐
│        Application Code      │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│    RedisService (singleton)  │
│  app/services/redis/client.py│
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│        RedisFactory          │
│  Cluster or single-node      │
│  auto-detection              │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│   redis-py async client      │
│   (Redis or RedisCluster)    │
└──────────────────────────────┘
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `""` | Single-node host |
| `REDIS_PORT` | `""` | Single-node port |
| `REDIS_CLUSTER_NODES` | `""` | Comma-separated `host:port` pairs for cluster mode |
| `REDIS_TTL` | `3600` | Default TTL in seconds (used by `setex` when no TTL is provided) |
| `ENABLE_REDIS_DYNAMIC_CONFIG` | `true` | When `false`, live config skips Redis and resolves from env vars only |
| `BLACKLIST_CACHE_TTL` | `300` | TTL for blacklisted-number cache entries (seconds) |

### Mode Selection

- If `REDIS_HOST` and `REDIS_PORT` are set, a **single-node** connection is created.
- If `REDIS_CLUSTER_NODES` is set (and `REDIS_HOST`/`REDIS_PORT` are empty), a **cluster** connection is created using `RedisCluster` with the parsed startup nodes.
- All connections use `decode_responses=True`.

## Components

### `RedisFactory`

Creates and caches either a `redis.asyncio.Redis` or `redis.asyncio.cluster.RedisCluster` client. Runs a `PING` on first connection to verify connectivity. Call `close()` to tear down connections.

### `RedisService`

Wraps `RedisFactory` and exposes these async methods:

| Method | Signature | Description |
|---|---|---|
| `get` | `(key: str) -> Optional[str]` | Get a value. Returns `None` on miss or error. |
| `set` | `(key: str, value: str, nx: bool = False, ex: Optional[int] = None) -> bool` | Set a value. `nx=True` sets only if key does not exist. `ex` sets expiration in seconds. |
| `setex` | `(key: str, value: str, ttl_seconds: Optional[int] = None) -> bool` | Set with TTL. Falls back to `REDIS_TTL` if `ttl_seconds` is `None`. |
| `delete` | `(key: str) -> bool` | Delete a key. |
| `exists` | `(key: str) -> bool` | Check if a key exists. |
| `ping` | `() -> bool` | Test connectivity. |
| `incr` | `(key: str) -> int` | Increment by 1 (creates key with value 1 if missing). Raises on error. |
| `expire` | `(key: str, seconds: int) -> bool` | Set TTL on an existing key. |
| `get_client` | `() -> Union[Redis, RedisCluster]` | Get the underlying client directly. |
| `close` | `() -> None` | Close connections and reset factory. |

All methods except `incr` swallow `RedisError` and return a safe default (`None`, `False`). `incr` raises on failure.

### Global Singleton

```python
from app.services.redis.client import get_redis_service, close_redis_connections

redis_service = await get_redis_service()   # lazy-initialized singleton
await close_redis_connections()              # teardown (called at app shutdown)
```

### `is_redis_configured()`

Returns `True` if either `REDIS_HOST`+`REDIS_PORT` or `REDIS_CLUSTER_NODES` contain valid values. Used to guard Redis operations in optional-Redis environments.

## Module Exports

`app/services/redis/__init__.py` exports:

- `RedisFactory`
- `RedisService`
- `get_redis_service`
- `close_redis_connections`
- `is_redis_configured`

## Health Check

**Endpoint:** `GET /health/redis` (defined in `app/api/routers/systems.py`)

Runs four checks against the live Redis connection, each with latency measurement:

1. **PING** -- verifies connectivity
2. **SET** -- writes a test key `health:check:test`
3. **GET** -- reads it back and verifies value match
4. **DELETE** -- removes the test key

Returns a JSON object with `status` (`"healthy"`, `"degraded"`, or `"unhealthy"`) and per-check details.

## Usage Patterns in the Codebase

### Dynamic / Live Config

`app/services/live_config/store.py` resolves config values via **Redis -> Environment -> Default**. When `ENABLE_REDIS_DYNAMIC_CONFIG` is `false`, the Redis step is skipped entirely.

```python
value = await get_config("SOME_FLAG", default_value="fallback", return_type=str)
```

### Distributed Locking (Background Tasks)

`app/core/background_tasks/scheduler.py` uses `SET NX EX` to acquire distributed locks so that only one instance runs a given background task:

```python
redis_service = await get_redis_service()
lock_acquired = await redis_service.set(
    key=f"background:task:{task_name}:lock",
    value="locked",
    nx=True,
    ex=task.interval_seconds,
)
```

### Blacklist Number Caching

`app/database/accessor/breeze_buddy/blacklisted_numbers.py` caches blacklist lookups in Redis using `setex` with `BLACKLIST_CACHE_TTL`.

### Rate Limiting / Counting

`app/services/langfuse/tasks/score_monitor/score.py` uses `incr` + `expire` to count events within a time window.

### Warm Transfer State

`app/ai/voice/agents/breeze_buddy/utils/warm_transfer.py` stores and retrieves ephemeral transfer state using `setex`, `get`, `delete`, and `set(nx=True, ex=...)`.

## Startup and Shutdown

In `app/main.py`, the Redis client is initialized eagerly at startup:

```python
redis_service = await get_redis_service()
await redis_service.get_client()  # Initialize the client
```

Connections are closed during app shutdown via `close_redis_connections()`.
