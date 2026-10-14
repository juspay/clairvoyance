"""Google Vertex AI (Gemini) LLM config and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.google.vertex.llm import (
    GoogleVertexLLMService,
    GoogleVertexLLMSettings,
)

from app.ai.voice.llm._pools import get_genai_vertex_client
from app.core.logger import logger

__all__ = ["VertexConfig", "build_vertex_llm"]


class _PooledGoogleVertexLLMService(GoogleVertexLLMService):
    """GoogleVertexLLMService on the process-wide genai client (``_pools.py``).

    The stock constructor refreshes the OAuth token with a blocking HTTPS
    call (``pipecat/services/google/vertex/llm.py`` ``_get_credentials``)
    and builds a new client, on every chat turn. The pooled client keeps
    its token and refreshes it in a thread when it expires. Chat only —
    voice keeps the stock service.
    """

    def __init__(self, *, credentials: str, **kwargs: Any) -> None:
        # Set before super().__init__, which calls create_client.
        self._pooled_credentials_json = credentials
        super().__init__(credentials=credentials, **kwargs)

    @staticmethod
    def _get_credentials(credentials, credentials_path):
        # No per-turn refresh: create_client below uses the pooled client.
        return None

    def create_client(self):
        self._client = get_genai_vertex_client(
            credentials_json=self._pooled_credentials_json,
            project_id=self._project_id,
            location=self._location,
        )


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


def build_vertex_llm(
    config: VertexConfig, *, pooled: bool = False
) -> GoogleVertexLLMService:
    """Create a Google Vertex AI LLM service instance.

    Args:
        config: Vertex AI-specific configuration.
        pooled: When True, share the process-wide genai client (connections
            and OAuth token) across calls. Chat only; voice keeps the stock
            per-call service.

    Returns:
        Configured GoogleVertexLLMService instance.
    """
    # Pooled callers (chat) build this every turn — keep that log at DEBUG.
    _build_log = logger.debug if pooled else logger.info
    _build_log(
        f"Building Google Vertex LLM service with model={config.model}, "
        f"project_id={config.project_id}, location={config.location}, "
        f"thinking_budget={config.thinking_budget}, thinking_level={config.thinking_level}, "
        f"pooled={pooled}"
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

    cls = _PooledGoogleVertexLLMService if pooled else GoogleVertexLLMService
    return cls(
        credentials=config.credentials_json,
        project_id=config.project_id,
        location=config.location,
        model=config.model,
        settings=GoogleVertexLLMSettings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
