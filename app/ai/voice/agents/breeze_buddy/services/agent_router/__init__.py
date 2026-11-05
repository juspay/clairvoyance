"""
Smart Router Service

Provides the Smart Router HTTP client for the Breeze Buddy voice agent.
Handles pod allocation and release requests with circuit breaker resilience.

Pod lifecycle (registration, pool management, heartbeats) is handled entirely
by Smart Router via Kubernetes API watch.
"""

from app.ai.voice.agents.breeze_buddy.services.agent_router.client import (
    PodAllocation,
    SmartRouterClient,
    close_smart_router_client,
    get_smart_router_client,
    safe_allocate_pod,
    safe_release_pod,
)

__all__ = [
    "SmartRouterClient",
    "PodAllocation",
    "get_smart_router_client",
    "close_smart_router_client",
    "safe_allocate_pod",
    "safe_release_pod",
]
