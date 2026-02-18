"""Custom Soniox STT service with endpoint detection timeout for telephony.

This module provides a custom Soniox STT implementation that extends pipecat's
SonioxSTTService with a configurable endpoint detection timeout. Instead of
immediately finalizing transcription when VAD detects silence, we wait for a
configurable delay (default 500ms) before sending the finalize message.

This prevents mid-sentence cutoffs common in telephony (8kHz mulaw audio) where
brief pauses between words trigger premature finalization.

Additionally, this service enables Soniox's native endpoint detection alongside
VAD-driven finalization (hybrid mode), allowing Soniox to also detect natural
endpoints for longer pauses.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator, List, Optional

from loguru import logger
from pydantic import BaseModel

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.protocol import State
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error("In order to use Soniox, you need to `pip install pipecat-ai[soniox]`.")
    raise Exception(f"Missing module: {e}")


KEEPALIVE_MESSAGE = '{"type": "keepalive"}'
FINALIZE_MESSAGE = '{"type": "finalize"}'
END_TOKEN = "<end>"
FINALIZED_TOKEN = "<fin>"

# Default endpoint detection timeout in seconds.
# After VAD detects silence, wait this long before finalizing.
# If user resumes speaking within this window, the finalize is cancelled.
DEFAULT_ENDPOINT_TIMEOUT = 0.5


class CustomSonioxInputParams(BaseModel):
    """Real-time transcription settings for custom Soniox STT.

    See Soniox WebSocket API documentation for more details:
    https://soniox.com/docs/speech-to-text/api-reference/websocket-api
    """

    model: str = "stt-rt-v3"
    audio_format: Optional[str] = "pcm_s16le"
    num_channels: Optional[int] = 1
    language_hints: Optional[List[Language]] = None
    language_hints_strict: Optional[bool] = None
    context: Optional[dict | str] = None
    enable_speaker_diarization: Optional[bool] = False
    enable_language_identification: Optional[bool] = False
    client_reference_id: Optional[str] = None
    enable_non_final_tokens: Optional[bool] = True
    max_non_final_tokens_duration_ms: Optional[int] = None


def _is_end_token(token: dict) -> bool:
    """Determine if a token is an end/finalized token."""
    return token["text"] == END_TOKEN or token["text"] == FINALIZED_TOKEN


def _language_to_soniox(language: Language) -> str:
    """Convert pipecat Language to Soniox language code."""
    lang_str = str(language.value).lower()
    if "-" in lang_str:
        return lang_str.split("-")[0]
    return lang_str


def _prepare_language_hints(
    language_hints: Optional[List[Language]],
) -> Optional[List[str]]:
    if language_hints is None:
        return None
    prepared = [_language_to_soniox(lang) for lang in language_hints]
    return list(set(prepared))


class CustomSonioxSTTService(WebsocketSTTService):
    """Custom Soniox STT with endpoint detection timeout for telephony.

    Key differences from pipecat's SonioxSTTService:

    1. **Endpoint detection timeout**: On VAD stop, waits `endpoint_timeout`
       seconds (default 500ms) before sending FINALIZE_MESSAGE. If VAD start
       arrives within that window, the finalize is cancelled. This prevents
       mid-sentence cutoffs from brief pauses.

    2. **Hybrid endpoint detection**: Enables Soniox's native endpoint detection
       alongside VAD-driven finalization. Soniox can detect natural endpoints
       for longer pauses even without VAD triggering.

    3. **Optimized for 8kHz telephony**: Works correctly with low-amplitude
       telephony audio where Silero VAD confidence is lower.
    """

    def __init__(
        self,
        *,
        api_key: str,
        url: str = "wss://stt-rt.soniox.com/transcribe-websocket",
        sample_rate: Optional[int] = None,
        params: Optional[CustomSonioxInputParams] = None,
        endpoint_timeout: float = DEFAULT_ENDPOINT_TIMEOUT,
        enable_soniox_endpoint_detection: bool = True,
        **kwargs,
    ):
        """Initialize the custom Soniox STT service.

        Args:
            api_key: Soniox API key.
            url: Soniox WebSocket API URL.
            sample_rate: Audio sample rate.
            params: Transcription configuration parameters.
            endpoint_timeout: Seconds to wait after VAD stop before finalizing.
                If user resumes speaking within this window, finalize is cancelled.
                Set to 0 to finalize immediately (same as pipecat default).
                Defaults to 0.5 seconds.
            enable_soniox_endpoint_detection: If True, also enables Soniox's
                native endpoint detection (hybrid mode). Defaults to True.
            **kwargs: Additional arguments passed to WebsocketSTTService.
        """
        super().__init__(
            sample_rate=sample_rate,
            keepalive_timeout=1,
            keepalive_interval=5,
            **kwargs,
        )
        params = params or CustomSonioxInputParams()

        self._api_key = api_key
        self._url = url
        self.set_model_name(params.model)
        self._params = params
        self._endpoint_timeout = endpoint_timeout
        self._enable_soniox_endpoint_detection = enable_soniox_endpoint_detection

        self._final_transcription_buffer: list = []
        self._last_tokens_received: Optional[float] = None

        self._receive_task = None
        self._endpoint_timeout_task: Optional[asyncio.Task] = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._cancel_endpoint_timeout()
        await self._send_stop_recording()
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._cancel_endpoint_timeout()
        await self._disconnect()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Send audio data to Soniox."""
        await self.start_processing_metrics()
        if self._websocket and self._websocket.state is State.OPEN:
            await self._websocket.send(audio)
        await self.stop_processing_metrics()
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames with endpoint detection timeout logic.

        On VAD stop: start a timer instead of immediately finalizing.
        On VAD start: cancel any pending finalize timer.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            # User resumed speaking — cancel any pending finalize
            if self._endpoint_timeout_task:
                logger.debug(
                    "User resumed speaking, cancelling pending endpoint finalize"
                )
                await self._cancel_endpoint_timeout()

        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._endpoint_timeout <= 0:
                # No timeout configured — finalize immediately (pipecat default behavior)
                await self._send_finalize()
            else:
                # Start endpoint detection timeout
                await self._cancel_endpoint_timeout()
                self._endpoint_timeout_task = asyncio.ensure_future(
                    self._endpoint_timeout_handler()
                )
                logger.debug(
                    f"VAD stop detected, starting {self._endpoint_timeout}s endpoint timeout"
                )

    async def _endpoint_timeout_handler(self):
        """Wait for endpoint timeout, then finalize if not cancelled."""
        try:
            await asyncio.sleep(self._endpoint_timeout)
            logger.debug(
                f"Endpoint timeout ({self._endpoint_timeout}s) expired, sending finalize"
            )
            await self._send_finalize()
        except asyncio.CancelledError:
            pass
        finally:
            self._endpoint_timeout_task = None

    async def _cancel_endpoint_timeout(self):
        """Cancel any pending endpoint detection timeout."""
        if self._endpoint_timeout_task:
            self._endpoint_timeout_task.cancel()
            try:
                await self._endpoint_timeout_task
            except asyncio.CancelledError:
                pass
            self._endpoint_timeout_task = None

    async def _send_finalize(self):
        """Send finalize message to Soniox to get final tokens."""
        if self._websocket and self._websocket.state is State.OPEN:
            await self._websocket.send(FINALIZE_MESSAGE)
            logger.debug("Sent finalize message to Soniox")

    async def _send_stop_recording(self):
        """Send stop recording message to Soniox."""
        if self._websocket and self._websocket.state is State.OPEN:
            await self._websocket.send("")

    async def _connect(self):
        await self._connect_websocket()
        await super()._connect()
        if self._websocket and not self._receive_task:
            self._receive_task = self.create_task(
                self._receive_task_handler(self._report_error)
            )

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self):
        """Establish websocket connection with hybrid endpoint detection config."""
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return

            logger.debug("Connecting to Soniox STT (custom)")

            self._websocket = await websocket_connect(self._url)

            if not self._websocket:
                await self.push_error(
                    error_msg=f"Unable to connect to Soniox API at {self._url}"
                )
                raise Exception(f"Unable to connect to Soniox API at {self._url}")

            # Hybrid mode: enable Soniox's native endpoint detection alongside
            # our VAD-driven finalization with timeout.
            enable_endpoint_detection = self._enable_soniox_endpoint_detection

            context = self._params.context
            if isinstance(context, BaseModel):
                context = context.model_dump()

            config = {
                "api_key": self._api_key,
                "model": self._model_name,
                "audio_format": self._params.audio_format,
                "num_channels": self._params.num_channels or 1,
                "enable_endpoint_detection": enable_endpoint_detection,
                "sample_rate": self.sample_rate,
                "language_hints": _prepare_language_hints(self._params.language_hints),
                "language_hints_strict": self._params.language_hints_strict,
                "context": context,
                "enable_speaker_diarization": self._params.enable_speaker_diarization,
                "enable_language_identification": self._params.enable_language_identification,
                "client_reference_id": self._params.client_reference_id,
            }

            await self._websocket.send(json.dumps(config))

            await self._call_event_handler("on_connected")
            logger.debug(
                f"Connected to Soniox STT (custom) - "
                f"endpoint_timeout={self._endpoint_timeout}s, "
                f"soniox_endpoint_detection={enable_endpoint_detection}"
            )
        except Exception as e:
            await self.push_error(
                error_msg=f"Unable to connect to Soniox: {e}", exception=e
            )
            raise

    async def _disconnect_websocket(self):
        try:
            if self._websocket:
                logger.debug("Disconnecting from Soniox STT (custom)")
                await self._websocket.close()
        except Exception as e:
            await self.push_error(
                error_msg=f"Error closing websocket: {e}", exception=e
            )
        finally:
            self._websocket = None
            await self._call_event_handler("on_disconnected")

    def _get_websocket(self):
        if self._websocket:
            return self._websocket
        raise Exception("Websocket not connected")

    async def _receive_messages(self):
        """Receive and process Soniox websocket messages."""
        self._final_transcription_buffer = []

        async def send_endpoint_transcript():
            if self._final_transcription_buffer:
                text = "".join(
                    token["text"] for token in self._final_transcription_buffer
                )
                await self.push_frame(
                    TranscriptionFrame(
                        text=text,
                        user_id=self._user_id,
                        timestamp=time_now_iso8601(),
                        result=self._final_transcription_buffer,
                        finalized=True,
                    )
                )
                await self.stop_processing_metrics()
                self._final_transcription_buffer = []

        async for message in self._get_websocket():
            try:
                content = json.loads(message)
                tokens = content["tokens"]

                if tokens:
                    if len(tokens) == 1 and tokens[0]["text"] == FINALIZED_TOKEN:
                        # Ignore lone finalized token to prevent auto-finalize cycling
                        pass
                    else:
                        self._last_tokens_received = time.time()

                non_final_transcription = []

                for token in tokens:
                    if token["is_final"]:
                        if _is_end_token(token):
                            await send_endpoint_transcript()
                        else:
                            self._final_transcription_buffer.append(token)
                    else:
                        non_final_transcription.append(token)

                if self._final_transcription_buffer or non_final_transcription:
                    final_text = "".join(
                        token["text"]
                        for token in self._final_transcription_buffer
                    )
                    non_final_text = "".join(
                        token["text"] for token in non_final_transcription
                    )

                    await self.push_frame(
                        InterimTranscriptionFrame(
                            text=final_text + non_final_text,
                            user_id=self._user_id,
                            timestamp=time_now_iso8601(),
                            result=self._final_transcription_buffer
                            + non_final_transcription,
                        )
                    )

                error_code = content.get("error_code")
                error_message = content.get("error_message")
                if error_code or error_message:
                    await send_endpoint_transcript()
                    await self.push_error(
                        error_msg=f"Soniox error: {error_code} - {error_message}"
                    )

                finished = content.get("finished")
                if finished:
                    await send_endpoint_transcript()
                    logger.debug("Soniox transcription finished.")
                    return

            except json.JSONDecodeError:
                logger.warning(f"Received non-JSON message from Soniox: {message}")
            except Exception as e:
                logger.warning(f"Error processing Soniox message: {e}")

    async def _send_keepalive(self, silence: bytes):
        """Send Soniox protocol-level keepalive."""
        await self._websocket.send(KEEPALIVE_MESSAGE)
