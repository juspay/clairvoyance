"""
System Health Check Router

Endpoints for monitoring system health including Redis connectivity.
"""

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.logger import logger
from app.database import get_db_connection
from app.services.redis.client import get_redis_service

router = APIRouter()


@router.get("/health/redis")
async def redis_health_check():
    """
    Redis health check - tests PING, SET, GET, DELETE operations.
    """
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {},
        "cluster_info": None,
    }

    test_key = "health:check:test"
    test_value = f"test-{int(time.time())}"

    try:
        redis_service = await get_redis_service()
        client = await redis_service.get_client()

        # Check 1: PING
        try:
            start = time.time()
            await client.ping()
            health_status["checks"]["ping"] = {
                "status": "ok",
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["checks"]["ping"] = {"status": "failed", "error": str(e)}
            raise

        # Check 2: SET
        try:
            start = time.time()
            await client.set(test_key, test_value)
            health_status["checks"]["set"] = {
                "status": "ok",
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["checks"]["set"] = {"status": "failed", "error": str(e)}
            raise

        # Check 3: GET
        try:
            start = time.time()
            retrieved_value = await client.get(test_key)
            latency = round((time.time() - start) * 1000, 2)

            if retrieved_value == test_value:
                health_status["checks"]["get"] = {
                    "status": "ok",
                    "latency_ms": latency,
                    "value_match": True,
                }
            else:
                health_status["status"] = "degraded"
                health_status["checks"]["get"] = {
                    "status": "warning",
                    "latency_ms": latency,
                    "value_match": False,
                    "expected": test_value,
                    "actual": retrieved_value,
                }
        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["checks"]["get"] = {"status": "failed", "error": str(e)}
            raise

        # Check 4: DELETE
        try:
            start = time.time()
            deleted_count = await client.delete(test_key)
            health_status["checks"]["delete"] = {
                "status": "ok",
                "latency_ms": round((time.time() - start) * 1000, 2),
                "keys_deleted": deleted_count,
            }
        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["checks"]["delete"] = {"status": "failed", "error": str(e)}

        # Check 5: Cluster Info
        if hasattr(client, "get_nodes"):
            try:
                nodes = client.get_nodes()
                health_status["cluster_info"] = {
                    "mode": "cluster",
                    "node_count": len(nodes),
                    "nodes": [
                        {
                            "host": node.host,
                            "port": node.port,
                            "name": (
                                node.name
                                if hasattr(node, "name")
                                else f"{node.host}:{node.port}"
                            ),
                        }
                        for node in nodes
                    ],
                }
            except Exception as e:
                health_status["cluster_info"] = {"mode": "cluster", "error": str(e)}
        else:
            health_status["cluster_info"] = {"mode": "single-node"}

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)
        return JSONResponse(status_code=503, content=health_status)


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    logger.info("Health check endpoint called")
    return JSONResponse({"status": "healthy"})


@router.get("/health/database")
async def database_health_check():
    """Check database connectivity and health."""
    logger.info("Database health check endpoint called")
    try:
        async for conn in get_db_connection():
            result = await conn.fetchval("SELECT 1")
            if result == 1:
                return JSONResponse(
                    {
                        "status": "healthy",
                        "database": "connected",
                        "message": "Database connection is healthy",
                    }
                )
            else:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "unhealthy",
                        "database": "error",
                        "message": "Database query returned unexpected result",
                    },
                )
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return JSONResponse(
            status_code=400,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "message": f"Database connection failed: {str(e)}",
            },
        )


@router.get("/version")
async def get_version():
    """Get application version."""
    from app import __version__

    return JSONResponse({"version": __version__})
