"""
System Health Check Router

Endpoints for monitoring system health including Redis connectivity.
"""

import time
from typing import Awaitable, cast

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from google import genai
from openai import AsyncAzureOpenAI

from app.api.security.breeze_buddy.rbac_token import (
    UserInfo,
    get_current_user_with_rbac,
)
from app.core.config.static import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    DAILY_API_KEY,
    DAILY_API_URL,
    GEMINI_API_KEY,
)
from app.core.logger import logger
from app.database import get_db_connection
from app.services.redis.client import get_redis_service
from app.services.service_health.monitor import service_health_monitor

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
            ping_result = client.ping()
            # Redis ping returns Union[Awaitable[bool], bool], await if awaitable
            if hasattr(ping_result, "__await__"):
                await cast(Awaitable[bool], ping_result)
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


@router.get("/health/gemini")
async def gemini_health_check():
    """
    Gemini API health check - tests API key validity and connectivity.
    """
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {},
        "api_info": None,
    }

    try:
        if not GEMINI_API_KEY:
            health_status["status"] = "unhealthy"
            health_status["checks"]["api_key"] = {
                "status": "failed",
                "error": "GEMINI_API_KEY not configured",
            }
            return JSONResponse(status_code=503, content=health_status)

        health_status["checks"]["api_key"] = {
            "status": "ok",
            "configured": True,
        }

        try:
            start = time.time()

            client = genai.Client(api_key=GEMINI_API_KEY)

            models_response = client.models.list()
            models = list(models_response)

            latency = round((time.time() - start) * 1000, 2)

            if models:
                health_status["checks"]["api_call"] = {
                    "status": "ok",
                    "latency_ms": latency,
                    "models_found": len(models),
                }
                health_status["api_info"] = {
                    "models_available": len(models),
                    "api_accessible": True,
                }
            else:
                health_status["status"] = "degraded"
                health_status["checks"]["api_call"] = {
                    "status": "warning",
                    "latency_ms": latency,
                    "message": "API responded but no models returned",
                }

        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["checks"]["api_call"] = {
                "status": "failed",
                "error": str(e),
                "error_type": type(e).__name__,
            }
            raise

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Gemini health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)
        return JSONResponse(status_code=503, content=health_status)


@router.get("/health/daily")
async def daily_health_check():
    """
    Daily API health check - tests API key validity and connectivity.
    """
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {},
        "api_info": None,
    }

    try:
        if not DAILY_API_KEY:
            health_status["status"] = "unhealthy"
            health_status["checks"]["api_key"] = {
                "status": "failed",
                "error": "DAILY_API_KEY not configured",
            }
            return JSONResponse(status_code=503, content=health_status)

        health_status["checks"]["api_key"] = {
            "status": "ok",
            "configured": True,
        }

        try:
            start = time.time()

            headers = {"Authorization": f"Bearer {DAILY_API_KEY}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{DAILY_API_URL}/rooms",
                    headers=headers,
                )
                response.raise_for_status()

            latency = round((time.time() - start) * 1000, 2)

            health_status["checks"]["api_call"] = {
                "status": "ok",
                "latency_ms": latency,
            }
            health_status["api_info"] = {
                "response_received": True,
            }

        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["checks"]["api_call"] = {
                "status": "failed",
                "error": str(e),
                "error_type": type(e).__name__,
            }
            raise

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Daily health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)
        return JSONResponse(status_code=503, content=health_status)


@router.get("/health/azure-openai")
async def azure_openai_health_check():
    """
    Azure OpenAI health check - tests API key validity and connectivity.
    """
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "checks": {},
        "api_info": None,
    }

    try:
        if not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_ENDPOINT:
            health_status["status"] = "unhealthy"
            health_status["checks"]["api_key"] = {
                "status": "failed",
                "error": "AZURE_OPENAI_API_KEY or AZURE_OPENAI_ENDPOINT not configured",
            }
            return JSONResponse(status_code=503, content=health_status)

        health_status["checks"]["api_key"] = {
            "status": "ok",
            "configured": True,
        }

        try:
            start = time.time()

            async with AsyncAzureOpenAI(
                api_key=AZURE_OPENAI_API_KEY,
                azure_endpoint=AZURE_OPENAI_ENDPOINT,
            ) as client:
                models = await client.models.list()
                model_list = [m async for m in models]

            latency = round((time.time() - start) * 1000, 2)

            if model_list:
                health_status["checks"]["api_call"] = {
                    "status": "ok",
                    "latency_ms": latency,
                    "models_found": len(model_list),
                }
                health_status["api_info"] = {
                    "models_available": len(model_list),
                }
            else:
                health_status["status"] = "degraded"
                health_status["checks"]["api_call"] = {
                    "status": "warning",
                    "latency_ms": latency,
                    "message": "API responded but no models returned",
                }

        except Exception as e:
            health_status["status"] = "unhealthy"
            health_status["checks"]["api_call"] = {
                "status": "failed",
                "error": str(e),
                "error_type": type(e).__name__,
            }
            raise

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Azure OpenAI health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)
        return JSONResponse(status_code=503, content=health_status)


@router.get("/health/service")
async def service_health_status():
    """Return current service-health state: pause overlay + per-rule error counts."""
    status = await service_health_monitor.get_status()
    return JSONResponse(status)


@router.post("/health/pause")
async def service_health_pause(
    request: Request,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Manually pause outbound calls. Requires admin authentication."""
    body = await request.json()
    reason = body.get("reason", "Manual pause via API")
    paused_by = current_user.username or current_user.id or "admin"
    await service_health_monitor.pause_calls(
        reason=reason, paused_by=paused_by, source_rule=None
    )
    return JSONResponse({"status": "paused", "reason": reason, "paused_by": paused_by})


@router.post("/health/resume")
async def service_health_resume(
    request: Request,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Manually resume outbound calls. Requires admin authentication."""
    resumed_by = current_user.username or current_user.id or "admin"
    await service_health_monitor.resume_calls(resumed_by=resumed_by)
    return JSONResponse({"status": "resumed", "resumed_by": resumed_by})


@router.post("/health/alert")
async def service_health_alert(
    request: Request,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Webhook endpoint for external alert systems to trigger a service pause. Requires authentication."""
    body = await request.json()
    rule = body.get("rule", "external_alert")
    reason = body.get("reason", f"External alert received for rule: {rule}")
    paused_by = current_user.username or current_user.id or "alert_webhook"
    await service_health_monitor.pause_calls(
        reason=reason, paused_by=paused_by, source_rule=rule
    )
    return JSONResponse({"status": "paused", "rule": rule, "reason": reason})


@router.get("/version")
async def get_version():
    """Get application version."""
    from app import __version__

    return JSONResponse({"version": __version__})
