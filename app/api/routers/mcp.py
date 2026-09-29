"""``POST /mcp/{platform}/{store}``: the public route to our MCP tools.

Mounted only when ``MCP_PUBLIC_ENDPOINT_ENABLED`` is on (``app/main.py``). Our
engine never uses it; it answers the same URL in process. There is no caller
check yet, so keep it off until auth is added here. Calls are capped per
store host.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.ai.voice.agents.breeze_buddy.mcp import in_process, server
from app.core.logger.context import set_log_context
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

# A slow store cannot hold a request open longer than this.
_DEADLINE_S = 25.0
# Calls per store per minute. The cap is per store, not per caller IP.
_CALLS_PER_STORE_MINUTE = 600


@router.post("/mcp/{platform}/{store}")
async def mcp_tools_call(platform: str, store: str, request: Request) -> JSONResponse:
    """Answer one JSON-RPC ``tools/call`` for ``store``.

    404 for a platform we do not host or a store that is not a host name;
    429 over the per-store cap. A tool failure is a JSON-RPC ``error`` with
    HTTP 200.
    """
    store = store.lower()
    call = in_process.tool_call(platform, store)
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    set_log_context(shop_url=store)

    decision = await check_rate_limit(
        bucket="mcp_tools_call",
        identifier=store,
        limit=_CALLS_PER_STORE_MINUTE,
        window_seconds=60,
        prefix="mcp",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many tool calls for this store. Try again shortly.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    try:
        body: Any = json.loads(await request.body())
    except ValueError:
        return JSONResponse(server.error(None, server.PARSE_ERROR, "Invalid JSON."))

    answer: Dict[str, Any] = await server.handle(body, call, deadline_s=_DEADLINE_S)
    return JSONResponse(answer)
