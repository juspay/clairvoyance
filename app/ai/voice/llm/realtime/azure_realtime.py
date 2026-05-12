"""Azure OpenAI Realtime LLM config and builder.

Wraps pipecat's ``AzureRealtimeLLMService`` (which subclasses
``OpenAIRealtimeLLMService``) so Azure-hosted OpenAI Realtime deployments
can be used with the same direct-mode + S2S wiring as the OpenAI path.

Compatibility note
------------------
Pipecat v1.1.0 targets the OpenAI Realtime v1 schema (api-version ≥ 2025).
Azure deployments on ``api-version=2024-10-01-preview`` use an older flat
schema with different field names.  ``AzureRealtimeLegacyLLMService`` is a
thin shim that patches wire payloads in both directions to stay compatible
with the older API version.

Known renames between Pipecat v1.1.0 and 2024-10-01-preview:
  Outbound (client → server) — Pipecat name → Azure 2024-10-01-preview name:
    response.output_modalities  → response.modalities

  Inbound (server → client) — Azure 2024-10-01-preview name → Pipecat name:
    conversation.item.created       → conversation.item.added
    response.audio.delta            → response.output_audio.delta
    response.audio.done             → response.output_audio.done
    response.audio_transcript.delta → response.output_audio_transcript.delta
    response.audio_transcript.done  → response.output_audio_transcript.done
    response.text.delta             → response.output_text.delta
    response.text.done              → response.output_text.done
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from pipecat.services.azure.realtime.llm import AzureRealtimeLLMService
from pipecat.services.openai.realtime.events import (
    ResponseCreateEvent,
    SessionProperties,
)
from websockets.asyncio.client import connect as websocket_connect

from app.core.logger import logger

__all__ = [
    "AzureRealtimeConfig",
    "AzureRealtimeLegacyLLMService",
    "build_azure_realtime_llm",
]


class _TranslatingWebSocket:
    """Async-iterable wrapper that rewrites Azure legacy server event types.

    Azure ``api-version=2024-10-01-preview`` uses older event type names that
    Pipecat v1.1.0 does not recognise.  This wrapper intercepts each inbound
    raw JSON message and renames the ``type`` field before Pipecat's
    ``parse_server_event`` ever sees it.  All other websocket operations
    (``send``, ``close``, …) are delegated transparently to the real socket.
    """

    # Azure 2024-10-01-preview drops the `output_` prefix from response
    # sub-events that OpenAI Realtime v1 (Pipecat v1.1.0) later added.
    _RENAMES: dict[str, str] = {
        "conversation.item.created": "conversation.item.added",
        "response.audio.delta": "response.output_audio.delta",
        "response.audio.done": "response.output_audio.done",
        "response.audio_transcript.delta": "response.output_audio_transcript.delta",
        "response.audio_transcript.done": "response.output_audio_transcript.done",
        "response.text.delta": "response.output_text.delta",
        "response.text.done": "response.output_text.done",
    }

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    def __aiter__(self) -> Any:
        return self._translate()

    async def _translate(self):
        # Use `async for` on the real websocket — the correct API for
        # ClientConnection. Calling __anext__ directly is not supported.
        async for msg in self._ws:
            try:
                data = json.loads(msg)
                event_type = data.get("type")
                if event_type in self._RENAMES:
                    logger.debug(
                        f"[azure-compat] translating server event "
                        f"{event_type!r} → {self._RENAMES[event_type]!r}"
                    )
                    data["type"] = self._RENAMES[event_type]
                    msg = json.dumps(data)
            except (json.JSONDecodeError, TypeError):
                pass
            yield msg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)


class AzureRealtimeLegacyLLMService(AzureRealtimeLLMService):
    """Compatibility shim for Azure OpenAI Realtime ``api-version=2024-10-01-preview``.

    Intercepts every outgoing client event and rewrites field names that were
    renamed between the legacy Azure schema and the Pipecat v1.1.0 / OpenAI
    Realtime v1 schema, so that both can coexist without touching Pipecat
    internals.
    """

    async def send_client_event(self, event: Any) -> None:
        dump = event.model_dump(exclude_none=True)

        # response.create: Pipecat v1.1.0 sends `output_modalities`; the
        # 2024-10-01-preview API expects the older name `modalities`.
        if isinstance(event, ResponseCreateEvent):
            response = dump.get("response", {})
            if "output_modalities" in response:
                modalities = response.pop("output_modalities")
                # Azure 2024-10-01-preview rejects ["audio"] alone; only
                # ["text"] and ["audio", "text"] are valid.
                if modalities == ["audio"]:
                    modalities = ["audio", "text"]
                response["modalities"] = modalities

        await self._ws_send(dump)

    async def _connect(self) -> None:
        """Override to install the inbound event-type translation wrapper.

        Mirrors ``AzureRealtimeLLMService._connect`` exactly, except the raw
        websocket is wrapped in ``_TranslatingWebSocket`` before being assigned
        to ``self._websocket``.  This ensures every inbound message has its
        event type renamed before ``parse_server_event`` sees it.
        """
        try:
            if self._websocket:
                return
            logger.info(f"Connecting to {self.base_url} (legacy compat mode)")
            raw_ws = await websocket_connect(
                uri=self.base_url,
                additional_headers={"api-key": self.api_key},
            )
            self._websocket = _TranslatingWebSocket(raw_ws)
            self._receive_task = self.create_task(self._receive_task_handler())
        except Exception as e:
            await self.push_error(error_msg=f"initialization error: {e}", exception=e)
            self._websocket = None


@dataclass
class AzureRealtimeConfig:
    """Configuration for the Azure-hosted OpenAI Realtime service.

    ``base_url`` is the full Azure WebSocket endpoint URL including the
    ``api-version`` query parameter and the ``deployment`` name; the
    deployment effectively selects the underlying realtime model, so there
    is no separate ``model`` field.
    """

    api_key: str
    base_url: str
    voice: Optional[str] = None
    function_call_timeout_secs: float = 10.0


def build_azure_realtime_llm(
    config: AzureRealtimeConfig,
) -> AzureRealtimeLegacyLLMService:
    """Create an ``AzureRealtimeLegacyLLMService`` instance.

    Uses ``AzureRealtimeLegacyLLMService`` (a thin shim over
    ``AzureRealtimeLLMService``) to patch wire payloads for compatibility with
    ``api-version=2024-10-01-preview``.

    Session audio config (voice, turn detection, noise reduction, transcription)
    is intentionally omitted — the 2024-10-01-preview API uses a flat session
    schema that is incompatible with the nested ``session.audio`` structure
    Pipecat v1.1.0 generates.  Azure deployment defaults apply instead.
    """
    # Do not pass session.type or session.audio — both are incompatible with
    # the 2024-10-01-preview flat session schema.
    session_properties = SessionProperties(
        type=None,
        audio=None,
    )

    logger.info(
        f"Building Azure Realtime LLM service (legacy compat) with "
        f"base_url={config.base_url}, voice={config.voice or 'deployment default'}"
    )
    if config.voice:
        logger.warning(
            f"config.voice={config.voice!r} is set but will not be applied — "
            "session audio config is incompatible with the 2024-10-01-preview schema; "
            "the Azure deployment default voice applies instead."
        )

    return AzureRealtimeLegacyLLMService(
        api_key=config.api_key,
        base_url=config.base_url,
        settings=AzureRealtimeLegacyLLMService.Settings(
            session_properties=session_properties,
        ),
        function_call_timeout_secs=config.function_call_timeout_secs,
    )
