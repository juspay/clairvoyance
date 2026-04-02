"""Claude on Vertex AI LLM config and builder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from anthropic import AsyncAnthropicVertex
from google.oauth2 import service_account
from pipecat.services.anthropic.llm import AnthropicLLMService

from app.core.logger import logger

__all__ = ["ClaudeVertexConfig", "build_claude_vertex_llm"]


@dataclass
class ClaudeVertexConfig:
    """Configuration for Claude on Vertex AI.

    Uses Anthropic SDK with a Vertex AI endpoint.
    Authentication is via Google service account credentials.
    """

    credentials_json: str
    project_id: str
    region: str
    model: str
    temperature: float
    max_tokens: int
    thinking_enabled: bool = False
    thinking_budget_tokens: Optional[int] = None


def build_claude_vertex_llm(config: ClaudeVertexConfig) -> AnthropicLLMService:
    """Create a Claude on Vertex AI LLM service instance.

    Args:
        config: Claude Vertex-specific configuration.

    Returns:
        Configured AnthropicLLMService instance with Vertex AI client.
    """
    logger.info(
        f"Building Claude Vertex LLM service with model={config.model}, "
        f"project_id={config.project_id}, region={config.region}, "
        f"thinking_enabled={config.thinking_enabled}"
    )

    creds = service_account.Credentials.from_service_account_info(
        json.loads(config.credentials_json),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )

    vertex_client = AsyncAnthropicVertex(
        region=config.region,
        project_id=config.project_id,
        credentials=creds,
    )

    # Build thinking config if enabled
    thinking = None
    if config.thinking_enabled and config.thinking_budget_tokens:
        thinking = AnthropicLLMService.ThinkingConfig(
            type="enabled",
            budget_tokens=config.thinking_budget_tokens,
        )

    params = AnthropicLLMService.InputParams(
        max_tokens=config.max_tokens,
        temperature=config.temperature,
    )
    if thinking is not None:
        params.thinking = thinking

    return AnthropicLLMService(
        api_key="dummy",
        model=config.model,
        params=params,
        client=vertex_client,
    )
