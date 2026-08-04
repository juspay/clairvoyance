"""Shared driver plumbing: the event contract every provider streamer
yields, and best-effort stream cleanup. Lives outside ``driver.py`` so
provider subpackages (``gemini/``) can import it without a cycle
(``driver`` imports the provider streamers for dispatch)."""

from __future__ import annotations

from typing import Any, Literal, Tuple, Union

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMSpecificMessage

from app.core.logger import logger

DriverEvent = Tuple[
    Literal["text", "tool_call", "context_message", "finish_reason"],
    Union[str, FunctionCallFromLLM, LLMSpecificMessage],
]


async def close_stream(response: Any) -> None:
    """Best-effort close for SDK stream / async-generator return values.

    - Python async generators (Gemini's ``base_async_generator``) expose
      ``aclose``; calling it raises ``GeneratorExit`` into the body and
      cascades to the wrapped SDK stream.
    - SDK ``AsyncStream`` classes (Anthropic, OpenAI) expose ``close``.
    - Swallow exceptions: cleanup runs in a ``finally`` and we'd rather
      lose the close error than mask the real one being unwound.
    """
    closer = getattr(response, "aclose", None) or getattr(response, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:  # noqa: BLE001 — cleanup must never mask errors
        logger.debug(f"llm driver: stream close failed ({type(exc).__name__}: {exc})")


__all__ = ["DriverEvent", "close_stream"]
