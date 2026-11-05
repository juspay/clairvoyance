"""Google Vertex AI (Gemini) LLM config and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.google.vertex.llm import (
    GoogleVertexLLMService,
    GoogleVertexLLMSettings,
)

from app.core.logger import logger

__all__ = ["VertexConfig", "build_vertex_llm"]


@dataclass
class VertexConfig:
    """Configuration for Google Vertex AI (Gemini) LLM."""

    credentials_json: str
    project_id: str
    location: str
    model: str
    temperature: float
    max_tokens: int
    thinking_budget: Optional[int] = None
    thinking_level: Optional[str] = None
    function_call_timeout_secs: float = 10.0


def build_vertex_llm(config: VertexConfig) -> GoogleVertexLLMService:
    """Create a Google Vertex AI LLM service instance.

    Args:
        config: Vertex AI-specific configuration.

    Returns:
        Configured GoogleVertexLLMService instance.
    """
    logger.info(
        f"Building Google Vertex LLM service with model={config.model}, "
        f"project_id={config.project_id}, location={config.location}, "
        f"thinking_budget={config.thinking_budget}, thinking_level={config.thinking_level}"
    )

    # Build thinking config if either budget or level is provided
    thinking = None
    if config.thinking_budget is not None or config.thinking_level is not None:
        thinking = GoogleLLMService.ThinkingConfig(
            thinking_budget=config.thinking_budget,
            thinking_level=config.thinking_level,
        )

    settings_kwargs: dict[str, Any] = {
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
    }
    if thinking is not None:
        settings_kwargs["thinking"] = thinking

    return GoogleVertexLLMService(
        credentials=config.credentials_json,
        project_id=config.project_id,
        location=config.location,
        model=config.model,
        settings=GoogleVertexLLMSettings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
