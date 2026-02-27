"""
FastAPI router for the Blueprint text agent.

Provides endpoints to generate Clairvoyance voice agent templates
from natural language descriptions using the deep agent pipeline.

Includes:
- POST /generate — One-shot generation (returns full result)
- POST /generate/stream — SSE streaming with real-time pipeline status
- GET /health — Service health check
"""

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.ai.text.blueprint.agent import (
    generate_template,
    generate_template_stream,
)
from app.core.logger import logger

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class GenerateTemplateRequest(BaseModel):
    """Request body for template generation."""

    description: str = Field(
        ...,
        min_length=10,
        description=(
            "Natural language description of the voice agent use case. "
            "Include details like: purpose, merchant name, conversation flow, "
            "what data to collect, desired outcomes, and any specific behaviors."
        ),
        examples=[
            (
                "Create a voice agent for confirming COD orders at an "
                "e-commerce store called ShopEasy. The agent should greet "
                "the customer, verify order items and delivery address, "
                "handle address updates, and allow cancellation with a reason."
            )
        ],
    )
    orchestrator_model: Optional[str] = Field(
        None,
        description="Override the orchestrator model (default: claude-sonnet).",
    )
    architect_model: Optional[str] = Field(
        None,
        description="Override the Template Architect model (default: claude-sonnet).",
    )
    enhancer_model: Optional[str] = Field(
        None,
        description="Override the Dialogue Enhancer model (default: gpt-4o).",
    )
    reviewer_model: Optional[str] = Field(
        None,
        description="Override the Reviewer model (default: claude-sonnet).",
    )


class GenerateTemplateResponse(BaseModel):
    """Response body for template generation."""

    status: str = Field(description="'success' or 'error'")
    message: str = Field(description="Human-readable status message")
    result: Optional[str] = Field(
        None,
        description="The generated template JSON as a string, or error details.",
    )


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------


async def _sse_event_generator(request: GenerateTemplateRequest):
    """Async generator that yields SSE-formatted events from the pipeline.

    Each event follows the SSE protocol:
        event: <stage>
        data: <json-payload>

    Event types emitted:
        - status: Pipeline stage updates with progress percentage
        - error: Error events
        - done: Final completion signal
    """
    async for status in generate_template_stream(
        description=request.description,
        orchestrator_model=(
            request.orchestrator_model or "anthropic:claude-sonnet-4-20250514"
        ),
        architect_model=request.architect_model,
        enhancer_model=request.enhancer_model,
        reviewer_model=request.reviewer_model,
    ):
        payload = json.dumps(status.model_dump(), default=str)
        event_type = "error" if status.stage == "error" else "status"
        yield f"event: {event_type}\ndata: {payload}\n\n"

    # Signal stream end
    yield "event: done\ndata: {}\n\n"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/generate",
    response_model=GenerateTemplateResponse,
    summary="Generate a voice agent template",
    description=(
        "Takes a natural language description and generates a production-ready "
        "Clairvoyance voice agent template JSON through a 3-agent pipeline: "
        "Template Architect (structure) -> Dialogue Enhancer (natural language) "
        "-> Reviewer (validation)."
    ),
)
async def generate_template_endpoint(
    request: GenerateTemplateRequest,
) -> GenerateTemplateResponse:
    """Generate a voice agent template from a natural language description."""
    logger.info(f"Template generation requested: {request.description[:100]}...")

    try:
        result = await generate_template(
            description=request.description,
            orchestrator_model=(
                request.orchestrator_model or "anthropic:claude-sonnet-4-20250514"
            ),
            architect_model=request.architect_model,
            enhancer_model=request.enhancer_model,
            reviewer_model=request.reviewer_model,
        )

        return GenerateTemplateResponse(
            status=result["status"],
            message=result["message"],
            result=result.get("result"),
        )
    except Exception as e:
        logger.error(f"Template generation endpoint error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Template generation failed: {str(e)}",
        )


@router.post(
    "/generate/stream",
    summary="Generate a template with real-time status updates (SSE)",
    description=(
        "Server-Sent Events endpoint that streams real-time pipeline status "
        "updates as the template is generated. Connect with EventSource or "
        "fetch() to receive live progress.\n\n"
        "Event types:\n"
        "- `status`: Pipeline stage update with progress percentage\n"
        "- `error`: Error occurred during generation\n"
        "- `done`: Pipeline completed\n\n"
        "Each event data payload contains: stage, message, progress_pct, "
        "agent_name, elapsed_secs, detail."
    ),
)
async def generate_template_stream_endpoint(
    request: GenerateTemplateRequest,
) -> StreamingResponse:
    """Stream real-time pipeline status via Server-Sent Events."""
    logger.info(
        f"Streaming template generation requested: {request.description[:100]}..."
    )

    return StreamingResponse(
        _sse_event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/health",
    summary="Blueprint agent health check",
)
async def health_check():
    """Check if the Blueprint agent service is available."""
    return {"status": "ok", "service": "blueprint"}
