"""Process-wide HTTP / API client pools for the chat LLM path.

Why this exists, in two beats:

1. **HTTP/2 is load-bearing for streaming.** The OpenAI SDK's SSE iterator
   (``openai/_streaming.py:__stream__``) breaks out on ``[DONE]`` and calls
   ``response.aclose()`` mid-body. With HTTP/1.1 httpx refuses to re-pool a
   response with unread bytes — every chat turn paid a fresh handshake. With
   HTTP/2 the SDK's per-stream close terminates only that stream; the
   underlying TCP+TLS connection multiplexes the next request on top. Azure
   OpenAI endpoints negotiate h2 via ALPN (``curl -sI --http2`` confirms
   ``HTTP/2 200``); falls back to HTTP/1.1 for endpoints that don't.

2. **Pool can't live in Redis.** httpx pools own live TCP sockets and TLS
   sessions — process-local file descriptors that no protocol can transfer
   across pods. Each pod warms its own pool on first use. Pod count is
   bounded; session count isn't, so this scales fine.

Voice is unaffected: each voice call runs in its own subprocess (one client,
lives for the call), so the per-call pool is single-entry-and-die regardless.

The Anthropic Vertex pool follows the same logic: ``AsyncAnthropicVertex``
owns its own httpx client *and* an OAuth token cache, so reusing one client
across chat turns avoids both repeated TLS handshakes and re-minting the
Vertex access token on every turn.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

import httpx
from anthropic import AsyncAnthropicVertex
from google.oauth2 import service_account

from app.core.logger import logger

__all__ = [
    "close_all_pools",
    "get_anthropic_vertex_client",
    "get_azure_httpx_client",
]

# ``keepalive_expiry=120s`` stays comfortably under Azure's idle timeout
# (~4 min) so we close from our side first and avoid "connection reset by
# peer" surprises. Limits match pipecat's generic OpenAI service
# (``pipecat/services/openai/base_llm.py:255``).
_AZURE_HTTPX_POOLS: dict[tuple[str, str], httpx.AsyncClient] = {}


def get_azure_httpx_client(endpoint: str, api_version: str) -> httpx.AsyncClient:
    """Return the shared httpx client for ``(endpoint, api_version)``."""
    key = (endpoint, api_version)
    client = _AZURE_HTTPX_POOLS.get(key)
    if client is None:
        client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(
                max_keepalive_connections=100,
                max_connections=1000,
                keepalive_expiry=120.0,
            ),
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
        _AZURE_HTTPX_POOLS[key] = client
        logger.info(
            f"Azure httpx pool created for endpoint={endpoint} api_version={api_version}"
        )
    return client


_ANTHROPIC_VERTEX_POOLS: dict[tuple[str, str, str], AsyncAnthropicVertex] = {}


def _credentials_fingerprint(credentials_json: str) -> str:
    """Stable short hash so two distinct SAs for the same project/region get
    distinct cache entries. Hash, not the raw JSON, so the cache key never
    holds private-key material."""
    return hashlib.sha256(credentials_json.encode("utf-8")).hexdigest()[:16]


def get_anthropic_vertex_client(
    *,
    credentials_json: str,
    project_id: str,
    region: str,
) -> AsyncAnthropicVertex:
    """Return a shared ``AsyncAnthropicVertex`` for ``(project, region, creds)``.

    Sharing this client across chat turns reuses both the underlying httpx
    connection pool and the Vertex OAuth token cache that the Anthropic SDK
    maintains on each ``AsyncAnthropicVertex`` instance.
    """
    key = (project_id, region, _credentials_fingerprint(credentials_json))
    client = _ANTHROPIC_VERTEX_POOLS.get(key)
    if client is None:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(credentials_json),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client = AsyncAnthropicVertex(
            region=region,
            project_id=project_id,
            credentials=creds,
        )
        _ANTHROPIC_VERTEX_POOLS[key] = client
        logger.info(
            f"AsyncAnthropicVertex pool created for project={project_id} "
            f"region={region} creds_fp={key[2]}"
        )
    return client


async def _close_anthropic_vertex_clients() -> None:
    for key, client in list(_ANTHROPIC_VERTEX_POOLS.items()):
        close: Optional[object] = getattr(client, "aclose", None) or getattr(
            client, "close", None
        )
        if close is None:
            continue
        try:
            result = close()  # type: ignore[operator]
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.warning(f"AsyncAnthropicVertex pool close failed for {key}: {exc}")
    _ANTHROPIC_VERTEX_POOLS.clear()


async def close_all_pools() -> None:
    """Close every shared client. Wired into FastAPI lifespan shutdown."""
    for key, client in list(_AZURE_HTTPX_POOLS.items()):
        try:
            await client.aclose()
        except Exception as exc:
            logger.warning(f"Azure httpx pool close failed for {key}: {exc}")
    _AZURE_HTTPX_POOLS.clear()
    logger.info("All Azure httpx pools closed")

    await _close_anthropic_vertex_clients()
    logger.info("All AsyncAnthropicVertex pools closed")
