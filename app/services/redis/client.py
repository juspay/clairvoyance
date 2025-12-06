"""
Simplified Redis Client Service

Basic Redis client for essential key-value operations only.
"""

from typing import Any, Dict, List, Optional

from redis.asyncio import Redis
from redis.asyncio.cluster import ClusterNode, RedisCluster
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from app.core.config.static import (
    REDIS_CLUSTER_NODES,
    REDIS_HOST,
    REDIS_PORT,
    REDIS_TTL,
)
from app.core.logger import logger


def is_redis_configured() -> bool:
    """Check if Redis is properly configured"""
    if REDIS_HOST.strip() and REDIS_PORT.strip():
        try:
            int(REDIS_PORT)  # Validate port is a number
            return True
        except ValueError:
            return False

    if REDIS_CLUSTER_NODES.strip():
        return True

    return False


class RedisFactory:
    """Simple factory for creating Redis connections"""

    def __init__(self):
        self._cluster_client: Optional[RedisCluster] = None
        self._single_client: Optional[Redis] = None

    async def get_client(self) -> Redis:
        """Get appropriate Redis client (cluster or single)"""
        if self._cluster_client is not None:
            return self._cluster_client
        elif self._single_client is not None:
            return self._single_client
        else:
            await self._create_client()
            return self._cluster_client or self._single_client

    async def _create_client(self) -> None:
        """Create Redis client"""
        try:
            startup_nodes = self._get_startup_nodes()

            # Determine if we should use cluster mode
            # Use cluster if: explicitly set OR multiple nodes configured
            use_cluster = len(startup_nodes) and REDIS_CLUSTER_NODES

            if use_cluster:
                # CLUSTER MODE
                logger.info(
                    f"Initializing Redis CLUSTER with {len(startup_nodes)} node(s): {startup_nodes}"
                )

                # Convert dict nodes to ClusterNode objects
                cluster_nodes = [
                    ClusterNode(host=node["host"], port=node["port"])
                    for node in startup_nodes
                ]

                self._cluster_client = RedisCluster(
                    startup_nodes=cluster_nodes,
                    decode_responses=True,  # Decode bytes to strings automatically
                )

                # Test connection
                await self._cluster_client.ping()
                logger.info("Redis cluster connection established successfully")

            else:
                # SINGLE NODE MODE
                node = startup_nodes[0]
                logger.info(
                    f"Initializing Redis SINGLE NODE at {node['host']}:{node['port']}"
                )

                self._single_client = Redis(
                    host=node["host"],
                    port=node["port"],
                    decode_responses=True,  # Decode bytes to strings automatically
                )

                # Test connection
                await self._single_client.ping()
                logger.info("Redis single-node connection established successfully")

        except RedisConnectionError as e:
            logger.error(f"Redis connection failed: {e}", exc_info=True)
            raise
        except ValueError as e:
            logger.error(f"Redis configuration error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating Redis client: {e}", exc_info=True)
            raise RedisConnectionError(f"Cannot connect to Redis: {e}") from e

    def _get_startup_nodes(self) -> List[Dict[str, Any]]:
        """Get Redis nodes configuration"""
        startup_nodes = []

        # Single node configuration
        if REDIS_HOST.strip() and REDIS_PORT.strip():
            try:
                port = int(REDIS_PORT)
                startup_nodes.append({"host": REDIS_HOST, "port": port})
                logger.info(f"Using Redis single node: {REDIS_HOST}:{port}")
            except ValueError:
                logger.error(f"Invalid Redis port: {REDIS_PORT}")

        # Cluster nodes configuration
        elif REDIS_CLUSTER_NODES.strip():
            for hostport in REDIS_CLUSTER_NODES.split(","):
                hostport = hostport.strip()
                if not hostport:
                    continue
                try:
                    host, port = hostport.split(":")
                    startup_nodes.append(
                        {"host": host.strip(), "port": int(port.strip())}
                    )
                except ValueError:
                    logger.error(f"Invalid Redis cluster node format: {hostport}")
                    continue

            if startup_nodes:
                logger.info(f"Using Redis cluster with {len(startup_nodes)} nodes")

        if not startup_nodes:
            raise ValueError(
                "No valid Redis configuration found. Please set REDIS_HOST and REDIS_PORT."
            )

        return startup_nodes

    async def close(self) -> None:
        """Close Redis connections"""
        try:
            if self._cluster_client:
                await self._cluster_client.aclose()
                self._cluster_client = None

            if self._single_client:
                await self._single_client.aclose()
                self._single_client = None

            logger.info("Redis connections closed")
        except Exception as e:
            logger.error(f"Error closing Redis connections: {e}", exc_info=True)
            self._cluster_client = None
            self._single_client = None


class RedisService:
    """Minimal Redis service with basic operations"""

    def __init__(self):
        self._factory: Optional[RedisFactory] = None

    async def get_client(self) -> Redis:
        """Get Redis client instance"""
        if not is_redis_configured():
            raise ValueError("Redis is not properly configured")

        if self._factory is None:
            self._factory = RedisFactory()

        return await self._factory.get_client()

    async def get(self, key: str) -> Optional[str]:
        """Get value from Redis"""
        try:
            client = await self.get_client()
            value = await client.get(key)
            return value
        except RedisError as e:
            logger.error(f"Redis GET error for key {key}: {e}")
            return None

    async def set(
        self, key: str, value: str, nx: bool = False, ex: Optional[int] = None
    ) -> bool:
        """
        Set value in Redis with optional NX (only if not exists) and EX (expiration).

        Args:
            key: Redis key
            value: Value to set
            nx: Only set if key doesn't exist (default: False)
            ex: Expiration time in seconds (default: None)

        Returns:
            True if key was set, False otherwise
        """
        try:
            client = await self.get_client()
            result = await client.set(key, value, nx=nx, ex=ex)
            return bool(result)
        except RedisError as e:
            logger.error(f"Redis SET error for key {key}: {e}")
            return False

    async def setex(
        self, key: str, value: str, ttl_seconds: Optional[int] = None
    ) -> bool:
        """Set value in Redis with TTL (expiration)"""
        try:
            # Use provided TTL or default from environment
            ttl = ttl_seconds if ttl_seconds is not None else REDIS_TTL

            client = await self.get_client()
            result = await client.setex(key, ttl, value)
            return bool(result)
        except RedisError as e:
            logger.error(f"Redis SETEX error for key {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from Redis"""
        try:
            client = await self.get_client()
            result = await client.delete(key)
            return bool(result)
        except RedisError as e:
            logger.error(f"Redis DELETE error for key {key}: {e}")
            return False

    async def exists(self, key: str) -> bool:
        """Check if key exists in Redis"""
        try:
            client = await self.get_client()
            result = await client.exists(key)
            return bool(result)
        except RedisError as e:
            logger.error(f"Redis EXISTS error for key {key}: {e}")
            return False

    async def ping(self) -> bool:
        """Test Redis connection"""
        try:
            client = await self.get_client()
            await client.ping()
            return True
        except Exception as e:
            logger.error(f"Redis PING error: {e}")
            return False

    async def incr(self, key: str) -> int:
        """Increment value in Redis by 1. Creates key with value 1 if it doesn't exist."""
        try:
            client = await self.get_client()
            result = await client.incr(key)
            return result
        except RedisError as e:
            logger.error(f"Redis INCR error for key {key}: {e}")
            raise

    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiration time on a key in seconds."""
        try:
            client = await self.get_client()
            result = await client.expire(key, seconds)
            return bool(result)
        except RedisError as e:
            logger.error(f"Redis EXPIRE error for key {key}: {e}")
            return False

    async def close(self) -> None:
        """Close Redis connections"""
        if self._factory:
            await self._factory.close()
            self._factory = None


# Global Redis service instance
_redis_service: Optional[RedisService] = None


async def get_redis_service() -> RedisService:
    """Get global Redis service instance"""
    global _redis_service
    if _redis_service is None:
        _redis_service = RedisService()
    return _redis_service


async def close_redis_connections():
    """Close all Redis connections"""
    global _redis_service
    if _redis_service:
        try:
            await _redis_service.close()
            _redis_service = None
        except Exception as e:
            logger.error(f"Error closing Redis connections: {e}", exc_info=True)
            _redis_service = None  # Reset even if close fails
