"""Google Gemini Live (voice-to-voice) realtime LLM builder.

Wraps pipecat's ``GeminiLiveLLMService`` (Gemini Developer API) with the glue
to construct it from BB's ``LLMConfiguration``.
The service handles audio in/out, transcription, turn detection, and function
calling natively; no separate STT or TTS service is wired into the pipeline
when this is in use.

The model id on ``RealtimeConfig.model`` selects the model. Production uses
``gemini-3.1-flash-live-preview`` (Developer API; server-side VAD handles turn
detection), which is also the default when a template omits the model field.

As with the other realtime providers, ``system_instruction`` and ``tools`` are
NOT set here — pipecat-flows' FlowManager pushes them via frames after the
pipeline starts, and the Gemini Live service reconnects to register them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from google.genai.types import ThinkingConfig
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    GeminiVADParams,
)

from app.core.logger import logger

__all__ = [
    "BuddyGeminiLiveLLMService",
    "GeminiRealtimeConfig",
    "build_gemini_realtime_llm",
    "has_realtime_llm",
]

# Default model — Gemini 3.1 Flash Live (Developer API). This is the only
# realtime model used in production; templates may override via realtime.model.
DEFAULT_GEMINI_REALTIME_MODEL = "gemini-3.1-flash-live-preview"

# Default voice — a neutral Gemini prebuilt voice. Templates can override via
# ``realtime.voice``.
DEFAULT_GEMINI_REALTIME_VOICE = "Kore"


def _parse_async_tool_payload(content: Any) -> Optional[dict]:
    """Parse an async-tool JSON payload from a tool/developer message.

    The assistant aggregator writes both the "running" placeholder (``role:
    tool``) and the final result (``role: developer``, ``status=finished``)
    as JSON strings shaped ``{"type": "async_tool", ...}``. Returns the dict
    for those, None for anything else (sync results, plain text).
    """
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("type") == "async_tool":
        return payload
    return None


class BuddyGeminiLiveLLMService(GeminiLiveLLMService):
    """GeminiLiveLLMService that actually delivers async-tool results.

    pipecat 1.1.0's Gemini Live service never tells a live session that an
    async function call FINISHED, so the model waits forever for a
    "developer message" that never arrives (observed 2026-08-18: 19s of
    post-closing dead air; finish_call only fired after the customer spoke):

    - The assistant aggregator reports async results as a ``developer``
      message (``{"type": "async_tool", "status": "finished", "result": ...}``)
      and leaves the ``tool`` message as a "running" placeholder.
    - The Gemini adapter maps ``developer`` to plain user text, and the
      service's incremental ``_process_completed_function_calls`` only sends
      ``functionResponse`` parts — its ``value != "IN_PROGRESS"`` filter was
      written for the older placeholder format, so the RUNNING payload slips
      through and the real result is never sent.
    - ``_tool_result`` also lacks the Gemini 3.x realtime-input nudge that
      ``_create_single_response`` applies ("Gemini 3.x won't run inference
      without a realtime input").

    This override (a) suppresses the running placeholders, (b) sends each
    finished developer-message result through the service's own
    ``_tool_result`` plus the 3.x nudge, and (c) still calls super() so
    synchronous results keep their original path.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Async-tool ids whose FINAL (status=finished) result was sent (or
        # bookkept at initial context). Distinct from _completed_tool_calls,
        # which also accumulates ids whose running placeholder was merely
        # suppressed — those still owe a final result later.
        self._async_final_results_sent: set = set()

    async def _process_completed_function_calls(self, send_new_results: bool) -> None:
        if self._context is None:
            await super()._process_completed_function_calls(send_new_results)
            return
        messages = self._context.messages or []

        # tool_call_id -> function name, from assistant tool_calls messages.
        id_to_name: dict = {}
        for message in messages:
            for tool_call in message.get("tool_calls") or []:
                name = (tool_call.get("function") or {}).get("name")
                if name and tool_call.get("id"):
                    id_to_name[tool_call["id"]] = name

        # 1) Finished async results (developer messages) — the payload the
        #    base class never sends.
        for message in messages:
            if message.get("role") != "developer":
                continue
            payload = _parse_async_tool_payload(message.get("content"))
            if payload is None or payload.get("status") != "finished":
                continue
            tool_call_id = payload.get("tool_call_id")
            if not tool_call_id or tool_call_id in self._async_final_results_sent:
                continue
            if send_new_results:
                result = payload.get("result")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError:
                        result = {"value": result}
                if not isinstance(result, dict):
                    result = {"value": "COMPLETED"}
                await self._tool_result(
                    tool_call_id,
                    id_to_name.get(tool_call_id, "tool_call_result"),
                    result,
                )
                # Gemini 3.x won't run inference on a tool response without
                # a realtime input (same nudge as _create_single_response).
                if self._is_gemini_3 and self._session:
                    await self._session.send_realtime_input(text=" ")
            self._async_final_results_sent.add(tool_call_id)
            self._completed_tool_calls.add(tool_call_id)

        # 2) Running placeholders — mark completed WITHOUT sending, so the
        #    base class doesn't ship a "task started" payload as the result.
        for message in messages:
            if message.get("role") != "tool":
                continue
            payload = _parse_async_tool_payload(message.get("content"))
            if payload is None:
                continue
            tool_call_id = payload.get("tool_call_id") or message.get("tool_call_id")
            if tool_call_id:
                self._completed_tool_calls.add(tool_call_id)

        # 3) Synchronous results keep the base-class path untouched.
        await super()._process_completed_function_calls(send_new_results)


@dataclass
class GeminiRealtimeConfig:
    """Configuration for the Gemini Live (speech-to-speech) Developer-API service."""

    api_key: str
    model: str = DEFAULT_GEMINI_REALTIME_MODEL
    voice: Optional[str] = None
    language: Optional[str] = None
    thinking_level: Optional[str] = None
    silence_duration_ms: Optional[int] = None
    function_call_timeout_secs: float = 10.0
    endframe_deferral_timeout_secs: float = 1.0


def build_gemini_realtime_llm(
    config: GeminiRealtimeConfig,
) -> BuddyGeminiLiveLLMService:
    """Create a ``GeminiLiveLLMService`` (Developer API) instance."""
    voice = config.voice or DEFAULT_GEMINI_REALTIME_VOICE
    logger.info(
        f"Building Gemini Live realtime LLM: model={config.model}, voice={voice}, "
        f"language={config.language or 'auto'}, "
        f"thinking_level={config.thinking_level or 'default'}, "
        f"silence_duration_ms={config.silence_duration_ms or 'default'}"
    )
    # All optional params are passed conditionally: unset → pipecat/Gemini
    # defaults apply. Settings.language's type disallows None; thinking and vad
    # default to the model's behavior when omitted.
    settings_kwargs: dict = {"model": config.model, "voice": voice}
    if config.language:
        settings_kwargs["language"] = config.language
    if config.thinking_level:
        settings_kwargs["thinking"] = ThinkingConfig(
            thinking_level=config.thinking_level
        )
    if config.silence_duration_ms is not None:
        settings_kwargs["vad"] = GeminiVADParams(
            silence_duration_ms=config.silence_duration_ms
        )
    service = BuddyGeminiLiveLLMService(
        api_key=config.api_key,
        settings=BuddyGeminiLiveLLMService.Settings(**settings_kwargs),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
    # Cap pipecat's EndFrame deferral so finish_call/end_conversation actually
    # hang up the line (the default 30s leaves it open until the customer
    # hangs up). pipecat exposes no constructor param for this — it's a class
    # constant — so override it per-instance. Template-configurable via
    # realtime.endframe_deferral_timeout_secs (default 1.0s; 0 = immediate).
    service._END_FRAME_DEFERRAL_TIMEOUT_SECS = config.endframe_deferral_timeout_secs
    return service


def has_realtime_llm(llm_config: Any) -> bool:
    """True when the template uses a speech-to-speech realtime LLM.

    Realtime LLMs (e.g. Gemini Live) run STT/TTS/turn-detection server-side,
    so BB does not receive a reliable user-turn-start event at speech onset.
    The agent-owned post-greeting timer can therefore race a short reply and
    force a realtime reconnect that drops in-flight audio. The realtime LLM
    listens continuously and needs no separate wall-clock timer, so this extra
    timer is skipped.

    Args:
        llm_config: The ``LLMConfiguration`` (``configurations.llm_configurations``);
            ``None`` when unset.
    """
    return bool(llm_config and llm_config.realtime is not None)
