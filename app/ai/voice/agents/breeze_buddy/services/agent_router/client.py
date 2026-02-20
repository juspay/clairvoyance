"""
Smart Router Client for Clairvoyance

This module provides a client for the Smart Router service with:
- Circuit breaker pattern for resilience
- Automatic fallback to normal URLs (no pods) when Smart Router fails
- Structured logging

Uses aiohttp (the codebase standard) via create_aiohttp_session() for
automatic AWS proxy support and connection pooling.

IMPORTANT: Always use get_smart_router_client() to obtain the singleton instance.
Do NOT create SmartRouterClient() directly — doing so defeats the circuit breaker
(each instance has its own state) and wastes HTTP connections.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import aiohttp

from app.core.config.static import (
    ENABLE_VOICE_AGENT_POD_ISOLATION,
    SMART_ROUTER_API_KEY,
    SMART_ROUTER_BASE_URL,
    SMART_ROUTER_CB_FAILURE_THRESHOLD,
    SMART_ROUTER_CB_HALF_OPEN_MAX_CALLS,
    SMART_ROUTER_CB_RECOVERY_TIMEOUT,
    SMART_ROUTER_RETRY_ATTEMPTS,
    SMART_ROUTER_TIMEOUT_MS,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session


class CircuitState(Enum):
    """Circuit breaker states"""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class PodAllocation:
    """Result of a pod allocation from Smart Router"""

    pod_name: str
    ws_url: str
    source_pool: str
    allocated_at: float


@dataclass
class CircuitBreaker:
    """
    Simple circuit breaker implementation.

    State transitions:
    - CLOSED: Normal operation. Failures increment counter. When failures >= threshold -> OPEN.
    - OPEN: All requests rejected. After recovery_timeout -> HALF_OPEN.
    - HALF_OPEN: Allow limited requests. Successes -> CLOSED. Any failure -> OPEN.

    Thread safety: This implementation uses plain Python attributes (not locks).
    This is safe in a single-worker asyncio event loop because all state mutations
    happen between await points (no concurrent access). If multi-worker or
    multi-threaded deployment is needed in the future, add asyncio.Lock.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3

    failures: int = field(default=0)
    successes: int = field(default=0)
    last_failure_time: Optional[float] = field(default=None)
    state: CircuitState = field(default=CircuitState.CLOSED)

    def record_success(self):
        """Record a successful call."""
        self.failures = 0

        if self.state == CircuitState.HALF_OPEN:
            self.successes += 1
            if self.successes >= self.half_open_max_calls:
                self.state = CircuitState.CLOSED
                self.successes = 0
                logger.info("Circuit breaker CLOSED (recovered)")
        else:
            self.state = CircuitState.CLOSED

    def record_failure(self):
        """Record a failed call."""
        self.failures += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            logger.warning("Circuit breaker OPEN (recovery failed)")
        elif self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                "Circuit breaker OPEN (threshold reached)",
                extra={"threshold": self.failure_threshold, "failures": self.failures},
            )

    def check_open(self) -> bool:
        """
        Check if circuit breaker is open (rejecting requests).

        Also handles the OPEN -> HALF_OPEN transition when recovery timeout has elapsed.

        Returns:
            True if circuit is open and requests should be rejected.
        """
        if self.state != CircuitState.OPEN:
            return False

        # Check if recovery timeout has passed
        if self.last_failure_time:
            elapsed = time.time() - self.last_failure_time
            if elapsed > self.recovery_timeout:
                logger.info("Circuit breaker entering HALF_OPEN state")
                self.state = CircuitState.HALF_OPEN
                self.successes = 0
                return False

        return True


class SmartRouterClient:
    """
    Client for Smart Router service with fallback support.

    When Smart Router fails or circuit is open, falls back to returning
    normal URLs without pod allocation (routes to fallback Clairvoyance).

    IMPORTANT: Use get_smart_router_client() instead of creating instances directly.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url or SMART_ROUTER_BASE_URL
        self.timeout = aiohttp.ClientTimeout(
            total=(timeout_ms or SMART_ROUTER_TIMEOUT_MS) / 1000
        )
        self.api_key = api_key or SMART_ROUTER_API_KEY
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=SMART_ROUTER_CB_FAILURE_THRESHOLD,
            recovery_timeout=SMART_ROUTER_CB_RECOVERY_TIMEOUT,
            half_open_max_calls=SMART_ROUTER_CB_HALF_OPEN_MAX_CALLS,
        )
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session with proxy support."""
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            self._session = create_aiohttp_session(
                timeout=self.timeout,
                headers=headers,
            )
        return self._session

    async def close(self):
        """Close aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _should_use_smart_router(self) -> bool:
        """
        Determine if Smart Router should be used.

        Checks:
        1. Pod isolation must be enabled globally (static env var).
        2. Circuit breaker must not be open.

        Returns:
            True if Smart Router should be used.
        """
        if not ENABLE_VOICE_AGENT_POD_ISOLATION:
            return False

        if self.circuit_breaker.check_open():
            logger.debug("Circuit breaker open, skipping Smart Router")
            return False

        return True

    async def allocate_pod(
        self,
        call_sid: str,
        merchant_id: Optional[str] = None,
        provider: str = "exotel",
        flow: str = "v2",
        template: str = "",
    ) -> Optional[PodAllocation]:
        """
        Allocate a pod via Smart Router.

        Args:
            call_sid: Unique call identifier
            merchant_id: Optional merchant ID for tiered routing
            provider: Provider name (twilio, plivo, exotel)
            flow: WebSocket handler version ("v1" or "v2")
            template: Template name for WebSocket path (e.g., "order-confirmation").
                      If empty, Smart Router uses provider-specific defaults.

        Returns:
            PodAllocation if successful, None if no pods or error
        """
        if not self._should_use_smart_router():
            return None

        url = f"{self.base_url}/api/v1/allocate"
        payload = {
            "call_sid": call_sid,
            "merchant_id": merchant_id or "",
            "provider": provider,
            "flow": flow,
        }
        if template:
            payload["template"] = template

        start_time = time.time()

        try:
            session = await self._get_session()
            for attempt in range(1, SMART_ROUTER_RETRY_ATTEMPTS + 1):
                async with session.post(url, json=payload) as response:
                    duration_ms = (time.time() - start_time) * 1000

                    if response.status == 200:
                        try:
                            data = await response.json()
                        except (ValueError, aiohttp.ContentTypeError):
                            response_text = await response.text()
                            logger.error(
                                "Smart Router returned invalid JSON in 200 response",
                                extra={
                                    "call_sid": call_sid,
                                    "merchant_id": merchant_id,
                                    "status_code": response.status,
                                    "response_text": response_text[:200],
                                    "latency_ms": duration_ms,
                                    "provider": provider,
                                },
                            )
                            self.circuit_breaker.record_failure()
                            return None

                        allocation_data = data.get(
                            "allocation", data
                        )  # Handle both formats

                        self.circuit_breaker.record_success()

                        logger.info(
                            "Smart Router allocation successful",
                            extra={
                                "call_sid": call_sid,
                                "merchant_id": merchant_id,
                                "pod_name": allocation_data.get("pod_name"),
                                "source_pool": allocation_data.get("source_pool"),
                                "latency_ms": duration_ms,
                                "provider": provider,
                            },
                        )

                        pod_name = allocation_data.get("pod_name")
                        ws_url = allocation_data.get("ws_url")
                        if not pod_name or not ws_url:
                            logger.error(
                                "Smart Router 200 response missing required fields",
                                extra={
                                    "call_sid": call_sid,
                                    "merchant_id": merchant_id,
                                    "has_pod_name": bool(pod_name),
                                    "has_ws_url": bool(ws_url),
                                    "response_keys": list(allocation_data.keys()),
                                    "provider": provider,
                                },
                            )
                            # Contract mismatch, not a transient failure —
                            # don't trip circuit breaker
                            return None

                        return PodAllocation(
                            pod_name=pod_name,
                            ws_url=ws_url,
                            source_pool=allocation_data.get("source_pool", "unknown"),
                            allocated_at=allocation_data.get(
                                "allocated_at", time.time()
                            ),
                        )

                    if response.status == 503:
                        # No pods available — retrying won't help.
                        # Not a circuit breaker failure: Smart Router is healthy,
                        # just no capacity. Tripping the breaker here would block
                        # future calls even after pods free up.
                        logger.warning(
                            "Smart Router: No pods available",
                            extra={
                                "call_sid": call_sid,
                                "merchant_id": merchant_id,
                                "latency_ms": duration_ms,
                            },
                        )
                        return None

                    # Retryable error (500, 429, etc.)
                    error_text = await response.text()
                    logger.warning(
                        "Smart Router allocation attempt %d/%d failed",
                        attempt,
                        SMART_ROUTER_RETRY_ATTEMPTS,
                        extra={
                            "call_sid": call_sid,
                            "status_code": response.status,
                        },
                    )

                    if attempt < SMART_ROUTER_RETRY_ATTEMPTS:
                        # Exponential backoff: 100ms, 200ms, ...
                        await asyncio.sleep(0.1 * attempt)
                    else:
                        # Final attempt failed — record failure for circuit breaker
                        logger.error(
                            "Smart Router allocation failed after all retries",
                            extra={
                                "call_sid": call_sid,
                                "merchant_id": merchant_id,
                                "status_code": response.status,
                                "error": error_text,
                                "latency_ms": duration_ms,
                            },
                        )
                        self.circuit_breaker.record_failure()
                        return None

        except asyncio.TimeoutError:
            logger.error(
                "Smart Router allocation timeout",
                extra={
                    "call_sid": call_sid,
                    "merchant_id": merchant_id,
                    "timeout_ms": (self.timeout.total or 0) * 1000,
                },
            )
            self.circuit_breaker.record_failure()
            return None

        except aiohttp.ClientConnectorError as e:
            logger.error(
                "Smart Router connection error",
                extra={
                    "call_sid": call_sid,
                    "merchant_id": merchant_id,
                    "base_url": self.base_url,
                    "error": str(e),
                },
            )
            self.circuit_breaker.record_failure()
            return None

        except Exception as e:
            logger.error(
                "Smart Router allocation unexpected error",
                extra={
                    "call_sid": call_sid,
                    "merchant_id": merchant_id,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )
            self.circuit_breaker.record_failure()
            return None

    async def release_pod(
        self,
        call_sid: str,
        reason: str = "call_completed",
    ) -> bool:
        """
        Release a pod via Smart Router.

        This is idempotent - calling multiple times is safe.
        If pod already released, returns True.

        Args:
            call_sid: The call SID to release
            reason: Reason for release (call_completed, error, timeout)

        Returns:
            True if successful or already released, False on error
        """
        url = f"{self.base_url}/api/v1/release"
        payload = {
            "call_sid": call_sid,
            "reason": reason,
        }

        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.debug(
                        "Smart Router: Released pod",
                        extra={"call_sid": call_sid, "reason": reason},
                    )
                    return True

                elif response.status == 404:
                    # Call not found - already released or never allocated
                    logger.debug(
                        "Smart Router: Call not found (already released)",
                        extra={"call_sid": call_sid},
                    )
                    return True  # Consider this success - pod is not allocated

                else:
                    error_text = await response.text()
                    logger.error(
                        "Smart Router release failed",
                        extra={
                            "call_sid": call_sid,
                            "status_code": response.status,
                            "error": error_text,
                        },
                    )
                    return False

        except Exception as e:
            logger.error(
                "Smart Router release error",
                extra={
                    "call_sid": call_sid,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Global singleton instance
#
# ALWAYS use get_smart_router_client() to obtain the shared instance.
# This ensures circuit breaker state and HTTP connection pool are shared
# across all callers.
# ─────────────────────────────────────────────────────────────────────────────
_smart_router_client: Optional[SmartRouterClient] = None


def get_smart_router_client() -> SmartRouterClient:
    """
    Get the global Smart Router client singleton.

    Creates the instance on first call. The circuit breaker state and HTTP
    connection pool are shared across all callers.
    """
    global _smart_router_client
    if _smart_router_client is None:
        _smart_router_client = SmartRouterClient()
    return _smart_router_client


async def close_smart_router_client():
    """Close the global Smart Router client and release resources."""
    global _smart_router_client
    if _smart_router_client:
        await _smart_router_client.close()
        _smart_router_client = None


# ─────────────────────────────────────────────────────────────────────────────
# Convenience helpers
#
# These wrap the singleton + feature-flag check + logging so callers don't
# have to repeat the same boilerplate. Both are guaranteed to never raise.
# ─────────────────────────────────────────────────────────────────────────────


async def safe_release_pod(call_sid: str, reason: str = "call_completed") -> bool:
    """
    Release a pod if pod isolation is enabled. Never raises.

    Returns True if released (or isolation disabled / already released),
    False only on actual error.
    """
    if not ENABLE_VOICE_AGENT_POD_ISOLATION:
        return True

    client = get_smart_router_client()
    released = await client.release_pod(call_sid=call_sid, reason=reason)
    if released:
        logger.info(f"Released pod for call {call_sid} (reason={reason})")
    return released


async def safe_allocate_pod(
    call_sid: str,
    provider: str,
    merchant_id: Optional[str] = None,
    flow: str = "v2",
    template: str = "",
) -> Optional[PodAllocation]:
    """
    Allocate a pod if pod isolation is enabled. Never raises.

    Returns PodAllocation on success, None if isolation disabled,
    no pods available, or any error.
    """
    if not ENABLE_VOICE_AGENT_POD_ISOLATION:
        return None

    client = get_smart_router_client()
    allocation = await client.allocate_pod(
        call_sid=call_sid,
        merchant_id=merchant_id,
        provider=provider,
        flow=flow,
        template=template,
    )
    if allocation:
        logger.info(
            f"Allocated pod {allocation.pod_name} for call {call_sid}",
            extra={"provider": provider, "ws_url": allocation.ws_url},
        )
    else:
        logger.warning(f"No pod allocated for call {call_sid}")
    return allocation
