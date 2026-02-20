"""
Pod Health Probe Endpoints for Breeze Buddy Voice Agent

Health and readiness probes for the voice agent pod isolation architecture.

Endpoints:
- /health: Liveness probe (is the process alive?)
- /health/live: Alias for liveness (Kubernetes compatibility)
- /health/ready: Readiness probe (can this pod accept traffic?)
- /status: Pod status info

Admin operations (pool status, force release, pod info) are handled
directly by Smart Router's own API endpoints.

Pod lifecycle (registration, pool management, heartbeats) is handled entirely
by Smart Router via Kubernetes API watch. These endpoints only need to serve
K8s probes.
"""

from fastapi import APIRouter

from app.core.config.static import (
    ENABLE_VOICE_AGENT_POD_ISOLATION,
    VOICE_AGENT_POD_IP,
    VOICE_AGENT_POD_NAME,
)

router = APIRouter()


@router.get("/health")
async def health():
    """
    Liveness probe endpoint.

    Returns 200 if the application process is running. Does NOT check
    external dependencies (Redis, Smart Router) to avoid cascade failures.

    Kubernetes uses this to determine if the pod should be restarted.
    """
    return {
        "healthy": True,
        "pod_name": VOICE_AGENT_POD_NAME or "unknown",
        "pod_ip": VOICE_AGENT_POD_IP or "unknown",
    }


@router.get("/health/live")
async def health_live():
    """
    Liveness probe endpoint (alias for /health).

    Added for Kubernetes compatibility - some clusters expect /health/live.
    """
    return await health()


@router.get("/health/ready")
async def ready():
    """
    Readiness probe endpoint.

    Returns 200 if the pod can accept traffic. Smart Router uses K8s
    readiness to decide whether to include this pod in allocation pools.

    Currently always returns ready -- the pod is ready as long as the
    process is running. Smart Router manages allocation state separately.
    """
    return {
        "ready": True,
        "pod_name": VOICE_AGENT_POD_NAME or "unknown",
    }


@router.get("/status")
async def status():
    """
    Pod status endpoint.

    Returns basic pod identity info. For detailed allocation state,
    query Smart Router directly.
    """
    return {
        "pod_name": VOICE_AGENT_POD_NAME or "unknown",
        "pod_ip": VOICE_AGENT_POD_IP or "unknown",
        "isolation_enabled": ENABLE_VOICE_AGENT_POD_ISOLATION,
    }
