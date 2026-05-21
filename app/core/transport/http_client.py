"""
Centralized HTTP client factory with proxy support for both httpx and aiohttp.

Pool sizing
-----------
``aiohttp.ClientSession``'s default ``TCPConnector`` is unbounded per
host. Two pieces of machinery here keep that under control:

1. ``create_aiohttp_session()`` installs a bounded ``TCPConnector`` by
   default (sized via ``AIOHTTP_DEFAULT_POOL_LIMIT`` /
   ``AIOHTTP_DEFAULT_POOL_LIMIT_PER_HOST``) so per-request callers stop
   consuming the unbounded default. Callers can override by passing
   their own ``connector=`` kwarg.

2. ``init_shared_aiohttp_session()`` exposes a single process-wide
   session sized via ``AIOHTTP_SHARED_POOL_LIMIT`` /
   ``AIOHTTP_SHARED_POOL_LIMIT_PER_HOST``. The lifespan handler in
   ``app/main.py`` initialises it on startup and closes it on
   shutdown. Long-lived consumers (dispatch workers, Daily REST
   helper) pull this via ``get_shared_aiohttp_session()`` instead of
   each opening their own.

The shared session is created inside the lifespan's async context —
never at import time — so aiohttp's running-loop requirement is
respected on every supported Python version.
"""

from typing import Optional

import aiohttp
import httpx

from app.core.config.static import (
    AIOHTTP_DEFAULT_POOL_LIMIT,
    AIOHTTP_DEFAULT_POOL_LIMIT_PER_HOST,
    AIOHTTP_SHARED_POOL_LIMIT,
    AIOHTTP_SHARED_POOL_LIMIT_PER_HOST,
    AWS_PROXY_HOST,
    AWS_PROXY_PORT,
    CLOUD_ENVIRONMENT,
)
from app.core.logger import logger

# Module-level singleton. Owned by the FastAPI lifespan handler: init on
# startup, close on shutdown. Never created at import time — aiohttp's
# TCPConnector requires a running event loop.
_shared_session: Optional[aiohttp.ClientSession] = None


def get_proxy_config() -> Optional[str]:
    """Get proxy configuration from environment variables"""
    # Only use proxy configuration for AWS cloud environment
    if CLOUD_ENVIRONMENT.upper() != "AWS":
        logger.debug(
            f"Skipping proxy configuration for cloud environment: {CLOUD_ENVIRONMENT}"
        )
        return None

    if AWS_PROXY_HOST and AWS_PROXY_PORT:
        proxy_url = f"http://{AWS_PROXY_HOST}:{AWS_PROXY_PORT}"
        logger.info(f"Using proxy configuration for AWS environment: {proxy_url}")
        return proxy_url

    logger.debug("No proxy configuration found for AWS environment")
    return None


def create_http_client(**kwargs) -> httpx.AsyncClient:
    """
    Create an httpx AsyncClient with proxy support

    Args:
        **kwargs: Additional arguments to pass to httpx.AsyncClient

    Returns:
        httpx.AsyncClient: Configured HTTP client
    """
    proxy_url = get_proxy_config()

    client_kwargs = kwargs.copy()
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
        logger.debug(f"Created httpx client with proxy: {proxy_url}")
    else:
        logger.debug("Created httpx client without proxy")

    return httpx.AsyncClient(**client_kwargs)


def create_aiohttp_session(**session_kwargs) -> aiohttp.ClientSession:
    """
    Create an aiohttp.ClientSession with proxy support and a bounded pool.

    When no explicit ``connector=`` is supplied we substitute a bounded
    ``TCPConnector`` sized via the ``AIOHTTP_DEFAULT_POOL_LIMIT*``
    settings, so per-request callers don't each inherit aiohttp's
    unbounded default. Callers that need different sizing (e.g. the
    shared session) can still pass their own ``connector=`` and we'll
    respect it.

    For the long-lived dispatch-worker / Daily REST case, prefer
    ``get_shared_aiohttp_session()`` — that avoids creating N independent
    connection pools, one per worker.
    """
    proxy_url = get_proxy_config()

    if proxy_url:
        session_kwargs["proxy"] = proxy_url
        logger.debug(f"Created aiohttp session with proxy: {proxy_url}")
    else:
        logger.debug("Created aiohttp session without proxy")

    if "connector" not in session_kwargs:
        session_kwargs["connector"] = aiohttp.TCPConnector(
            limit=AIOHTTP_DEFAULT_POOL_LIMIT,
            limit_per_host=AIOHTTP_DEFAULT_POOL_LIMIT_PER_HOST,
        )

    return aiohttp.ClientSession(**session_kwargs)


async def init_shared_aiohttp_session() -> aiohttp.ClientSession:
    """
    Initialise the process-wide shared aiohttp session. Idempotent.

    The session uses a ``TCPConnector`` sized via the
    ``AIOHTTP_SHARED_POOL_LIMIT*`` settings — enough headroom for
    ``BB_WORKER_COUNT`` dispatch workers plus the FastAPI request path
    to share one pool without contending under burst load.

    Must be called from inside a running event loop (the FastAPI
    lifespan handler is the intended caller). The matching
    ``close_shared_aiohttp_session()`` must be called from lifespan
    shutdown to drain the connector cleanly.
    """
    global _shared_session
    if _shared_session is not None and not _shared_session.closed:
        return _shared_session
    connector = aiohttp.TCPConnector(
        limit=AIOHTTP_SHARED_POOL_LIMIT,
        limit_per_host=AIOHTTP_SHARED_POOL_LIMIT_PER_HOST,
    )
    _shared_session = create_aiohttp_session(connector=connector)
    logger.info(
        "Initialised shared aiohttp session "
        f"(limit={AIOHTTP_SHARED_POOL_LIMIT}, "
        f"limit_per_host={AIOHTTP_SHARED_POOL_LIMIT_PER_HOST})"
    )
    return _shared_session


def get_shared_aiohttp_session() -> aiohttp.ClientSession:
    """
    Return the process-wide shared aiohttp session.

    Raises ``RuntimeError`` if ``init_shared_aiohttp_session()`` has not
    been called yet — that's a programming error: the lifespan handler
    must initialise the session before workers or handlers consume it.
    """
    if _shared_session is None or _shared_session.closed:
        raise RuntimeError(
            "Shared aiohttp session is not initialised. "
            "Call init_shared_aiohttp_session() during application startup."
        )
    return _shared_session


async def close_shared_aiohttp_session() -> None:
    """Close the process-wide shared aiohttp session, if any."""
    global _shared_session
    if _shared_session is not None and not _shared_session.closed:
        await _shared_session.close()
        logger.info("Closed shared aiohttp session")
    _shared_session = None
