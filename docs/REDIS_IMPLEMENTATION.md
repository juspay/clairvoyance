# Redis Cluster Implementation for Clairvoyance

This document describes the Redis cluster implementation for the Clairvoyance voice agent platform, providing high-performance caching with automatic refresh capabilities.

## Overview

The Redis implementation provides:
- **Redis Cluster Support**: Horizontal scaling with sharding
- **Simple API**: Direct get/set operations with TTL management
- **Auto-Refresh**: Background worker that refreshes keys before expiration
- **Namespace Isolation**: Environment and service-based key organization
- **Health Monitoring**: Built-in health checks and observability
- **Graceful Degradation**: Fallback handling when Redis is unavailable

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Voice Agent   │    │   Analytics     │    │  Feature Flags  │
│                 │    │   Service       │    │   Service       │
└─────────┬───────┘    └─────────┬───────┘    └─────────┬───────┘
          │                      │                      │
          └──────────────────────┼──────────────────────┘
                                 │
                    ┌─────────────▼─────────────┐
                    │     Redis Service        │
                    │  (app/services/redis/)   │
                    └─────────────┬─────────────┘
                                 │
                    ┌─────────────▼─────────────┐
                    │   Redis Cluster/Single   │
                    │    (Managed/K8s/Local)   │
                    └───────────────────────────┘
```

## Key Components

### 1. Redis Configuration (`app/config/redis.py`)
- Connection factory for cluster and single-node Redis
- Environment-based configuration
- Automatic fallback from cluster to single node

### 2. Redis Service (`app/services/redis/client.py`)
- Simple get/set/delete operations
- JSON serialization support
- Namespace management
- TTL handling

### 3. Refresh Worker (`app/services/redis/refresh_worker.py`)
- Background worker for automatic key refresh
- Distributed locking to prevent stampedes
- Configurable refresh intervals and TTLs
- Callback-based value fetching

## Usage Examples

### Basic Operations

```python
from app.services.redis import redis_get, redis_set, redis_get_json, redis_set_json

# String operations
await redis_set("user_session", "session_data", ttl=300, namespace="sessions")
session_data = await redis_get("user_session", namespace="sessions")

# JSON operations
user_data = {"id": 123, "name": "John", "preferences": {"theme": "dark"}}
await redis_set_json("user_123", user_data, ttl=600, namespace="users")
retrieved_user = await redis_get_json("user_123", namespace="users")
```

### Auto-Refresh Pattern

```python
from app.services.redis import register_refresh_key, start_refresh_worker

# Define callback to fetch fresh data
async def fetch_feature_flag(flag_name):
    # Call external API (DevCycle, etc.)
    response = await api_client.get_flag(flag_name)
    return response.value

# Register for auto-refresh
await register_refresh_key(
    key="VAD_CONFIDENCE",
    namespace="feature_flags", 
    refresh_interval=30,  # Check every 30 seconds
    ttl=60,              # Cache for 60 seconds
    refresh_callback=lambda: fetch_feature_flag("VAD_CONFIDENCE"),
    enabled=True
)

# Start the refresh worker (usually done at app startup)
await start_refresh_worker()

# Your app code just does fast Redis lookups
confidence = await redis_get("VAD_CONFIDENCE", namespace="feature_flags")
```

## Configuration

### Environment Variables

```bash
# Development (single Redis instance)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# Production (Redis cluster)
REDIS_CLUSTER_STARTUP_NODES=node1:6379,node2:6379,node3:6379
REDIS_USE_TLS=true
REDIS_PASSWORD=your_secure_password

# Connection settings
REDIS_POOL_SIZE=10
REDIS_MAX_CONNECTIONS=50
REDIS_SOCKET_TIMEOUT=5.0

# Cache settings
CACHE_DEFAULT_TTL=300
CACHE_HIGH_FREQ_TTL=30
CACHE_NAMESPACE_PREFIX=clairvoyance

# Refresh worker
ENABLE_CACHE_REFRESH=true
CACHE_REFRESH_INTERVAL=30
CACHE_REFRESH_LOCK_TTL=25
```

### Key Naming Convention

Keys follow the pattern: `{prefix}:{environment}:{namespace}:{key}`

Examples:
- `clairvoyance:production:feature_flags:VAD_CONFIDENCE`
- `clairvoyance:production:sessions:user_12345`
- `clairvoyance:production:analytics:payment_stats_shop123`

## Deployment Options

### 1. Managed Redis (Recommended for Production)

**AWS ElastiCache (Cluster Mode)**
```yaml
# terraform/elasticache.tf
resource "aws_elasticache_replication_group" "clairvoyance" {
  replication_group_id       = "clairvoyance-redis"
  description                = "Redis cluster for Clairvoyance"
  
  node_type                  = "cache.r6g.large"
  port                       = 6379
  parameter_group_name       = "default.redis7.cluster.on"
  
  num_cache_clusters         = 3
  automatic_failover_enabled = true
  multi_az_enabled          = true
  
  subnet_group_name = aws_elasticache_subnet_group.clairvoyance.name
  security_group_ids = [aws_security_group.redis.id]
  
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = var.redis_auth_token
}
```

**Environment Variables for Managed Redis:**
```bash
REDIS_CLUSTER_STARTUP_NODES=cluster-endpoint:6379
REDIS_USE_TLS=true
REDIS_PASSWORD=your_auth_token
```

### 2. Kubernetes Deployment

**Using Redis Operator:**
```yaml
# k8s/redis-cluster.yaml
apiVersion: redis.redis.opstreelabs.in/v1beta1
kind: RedisCluster
metadata:
  name: clairvoyance-redis
spec:
  clusterSize: 3
  redisExporter:
    enabled: true
  storage:
    volumeClaimTemplate:
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 10Gi
  resources:
    requests:
      memory: "1Gi"
      cpu: "500m"
    limits:
      memory: "2Gi"
      cpu: "1000m"
```

**Using Bitnami Helm Chart:**
```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm install clairvoyance-redis bitnami/redis-cluster \
  --set cluster.nodes=6 \
  --set cluster.replicas=1 \
  --set auth.enabled=true \
  --set auth.password=your_secure_password \
  --set persistence.enabled=true \
  --set persistence.size=10Gi
```

### 3. Development Setup

**Docker Compose:**
```yaml
# docker-compose.yml
version: '3.8'
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data

volumes:
  redis_data:
```

**Local Redis:**
```bash
# macOS
brew install redis
redis-server

# Ubuntu
sudo apt install redis-server
sudo systemctl start redis-server
```

## Integration with Voice Agents

### Session Caching

```python
# Cache voice agent session data
session_data = {
    "user_id": user_id,
    "shop_id": shop_id,
    "conversation_context": context,
    "preferences": user_preferences
}

await redis_set_json(
    f"session_{session_id}", 
    session_data, 
    ttl=1800,  # 30 minutes
    namespace="voice_sessions"
)
```

### Analytics Caching

```python
# Cache expensive analytics queries
async def get_payment_analytics(shop_id, date_range):
    cache_key = f"payment_analytics_{shop_id}_{date_range}"
    
    # Try cache first
    cached = await redis_get_json(cache_key, namespace="analytics")
    if cached:
        return cached
    
    # Cache miss - fetch from API
    data = await juspay_api.get_payment_analytics(shop_id, date_range)
    
    # Cache for 5 minutes
    await redis_set_json(cache_key, data, ttl=300, namespace="analytics")
    return data
```

### Feature Flag Integration

```python
# Register feature flags for auto-refresh at startup
async def setup_feature_flags():
    flags = [
        ("VAD_CONFIDENCE", 30, 60),
        ("ENABLE_CHARTS", 60, 120), 
        ("MAX_SESSION_LIMIT", 300, 600)
    ]
    
    for flag_name, refresh_interval, ttl in flags:
        await register_refresh_key(
            key=flag_name,
            namespace="feature_flags",
            refresh_interval=refresh_interval,
            ttl=ttl,
            refresh_callback=lambda: fetch_from_devcycle(flag_name)
        )

# Fast flag lookups in voice agent
async def get_vad_confidence():
    confidence = await redis_get("VAD_CONFIDENCE", namespace="feature_flags")
    return float(confidence) if confidence else 0.85
```

## Monitoring and Observability

### Health Checks

The application provides Redis health check endpoints:

```bash
# Check Redis connectivity
curl http://localhost:8000/health/redis

# Response
{
  "status": "healthy",
  "redis": {
    "status": "healthy",
    "latency_ms": 1.23,
    "connected_clients": 5,
    "used_memory_human": "2.1M",
    "redis_version": "7.0.5",
    "cluster_enabled": true
  },
  "refresh_worker": {
    "running": true,
    "registered_keys": 3,
    "enabled_keys": 3
  }
}
```

### Metrics to Monitor

- **Cache Hit Rate**: `hits / (hits + misses)`
- **Average Latency**: Response time for Redis operations
- **Memory Usage**: Redis memory consumption
- **Connection Count**: Active Redis connections
- **Refresh Success Rate**: Background refresh success percentage
- **Key Expiration Rate**: How often keys expire vs refresh

### Logging

The Redis service provides structured logging:

```
2024-01-15 10:30:15 | INFO | Redis cluster connection established successfully
2024-01-15 10:30:16 | INFO | Redis refresh worker started
2024-01-15 10:30:45 | DEBUG | Redis GET hit: clairvoyance:production:feature_flags:VAD_CONFIDENCE
2024-01-15 10:31:00 | INFO | Refreshed key: feature_flags:VAD_CONFIDENCE (TTL: 60s)
```

## Testing

### Running Tests

```bash
# Install dependencies
uv sync

# Start local Redis (if not running)
redis-server

# Run Redis tests
uv run python test_redis.py
```

### Test Output

```
🚀 Starting Redis tests...

🔧 Testing basic Redis operations...
✅ SET test_key: True
✅ GET test_key: test_value
✅ EXISTS test_key: True
✅ SET JSON test_json: True
✅ GET JSON test_json: {'name': 'Redis Test', 'version': '1.0', 'features': ['caching', 'refresh']}
✅ DELETE test_key: True
✅ DELETE test_json: True

🏥 Testing Redis health check...
✅ Redis health: {'status': 'healthy', 'latency_ms': 0.89, 'connected_clients': 1, 'used_memory_human': '1.2M', 'redis_version': '7.0.5'}

🔄 Testing Redis refresh worker...
✅ Refresh worker started
✅ Registered key for auto-refresh
✅ Worker status: {'running': True, 'registered_keys': 1, 'enabled_keys': 1}
⏳ Waiting for refresh cycle...
✅ Auto-refreshed value: refreshed_value_1
✅ Refresh worker stopped

🎉 All Redis tests completed successfully!
```

## Performance Considerations

### Optimization Tips

1. **Use appropriate TTLs**: Balance freshness vs performance
2. **Batch operations**: Use pipelines for multiple operations
3. **Monitor memory**: Set maxmemory policies for production
4. **Connection pooling**: Configure appropriate pool sizes
5. **Key patterns**: Use consistent naming for easier management

### Scaling Guidelines

- **Single Redis**: Good for development and small deployments
- **Redis Cluster**: Use for production with high throughput
- **Read Replicas**: Add read replicas for read-heavy workloads
- **Sharding**: Distribute keys across multiple clusters if needed

## Troubleshooting

### Common Issues

**Connection Refused**
```bash
# Check Redis is running
redis-cli ping

# Check configuration
echo $REDIS_HOST $REDIS_PORT
```

**High Memory Usage**
```bash
# Check memory usage
redis-cli info memory

# Set memory limit
redis-cli config set maxmemory 2gb
redis-cli config set maxmemory-policy allkeys-lru
```

**Slow Operations**
```bash
# Monitor slow queries
redis-cli slowlog get 10

# Check latency
redis-cli --latency -h your-redis-host
```

### Debug Mode

Enable debug logging for troubleshooting:

```bash
export PROD_LOG_LEVEL=DEBUG
python -m app.main
```

## Security

### Production Security Checklist

- [ ] Enable Redis AUTH with strong password
- [ ] Use TLS encryption for data in transit
- [ ] Enable encryption at rest (managed services)
- [ ] Restrict network access (VPC, security groups)
- [ ] Disable dangerous commands (`FLUSHALL`, `CONFIG`)
- [ ] Monitor access logs
- [ ] Regular security updates

### Example Security Configuration

```bash
# Redis configuration
requirepass your_very_secure_password_here
tls-port 6380
tls-cert-file /path/to/redis.crt
tls-key-file /path/to/redis.key
tls-ca-cert-file /path/to/ca.crt

# Disable dangerous commands
rename-command FLUSHDB ""
rename-command FLUSHALL ""
rename-command CONFIG ""
```

## Future Enhancements

### Planned Features

1. **Pub/Sub Integration**: Cross-instance cache invalidation
2. **Metrics Collection**: Prometheus metrics export
3. **Circuit Breaker**: Automatic fallback on Redis failures
4. **Compression**: Automatic compression for large values
5. **Multi-Region**: Cross-region replication support

### Integration Opportunities

- **Feature Flag Service**: Complete DevCycle integration
- **Session Management**: Distributed session storage
- **Rate Limiting**: Redis-based rate limiting
- **Real-time Analytics**: Streaming analytics cache
- **A/B Testing**: Experiment configuration cache

---

For questions or issues, please refer to the [main documentation](../README.md) or create an issue in the repository.
