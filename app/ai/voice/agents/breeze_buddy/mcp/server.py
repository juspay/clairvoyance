"""Answers MCP ``tools/call`` requests: the server side of
``mcp/__init__.py``'s direct HTTP handler.

Used in process by our engine (``mcp/in_process.py``) and by the public
route when it is on. Platform-neutral: the caller passes the tool function.
It serves ``tools/call`` only; templates declare the tool schemas.

A failed tool call is a JSON-RPC ``error``, never a result with ``isError``:
the engine's handler turns ``error`` into ``status: "error"`` but keeps an
``isError`` result as success, and the add-to-cart flow would then read the
error as an empty cart and replace the shopper's cart.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict

from app.core.logger import logger

ToolCall = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TOOL_FAILED = -32000


class ToolError(Exception):
    """A tool call that could not be answered; the message is for the model."""


def error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


async def handle(body: Any, call: ToolCall, *, deadline_s: float) -> Dict[str, Any]:
    """Answer one JSON-RPC request with ``call(tool_name, arguments)``."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 request.")
    request_id = body.get("id")
    if body.get("method") != "tools/call":
        return error(request_id, METHOD_NOT_FOUND, "Only tools/call is served.")
    params = body.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    arguments = (params.get("arguments") if isinstance(params, dict) else None) or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return error(request_id, INVALID_PARAMS, "params needs a name and arguments.")
    try:
        async with asyncio.timeout(deadline_s):
            payload = await call(name, arguments)
    except ToolError as e:
        return error(request_id, TOOL_FAILED, str(e))
    except TimeoutError:
        return error(
            request_id, TOOL_FAILED, f"The tool did not answer within {deadline_s}s."
        )
    except Exception as e:
        logger.warning(f"[mcp] tool {name!r} failed: {e!r}")
        return error(request_id, INTERNAL_ERROR, "The tool failed.")
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }
