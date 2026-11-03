"""
System Health Check Router

Endpoints for monitoring system health including Redis connectivity.
Responses carry the error type only; the error text goes to the logs.
"""

import time
from typing import Awaitable, cast

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from google import genai
from openai import AsyncAzureOpenAI

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.config.static import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    DAILY_API_KEY,
    DAILY_API_URL,
    GEMINI_API_KEY,
)
from app.core.logger import logger
from app.core.security.authorization import require_admin
from app.database import db_connection
from app.schemas import UserInfo
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
            health_status["checks"]["ping"] = {
                "status": "failed",
                "error_type": type(e).__name__,
            }
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
            health_status["checks"]["set"] = {
                "status": "failed",
                "error_type": type(e).__name__,
            }
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
            health_status["checks"]["get"] = {
                "status": "failed",
                "error_type": type(e).__name__,
            }
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
            logger.error(f"Redis health check DELETE failed: {e}")
            health_status["status"] = "unhealthy"
            health_status["checks"]["delete"] = {
                "status": "failed",
                "error_type": type(e).__name__,
            }

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
                logger.error(f"Redis health check cluster info failed: {e}")
                health_status["cluster_info"] = {
                    "mode": "cluster",
                    "error_type": type(e).__name__,
                }
        else:
            health_status["cluster_info"] = {"mode": "single-node"}

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error_type"] = type(e).__name__
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
        async with db_connection() as conn:
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
                "message": "Database connection failed",
                "error_type": type(e).__name__,
            },
        )


@router.get("/health/gemini")
async def gemini_health_check(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Admin only: each request makes a live call to Gemini.

    Gemini API health check - tests API key validity and connectivity.
    """
    require_admin(current_user)
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
                "error_type": type(e).__name__,
            }
            raise

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Gemini health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error_type"] = type(e).__name__
        return JSONResponse(status_code=503, content=health_status)


@router.get("/health/daily")
async def daily_health_check(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Admin only: each request makes a live call to Daily.

    Daily API health check - tests API key validity and connectivity.
    """
    require_admin(current_user)
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
                "error_type": type(e).__name__,
            }
            raise

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Daily health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error_type"] = type(e).__name__
        return JSONResponse(status_code=503, content=health_status)


@router.get("/health/azure-openai")
async def azure_openai_health_check(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Admin only: each request makes a live call to Azure OpenAI.

    Azure OpenAI health check - tests API key validity and connectivity.
    """
    require_admin(current_user)
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
                "error_type": type(e).__name__,
            }
            raise

        status_code = 200 if health_status["status"] != "unhealthy" else 503
        return JSONResponse(status_code=status_code, content=health_status)

    except Exception as e:
        logger.error(f"Azure OpenAI health check failed: {e}")
        health_status["status"] = "unhealthy"
        health_status["error_type"] = type(e).__name__
        return JSONResponse(status_code=503, content=health_status)


@router.get("/version")
async def get_version():
    """Get application version."""
    from app import __version__

    return JSONResponse({"version": __version__})
