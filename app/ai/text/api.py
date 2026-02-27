"""
FastAPI router for the Text Agent playground.

Generic router that provides access to all registered text agents.
Lives at /agent/text/ and includes the Blueprint sub-router.
"""

import json
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from app.ai.text.blueprint.api import router as blueprint_router
from app.ai.text.playground import (
    PlaygroundRequest,
    PlaygroundResponse,
    _register_defaults,
    get_agent,
    list_agents,
)
from app.ai.text.playground_ui import PLAYGROUND_HTML
from app.core.logger import logger

router = APIRouter()

# Include Blueprint agent router
router.include_router(blueprint_router, prefix="/blueprint", tags=["Blueprint"])

# Register default agents on import
_register_defaults()


# ---------------------------------------------------------------------------
# Playground UI (HTML)
# ---------------------------------------------------------------------------


@router.get(
    "/playground",
    response_class=HTMLResponse,
    summary="Text Agent Playground UI",
    description="Interactive web UI for testing text agents with live SSE streaming.",
    include_in_schema=False,
)
async def playground_ui():
    """Serve the interactive playground HTML page."""
    return HTMLResponse(content=PLAYGROUND_HTML)


# ---------------------------------------------------------------------------
# Playground API endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/agents",
    summary="List available text agents",
    description="Returns all registered text agents with their descriptions.",
)
async def list_available_agents():
    """List all registered text agents."""
    return {"agents": list_agents()}


@router.post(
    "/playground/run",
    response_model=PlaygroundResponse,
    summary="Run a text agent",
    description=(
        "Invoke any registered text agent with an input prompt. "
        "Returns the result synchronously."
    ),
)
async def run_agent(request: PlaygroundRequest) -> PlaygroundResponse:
    """Run a registered text agent with the given input."""
    agent_info = get_agent(request.agent)
    if not agent_info:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Agent '{request.agent}' not found. "
                f"Available: {[a['name'] for a in list_agents()]}"
            ),
        )

    logger.info(f"Playground run: agent={request.agent}, input={request.input[:80]}...")
    start_time = time.time()

    try:
        result = await agent_info["invoke_fn"](description=request.input)
        elapsed = round(time.time() - start_time, 1)

        return PlaygroundResponse(
            agent=request.agent,
            status=result.get("status", "unknown"),
            message=result.get("message", ""),
            result=result.get("result"),
            elapsed_secs=elapsed,
        )
    except Exception as e:
        logger.error(f"Playground run failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Agent execution failed: {str(e)}",
        )


@router.post(
    "/playground/stream",
    summary="Run a text agent with real-time SSE status updates",
    description=(
        "Server-Sent Events endpoint that streams real-time status updates "
        "from any registered text agent. Connect with EventSource.\n\n"
        "Event types: status, error, done."
    ),
)
async def stream_agent(request: PlaygroundRequest) -> StreamingResponse:
    """Stream real-time status from a text agent via SSE."""
    agent_info = get_agent(request.agent)
    if not agent_info:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Agent '{request.agent}' not found. "
                f"Available: {[a['name'] for a in list_agents()]}"
            ),
        )

    if not agent_info["stream_fn"]:
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{request.agent}' does not support streaming.",
        )

    logger.info(
        f"Playground stream: agent={request.agent}, input={request.input[:80]}..."
    )

    stream_fn = agent_info["stream_fn"]

    async def _event_generator():
        async for status in stream_fn(description=request.input):
            payload = json.dumps(
                status.model_dump() if hasattr(status, "model_dump") else status,
                default=str,
            )
            event_type = (
                "error" if getattr(status, "stage", "") == "error" else "status"
            )
            yield f"event: {event_type}\ndata: {payload}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/health",
    summary="Text agent service health check",
)
async def health_check():
    """Check if the text agent service is available."""
    return {
        "status": "ok",
        "service": "text-agents",
        "agents_registered": len(list_agents()),
    }
