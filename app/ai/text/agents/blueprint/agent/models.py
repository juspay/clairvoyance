"""LLM model registry + factory for Blueprint.

Vertex-first. Claude models run via ``ChatAnthropicVertex`` using
Google service-account credentials — no ``ANTHROPIC_API_KEY``
required. A small wrapper around the inner model handles two gotchas
that otherwise silently break extraction and structured output:

1. **System-only message lists** — ``ChatAnthropicVertex`` extracts
   ``SystemMessage``s into the Anthropic ``system`` param, leaving
   ``messages: []`` which Vertex rejects with HTTP 400. We inject a
   stub ``HumanMessage`` when the list has no non-system messages.
2. **Thinking + tool_choice conflict** — extended thinking is
   incompatible with the tool_choice that ``with_structured_output``
   sets. We strip ``thinking`` from ``model_kwargs`` on the inner
   model before delegating to ``with_structured_output``.

Env vars (mirrored from the pre-rewrite registry so existing prod
deployments keep working):

* ``BLUE_PRINT_GOOGLE_CREDENTIALS_JSON`` — service-account JSON
* ``BLUEPRINT_VERTEX_PROJECT`` — GCP project (default: ``breeze-automatic-prod``)
* ``BLUEPRINT_VERTEX_LOCATION`` — GCP region (default: ``asia-southeast1``)
* ``BLUEPRINT_VERTEX_CLAUDE_MODEL`` — override default Sonnet model id
"""

from __future__ import annotations

import contextvars
import copy
import json
import os
from functools import lru_cache
from typing import Any, AsyncIterator

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_google_vertexai.model_garden import ChatAnthropicVertex

from app.core.logger import logger

DEFAULT_MODEL = "sonnet"

selected_model: contextvars.ContextVar[str] = contextvars.ContextVar(
    "blueprint_selected_model",
    default=DEFAULT_MODEL,
)


# ---------------------------------------------------------------------------
# Model registry — logical name → {provider, model_id, display_name}
# ---------------------------------------------------------------------------

# Always Sonnet — no model selection. Simple and good enough.
_MODEL_ID = os.environ.get("BLUEPRINT_VERTEX_CLAUDE_MODEL", "claude-sonnet-4-5")


def get_available_models() -> list[dict[str, str]]:
    """Return the single model available."""
    return [{"name": "sonnet", "display_name": "Claude Sonnet (Vertex)"}]


# ---------------------------------------------------------------------------
# Vertex credential loading (cached + pre-warmed)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_vertex_credentials() -> Any | None:
    """Parse Vertex AI service-account credentials and pre-warm the OAuth token.

    Pre-warming the token on first call saves ~300-500ms on the first LLM
    request. Returns ``None`` when no credentials env var is set —
    callers should treat that as "Vertex not configured".
    """
    creds_json = os.environ.get("BLUE_PRINT_GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json:
        return None
    try:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(Request())
        logger.info("Blueprint Vertex credentials loaded and OAuth token pre-warmed")
        return creds
    except Exception as exc:
        logger.warning(f"Blueprint: failed to parse Vertex credentials: {exc}")
        return None


def vertex_configured() -> bool:
    """True iff Blueprint can build a Vertex-backed LLM right now."""
    return _get_vertex_credentials() is not None


# ---------------------------------------------------------------------------
# Wrapper that fixes the two gotchas
# ---------------------------------------------------------------------------


_STUB_HUMAN = HumanMessage(content="Execute the task described above.")


def _ensure_human_message(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Append a stub ``HumanMessage`` when every message is a ``SystemMessage``."""
    if any(not isinstance(m, SystemMessage) for m in messages):
        return messages
    return list(messages) + [_STUB_HUMAN]


def _fix_input(input_val: Any) -> Any:
    """Normalise Runnable input — fix system-only message lists, passthrough dicts."""
    if isinstance(input_val, list):
        return _ensure_human_message(input_val)
    return input_val


class _VertexClaudeWrapper(BaseChatModel):
    """Thin ``BaseChatModel`` over ``ChatAnthropicVertex``.

    Two fixes baked in:
      * System-only message list → stub ``HumanMessage`` appended to avoid
        the ``messages: []`` HTTP 400 Vertex raises.
      * ``thinking`` is stripped before ``with_structured_output`` because
        Vertex rejects the combination of thinking + forced ``tool_choice``.
    """

    inner: Any

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "blueprint-vertex-claude-wrapper"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self.inner._generate(
            _ensure_human_message(messages),
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await self.inner._agenerate(
            _ensure_human_message(messages),
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        async for chunk in self.inner._astream(
            _ensure_human_message(messages),
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            yield chunk

    def _inner_without_thinking(self) -> Any:
        """Copy of ``self.inner`` with ``thinking`` stripped from ``model_kwargs``."""
        inner = self.inner
        if "thinking" not in getattr(inner, "model_kwargs", {}):
            return inner
        copied = copy.copy(inner)
        copied.model_kwargs = {
            k: v for k, v in copied.model_kwargs.items() if k != "thinking"
        }
        return copied

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:
        """Structured output with thinking auto-disabled + input-fix shim."""
        structured = self._inner_without_thinking().with_structured_output(
            schema, **kwargs
        )
        return RunnableLambda(_fix_input) | structured


# ---------------------------------------------------------------------------
# LLM factory (cached)
# ---------------------------------------------------------------------------

# Cached by ``(name, thinking_type, thinking_budget)`` so we reuse the inner
# httpx connection pool + OAuth token between calls.
_llm_cache: dict[str, BaseChatModel] = {}


def get_llm(name: str = DEFAULT_MODEL, **kwargs: Any) -> BaseChatModel:
    """Return a cached ``BaseChatModel`` (always Sonnet via Vertex).

    ``name`` is accepted for API compatibility but ignored — we always
    use the single configured Sonnet model.
    """
    model_id = _MODEL_ID

    thinking = kwargs.pop("thinking", {"type": "enabled", "budget_tokens": 5000})

    # Anthropic extended-thinking mandates ``temperature=1``; Vertex returns
    # HTTP 400 otherwise. If the caller supplied a non-1 temperature, they
    # want temperature control — disable thinking rather than override their
    # intent. If no temperature was passed and thinking is enabled, force it
    # to 1 so the call doesn't trip the Vertex guardrail.
    caller_temp = kwargs.get("temperature")
    if thinking.get("type") == "enabled":
        if caller_temp is not None and caller_temp != 1:
            thinking = {"type": "disabled"}
        elif caller_temp is None:
            kwargs["temperature"] = 1

    temp_key = kwargs.get("temperature", "default")
    cache_key = (
        f"{name}:{thinking['type']}:{thinking.get('budget_tokens', 0)}:{temp_key}"
    )
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    creds = _get_vertex_credentials()
    if creds is None:
        raise RuntimeError(
            "Blueprint Vertex credentials missing — set "
            "BLUE_PRINT_GOOGLE_CREDENTIALS_JSON."
        )

    model_kwargs: dict[str, Any] = {}
    if thinking.get("type") == "enabled":
        model_kwargs["thinking"] = thinking

    inner = ChatAnthropicVertex(
        model=model_id,
        project=os.environ.get("BLUEPRINT_VERTEX_PROJECT", "breeze-automatic-prod"),
        location=os.environ.get("BLUEPRINT_VERTEX_LOCATION", "asia-southeast1"),
        credentials=creds,
        max_tokens=16384,
        streaming=True,
        model_kwargs=model_kwargs,
        **kwargs,
    )
    llm = _VertexClaudeWrapper(inner=inner)
    _llm_cache[cache_key] = llm
    logger.info(f"Blueprint: built Vertex LLM {name} ({model_id})")
    return llm


__all__ = [
    "DEFAULT_MODEL",
    "get_available_models",
    "get_llm",
    "selected_model",
    "vertex_configured",
]
