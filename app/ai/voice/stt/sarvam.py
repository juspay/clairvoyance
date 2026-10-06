"""Sarvam STT helpers and builder."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Optional

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import MODEL_CONFIGS, SarvamSTTService
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from sarvamai.core.events import EventType
from websockets.exceptions import ConnectionClosed

from app.ai.voice.stt.events import STT_UNAVAILABLE_EVENT
from app.core.logger import logger

__all__ = [
    "SARVAM_CONNECT_TIMEOUT_SECS",
    "SARVAM_KEEPALIVE_SECS",
    "SARVAM_RECONNECT_BACKOFF_SECS",
    "SARVAM_END_SPEECH_SLACK_SECS",
    "SARVAM_UNFINALIZED_MAX_SECS",
    "SarvamConfig",
    "SarvamSTTServiceWithInterruptionPolicy",
    "get_sarvam_language",
    "build_sarvam_stt",
]

# One entry per reconnect attempt, so its length is the per-call cap. Sarvam
# drops mid-call sockets with 1011 "VAD RESOURCE_EXHAUSTED"; a short wait
# usually gets a fresh session, a long one only leaves the caller unheard.
SARVAM_RECONNECT_BACKOFF_SECS: tuple[float, ...] = (0.5, 1.5)

# Longest a handshake may take (the websockets default is 10 s) before the
# connect counts as failed; at call start the StartFrame waits on it.
SARVAM_CONNECT_TIMEOUT_SECS = 3.0

# Seconds without audio sent before pipecat's keepalive sends silence.
SARVAM_KEEPALIVE_SECS = 5.0

# pipecat's error messages for one failed connect attempt.
_ATTEMPT_ERRORS = (
    "Failed to connect to Sarvam",
    "Sarvam API error",
    "Error closing WebSocket connection",
)

# Queued among the buffered audio where a flush-mode flush is owed, so the
# replay sends it right after the audio it follows. Empty: never sent as audio.
_FLUSH_MARK = AudioRawFrame(audio=b"", sample_rate=8000, num_channels=1)

# Audio the socket took that no transcript has answered yet is kept this long
# (newest first) and replayed after a drop: Sarvam loses the half-heard
# segment with the socket. A segment is far shorter.
SARVAM_UNFINALIZED_MAX_SECS = 15.0

# Sarvam's END_SPEECH reaches us this long after the segment's audio was
# sent; audio sent within it already belongs to the next segment.
SARVAM_END_SPEECH_SLACK_SECS = 0.2

# A segment boundary among the unanswered audio (Sarvam's END_SPEECH).
# Never sent, never replayed.
_SEGMENT_END = AudioRawFrame(audio=b"", sample_rate=8000, num_channels=1)


def _secs(frame: AudioRawFrame) -> float:
    if not frame.audio or not frame.sample_rate:
        return 0.0
    return frame.num_frames / frame.sample_rate


class _Unanswered:
    """What the current socket has taken that no transcript has answered.

    Entries are audio frames, flush marks (flush mode) and segment ends
    (Sarvam's END_SPEECH). A transcript answers the oldest segment: entries
    up to and including the first flush mark or segment end.
    """

    def __init__(self) -> None:
        self._items: deque[tuple[AudioRawFrame, FrameDirection, float]] = deque()
        self._secs = 0.0

    def add(self, frame: AudioRawFrame, direction: FrameDirection) -> None:
        self._items.append((frame, direction, time.monotonic()))
        self._secs += _secs(frame)
        while self._secs > SARVAM_UNFINALIZED_MAX_SECS and self._items:
            self._secs -= _secs(self._items.popleft()[0])

    def end_segment(self) -> None:
        """Sarvam ended a segment: it is what was sent up to the slack ago."""
        cutoff = time.monotonic() - SARVAM_END_SPEECH_SLACK_SECS
        index = len(self._items)
        while index > 0 and self._items[index - 1][2] > cutoff:
            index -= 1
        self._items.insert(index, (_SEGMENT_END, FrameDirection.DOWNSTREAM, cutoff))

    def answer(self) -> None:
        """A transcript arrived: drop the segment it answers."""
        while self._items:
            frame = self._items.popleft()[0]
            self._secs -= _secs(frame)
            if frame is _SEGMENT_END or frame is _FLUSH_MARK:
                return
        self._secs = 0.0  # no boundary: the transcript answered all of it

    def take(self) -> list[tuple[AudioRawFrame, FrameDirection]]:
        """Everything still unanswered, oldest first, and forget it."""
        taken = [(f, d) for f, d, _ in self._items if f is not _SEGMENT_END]
        self._items.clear()
        self._secs = 0.0
        return taken


@dataclass
class SarvamConfig:
    """Configuration for Sarvam STT.

    Which of ``language`` / ``prompt`` reaches Sarvam is decided by what the
    model accepts (pipecat's ``MODEL_CONFIGS``), not by its family name:
    saaras:v3 takes a language and rejects a prompt, saaras:v2.5 the reverse,
    saarika takes a language. An unset language means Sarvam auto-detects.
    """

    api_key: str
    model: str
    sample_rate: int
    language_code: Optional[str] = None
    prompt: Optional[str] = None
    vad_signals: Optional[bool] = None
    high_vad_sensitivity: Optional[bool] = None
    self_interrupt: bool = True


def _close_details(error: Optional[BaseException], client: Any) -> tuple[Any, str]:
    """The close code and reason of a dead Sarvam socket, best effort."""
    if isinstance(error, ConnectionClosed):
        frame = error.rcvd or error.sent
        if frame is not None:
            return frame.code, frame.reason
    websocket = getattr(client, "_websocket", None)
    return (
        getattr(websocket, "close_code", None),
        getattr(websocket, "close_reason", None) or "",
    )


class SarvamSTTServiceWithInterruptionPolicy(SarvamSTTService):
    """pipecat's SarvamSTTService, with its own barge-in made optional and a
    reconnect on a dropped socket.

    Barge-in: Sarvam streams no partial words, only a START_SPEECH signal and
    the finished text of each segment. On START_SPEECH pipecat interrupts the
    bot itself, on the first sound, so a template's
    ``interruption.min_words`` ("haan" must not stop the bot) never gets a
    say. With ``self_interrupt=False`` the signal is still broadcast, but the
    interruption is left to the pipeline's rule, which then judges the
    finished segment when the caller pauses.

    Reconnect: pipecat's service is not a WebsocketService. When Sarvam
    closes the socket mid-call nothing notices, and every 20 ms audio frame
    is sent into the dead socket with an error. Here a send that hits a
    closed socket, or the listener ending on its own, stops the sends and
    reconnects through ``STTService._reconnect``. Caller audio from the drop
    until the new socket is up goes to one buffer of ours and is replayed in
    order, together with the audio the old socket took but never answered
    with a transcript, so no caller speech is lost; at most
    ``len(SARVAM_RECONNECT_BACKOFF_SECS)`` times per call. After that
    ``STT_UNAVAILABLE_EVENT`` fires once.
    """

    # Duck-typed by the agent: this service fires STT_UNAVAILABLE_EVENT.
    emits_stt_unavailable = True

    def __init__(self, *, self_interrupt: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._self_interrupt = self_interrupt
        # Connection state. ``_closing`` marks our own disconnects, so a
        # listener ending under them is not mistaken for a drop.
        self._closing = False
        self._shutting_down = False
        self._connection_lost = False
        self._gave_up = False
        self._reconnect_attempts = 0
        self._recovery_task: Optional[asyncio.Task] = None
        # The one replay buffer: caller audio (and flush marks) from the
        # drop until the new socket has taken it, oldest first. pipecat's
        # own reconnect buffer is never used: every frame is caught here
        # first, so its ordering rules never come into play.
        self._replay_buffer: list[tuple[AudioRawFrame, FrameDirection]] = []
        # Set by run_stt when this frame's send found the socket closed.
        self._send_failed = False
        # What the current socket has taken that no transcript answered:
        # lost with the socket if it drops, so replayed first then.
        self._unanswered = _Unanswered()
        # Frames that went into a dying socket, queued in order at the head
        # of _replay_buffer (until the attempt puts _unanswered before them).
        self._head_count = 0
        self._last_socket_error: Optional[BaseException] = None
        self._last_close = "never connected"
        # Why the last quieted reconnect attempt failed, for the give-up.
        self._last_attempt_error: Optional[str] = None
        # The last Sarvam session id seen, named in the drop warning.
        self._last_request_id: Optional[str] = None
        # Sarvam's own VAD said START_SPEECH and no END_SPEECH yet.
        self._sarvam_speech_open = False
        self._register_event_handler(STT_UNAVAILABLE_EVENT)

    def set_self_interrupt(self, enabled: bool) -> None:
        """Follow a node-level interruption override mid-call."""
        self._self_interrupt = enabled

    async def broadcast_interruption(self) -> None:
        # pipecat 1.1.0's Sarvam service calls this only on START_SPEECH.
        if self._self_interrupt:
            await super().broadcast_interruption()

    async def _handle_message(self, message: Any) -> None:
        """Note Sarvam's speech state and session id, then let pipecat act."""
        data = getattr(message, "data", None)
        kind = getattr(message, "type", None)
        if kind == "events":
            signal = getattr(data, "signal_type", None)
            if signal == "START_SPEECH":
                self._sarvam_speech_open = True
            elif signal == "END_SPEECH":
                self._sarvam_speech_open = False
                self._unanswered.end_segment()
        elif kind == "data":
            self._unanswered.answer()
            self._last_request_id = (
                getattr(data, "request_id", None) or self._last_request_id
            )
        elif kind == "error":
            # pipecat ignores these; they often name why a close follows.
            logger.warning(
                f"Sarvam STT error message: {getattr(data, 'error', None)!r} "
                f"code={getattr(data, 'code', None)} "
                f"request_id={self._last_request_id}"
            )
        await super()._handle_message(message)

    # ------------------------------------------------------------ connection

    async def _connect(self) -> None:
        parent_connect = super()._connect

        async def connect_and_attach() -> None:
            await parent_connect()
            # Same task, no await since pipecat created the receive task: the
            # listener cannot run (and fail unheard) before this is attached.
            if self._socket_client is not None:
                self._attach(self._socket_client)

        try:
            await asyncio.wait_for(connect_and_attach(), SARVAM_CONNECT_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            await self._disconnect()  # whatever the cut-off connect left
            # The cancel skipped pipecat's own error report; make it here,
            # worded as pipecat's so a retry's is quieted like the others.
            await self.push_error(
                error_msg=(
                    f"Failed to connect to Sarvam: handshake timed out after "
                    f"{SARVAM_CONNECT_TIMEOUT_SECS}s"
                )
            )
        if self._socket_client is None:
            # Inside a recovery the loop retries; anywhere else (call start,
            # a settings reconnect) start one, or the call runs with an STT
            # that never connected.
            if self._recovery_task is None and not self._shutting_down:
                logger.warning(
                    f"Sarvam STT could not connect "
                    f"(last request_id={self._last_request_id})"
                )
                self._connection_lost = True
                self._start_recovery()
            return
        # A fresh socket has no open speech; one left open by the old socket
        # (an END_SPEECH that never came) is closed here.
        await self._close_open_speech()

    def _attach(self, client: Any) -> None:
        """Reset the per-socket state and listen for the SDK's ERROR event."""
        self._connection_lost = False
        self._last_socket_error = None
        self._last_request_id = None  # a new Sarvam session

        # The SDK listener reports a websocket failure only as an ERROR
        # event; keep it for the close code and reason.
        def _on_error(error: BaseException) -> None:
            if client is self._socket_client:
                self._last_socket_error = error

        client.on(EventType.ERROR, _on_error)

    async def _disconnect(self) -> None:
        self._closing = True
        try:
            await super()._disconnect()
        finally:
            self._closing = False

    async def push_error(
        self,
        error_msg: str,
        exception: Optional[Exception] = None,
        fatal: bool = False,
    ) -> None:
        # pipecat reports each failed reconnect attempt as a pipeline error;
        # the outage is reported once, by _give_up, if it comes. Only those
        # messages, and only while a recovery runs, are downgraded. A failed
        # first connect is still reported at once (the /stt stream endpoint
        # rejects a stream on an error within its startup grace).
        if (
            not fatal
            and self._recovery_task is not None
            and error_msg.startswith(_ATTEMPT_ERRORS)
        ):
            logger.warning(f"Sarvam STT connect attempt: {error_msg}")
            self._last_attempt_error = error_msg
            return
        await super().push_error(error_msg, exception=exception, fatal=fatal)

    def _is_keepalive_ready(self) -> bool:
        # No keepalive into a socket already known to be dead.
        return super()._is_keepalive_ready() and not self._connection_lost

    async def _receive_task_handler(self) -> None:
        client = self._socket_client
        await super()._receive_task_handler()
        # Reached only when the listener ended by itself: our own disconnect
        # cancels this task, which raises past this line.
        await self._on_connection_lost("listener ended", client=client)

    async def process_audio_frame(
        self, frame: AudioRawFrame, direction: FrameDirection
    ) -> None:
        if self._muted:
            # As pipecat: muted audio is dropped. Checked here, when the
            # frame arrives, so audio captured unmuted is replayed whatever
            # the mute is by then.
            return
        if self._recovery_task is not None and (
            self._connection_lost or self._reconnecting
        ):
            self._replay_buffer.append((frame, direction))
            return
        client = self._socket_client
        self._send_failed = False
        await super().process_audio_frame(frame, direction)
        if not frame.audio or client is None:
            return
        if (
            self._send_failed
            or self._connection_lost
            or self._socket_client is not client
        ):
            # It did not get through, or went into a socket declared dead
            # while it was being sent: replayed, in order, after the audio
            # that went before it.
            if self._recovery_task is not None:
                self._replay_buffer.insert(self._head_count, (frame, direction))
                self._head_count += 1
        else:
            self._unanswered.add(frame, direction)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if self._connection_lost:
            yield None  # dead socket: drop, do not error per frame
            return
        client = self._socket_client
        async for frame in super().run_stt(audio):
            if isinstance(frame, ErrorFrame) and isinstance(
                frame.exception, ConnectionClosed
            ):
                self._send_failed = True
                await self._on_connection_lost(
                    "send failed", frame.exception, client=client
                )
                continue
            yield frame

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        flush_mode_stop = (
            isinstance(frame, VADUserStoppedSpeakingFrame)
            and not self._settings.vad_signals
        )
        if flush_mode_stop and (self._connection_lost or self._reconnecting):
            # Flush mode: the flush belongs right after the audio before it.
            # Queue a mark there and let the replay send it in place; skip
            # Sarvam's flush now (STTService's own handling still runs).
            if self._recovery_task is not None:
                self._replay_buffer.append((_FLUSH_MARK, direction))
            await STTService.process_frame(self, frame, direction)
            return
        client = self._socket_client
        try:
            await super().process_frame(frame, direction)
            if (
                flush_mode_stop
                and client is not None
                and client is self._socket_client
                and not self._connection_lost
            ):
                # The flush reached Sarvam: it bounds the segment the next
                # transcript answers, and is replayed with it if that never
                # comes.
                self._unanswered.add(_FLUSH_MARK, direction)
        except ConnectionClosed as e:
            # The flush on our VAD's stop can be the first send to find the
            # socket closed; it is owed to the new socket, after the audio
            # already queued before it.
            await self._on_connection_lost("flush failed", e, client=client)
            if flush_mode_stop and self._recovery_task is not None:
                self._replay_buffer.append((_FLUSH_MARK, direction))

    async def _on_connection_lost(
        self,
        cause: str,
        error: Optional[BaseException] = None,
        *,
        client: Any = None,
    ) -> None:
        """Stop using the dead socket, close any open turn, and reconnect.

        ``client`` is the socket the failure came from; a late failure of an
        old socket must not condemn the one that replaced it.
        """
        # Let transcripts already read from the socket (each handled in its
        # own task) mark their audio answered first, so it is not replayed
        # and transcribed twice.
        await asyncio.sleep(0)
        if (
            self._connection_lost
            or self._closing
            or self._shutting_down
            or self._socket_client is None
            or client is not self._socket_client
        ):
            return
        self._connection_lost = True
        self._last_attempt_error = None  # a new outage
        code, reason = _close_details(
            error or self._last_socket_error, self._socket_client
        )
        self._last_close = f"code={code} reason={reason!r}"
        logger.warning(
            f"Sarvam STT connection lost ({cause}): {self._last_close} "
            f"request_id={self._last_request_id}"
        )
        # Start the recovery before any await, so audio arriving meanwhile is
        # buffered for the replay rather than dropped.
        self._start_recovery()
        await self._close_open_speech()

    def _start_recovery(self) -> None:
        """Start the one recovery task, unless it is already running."""
        if self._recovery_task is None:
            self._recovery_task = self.create_task(
                self._recover(), name="sarvam_stt_reconnect"
            )

    async def _close_open_speech(self) -> None:
        """A START_SPEECH whose END_SPEECH died with the socket would leave
        the user turn controller and the aggregator holding "user speaking"
        for the rest of the call; close it here. The new socket sends its
        own START_SPEECH if the caller is still talking."""
        if not self._sarvam_speech_open:
            return
        self._sarvam_speech_open = False
        logger.info("Sarvam STT dropped mid-speech; closing the open user speech")
        await self._call_event_handler("on_speech_stopped")
        await self.broadcast_frame(UserStoppedSpeakingFrame)

    async def _recover(self) -> None:
        """Reconnect with backoff until it holds or the attempts run out."""
        try:
            while not self._shutting_down:
                if self._reconnect_attempts >= len(SARVAM_RECONNECT_BACKOFF_SECS):
                    await self._give_up()
                    return
                self._reconnect_attempts += 1
                await self._reconnect()
                if self._socket_client is not None and not self._connection_lost:
                    logger.info(
                        f"Sarvam STT reconnected (attempt {self._reconnect_attempts}"
                        f"/{len(SARVAM_RECONNECT_BACKOFF_SECS)})"
                    )
                    return
                # A failed attempt leaves its audio in _replay_buffer for
                # the next one.
        finally:
            self._recovery_task = None
            self._replay_buffer.clear()

    async def _do_reconnect(self) -> None:
        # Inside STTService._reconnect; audio arriving now, the backoff
        # included, goes to _replay_buffer (process_audio_frame).
        self._requeue_unanswered()
        await asyncio.sleep(SARVAM_RECONNECT_BACKOFF_SECS[self._reconnect_attempts - 1])
        if self._socket_client is not None and not self._connection_lost:
            # Someone else reconnected meanwhile (pipecat's settings path).
            # Live audio has been queueing behind the guard since this
            # attempt began, so the socket has had none of it: keep it and
            # replay into it rather than tearing it down.
            await self._replay()
            return
        await self._disconnect()
        await self._connect()
        if self._socket_client is None:
            # Failed attempt (pipecat's _connect has said why); returning,
            # not raising, so pipecat does not report it again. The buffer
            # stays for the next attempt.
            return
        await self._replay()

    async def _replay(self) -> None:
        """Send the buffered audio, oldest first, flushes in place.

        Still inside the reconnecting guard, so live frames keep queueing
        behind the buffered ones (a send that yields would otherwise let a
        live frame jump ahead). Frames arriving meanwhile join the end of
        the queue and are sent too; the guard drops only once it is empty,
        with no await in between.
        """
        buffer = self._replay_buffer
        sent = 0
        while sent < len(buffer) and not self._connection_lost:
            frame, direction = buffer[sent]
            if frame is _FLUSH_MARK:
                await self._flush_in_place()
            elif frame.audio:
                await self.process_generator(self.run_stt(frame.audio))
            if self._connection_lost:
                break  # this entry did not get through; keep it
            if frame is _FLUSH_MARK or frame.audio:
                self._unanswered.add(frame, direction)
            sent += 1
        del buffer[:sent]

    def _requeue_unanswered(self) -> None:
        """Put what the dead socket took unanswered in front of the replay:
        it is older than every frame queued since the drop."""
        self._replay_buffer[:0] = self._unanswered.take()
        self._head_count = 0

    async def _flush_in_place(self) -> None:
        client = self._socket_client
        if client is None:
            return
        try:
            await client.flush()
        except ConnectionClosed as e:
            await self._on_connection_lost("flush failed", e, client=client)
        except Exception as e:  # a flush is not worth the replay
            logger.warning(f"Sarvam STT flush after reconnect failed: {e}")

    async def _give_up(self) -> None:
        # Guards a second give-up: a reconnect of pipecat's own (a settings
        # change) can reconnect after this, and the next drop would fire the
        # event again.
        if self._gave_up:
            return
        self._gave_up = True
        message = (
            f"Sarvam STT connection lost ({self._last_close}) and the call's "
            f"{len(SARVAM_RECONNECT_BACKOFF_SECS)} reconnect attempts are used up "
            f"(last attempt: {self._last_attempt_error or 'none made'}; "
            f"last request_id={self._last_request_id})"
        )
        logger.error(message)
        await self._disconnect()  # release a socket that died in its replay
        self._unanswered = _Unanswered()
        # Non-fatal: the pipeline's error handler records it on the call.
        await self.push_error(error_msg=message)
        await self._call_event_handler(STT_UNAVAILABLE_EVENT, message)

    async def _stop_recovery(self) -> None:
        self._shutting_down = True
        if self._recovery_task is not None:
            await self.cancel_task(self._recovery_task)
        self._recovery_task = None

    async def stop(self, frame: EndFrame) -> None:
        await self._stop_recovery()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._stop_recovery()
        await super().cancel(frame)


def get_sarvam_language(language_code: Optional[str]) -> Optional[Language]:
    """
    Convert SARVAM language code to :class:`Language` for STT.

    Args:
        language_code: Language code string (e.g., "en-IN", "hi-IN").

    Returns:
        Language enum value, or None if invalid/not provided.
    """
    if language_code:
        try:
            return Language(language_code)
        except ValueError:
            logger.warning(
                "Invalid STT language code: %s, returning None", language_code
            )
            return None

    logger.debug("No SARVAM STT language code provided, returning None")
    return None


def build_sarvam_stt(config: SarvamConfig):
    """Create a Sarvam STT service.

    Sends ``language`` and ``prompt`` only to a model that accepts them.
    Previously every ``saaras`` model dropped the language, so saaras:v3
    always auto-detected: on noisy 8 kHz Hindi replies it drifted into
    Tamil, Kannada and Gujarati script even when the template pinned hi-IN.
    """
    capabilities = MODEL_CONFIGS.get(config.model)
    # Unknown model: fall back to the old family rule; pipecat validates it.
    takes_language = (
        capabilities.supports_language
        if capabilities
        else "saaras" not in config.model.lower()
    )
    takes_prompt = (
        capabilities.supports_prompt
        if capabilities
        else "saaras" in config.model.lower()
    )
    language_param = (
        get_sarvam_language(language_code=config.language_code)
        if takes_language
        else None
    )
    prompt_param = (config.prompt or None) if takes_prompt else None
    if config.language_code and not takes_language:
        logger.warning(
            f"Sarvam model {config.model} does not accept a language; "
            f"'{config.language_code}' ignored, the model auto-detects"
        )

    logger.info(
        f"Using Sarvam STT service with model={config.model}, language={'set' if language_param else 'none'}, prompt={'set' if prompt_param else 'none'}, vad_signals={config.vad_signals}, high_vad_sensitivity={config.high_vad_sensitivity}"
    )

    return SarvamSTTServiceWithInterruptionPolicy(
        self_interrupt=config.self_interrupt,
        # pipecat's own keepalive: silence after this long without audio, so
        # a quiet stretch cannot idle the socket shut and spend a reconnect.
        keepalive_timeout=SARVAM_KEEPALIVE_SECS,
        api_key=config.api_key,
        model=config.model,
        sample_rate=config.sample_rate,
        settings=SarvamSTTService.Settings(
            language=language_param,
            prompt=prompt_param,
            vad_signals=config.vad_signals if config.vad_signals is not None else True,
            high_vad_sensitivity=(
                config.high_vad_sensitivity
                if config.high_vad_sensitivity is not None
                else False
            ),
        ),
    )
