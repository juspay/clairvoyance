"""Sarvam STT helpers and builder."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import MODEL_CONFIGS, SarvamSTTService
from pipecat.transcriptions.language import Language

from app.core.logger import logger

# Set for the duration of one Sarvam data message's handling: pipecat runs
# each message in its own task, so this marks that message's frame only.
# Queue hand-offs between the user aggregator (our VAD) and this service:
# transcription_gate, transcript_collector (stream mode), our own input
# queue, plus one spare.
_VAD_START_HOPS = 4

_SARVAM_DATA_MESSAGE: ContextVar[bool] = ContextVar(
    "sarvam_data_message", default=False
)

__all__ = [
    "SarvamConfig",
    "SarvamSTTServiceWithInterruptionPolicy",
    "get_sarvam_language",
    "build_sarvam_stt",
]


@dataclass
class SarvamConfig:
    """Configuration for Sarvam STT.

    Which of ``language`` / ``prompt`` reaches Sarvam is decided by what the
    model accepts (pipecat's ``MODEL_CONFIGS``), not by its family name:
    saaras:v3 takes a language and rejects a prompt, saaras:v2.5 the reverse,
    saarika takes a language. An unset language means Sarvam auto-detects.
    ``negative_frames_count`` / ``negative_frames_window`` reach only a model
    that takes fine-grained VAD parameters (saaras:v3); unset sends nothing.
    """

    api_key: str
    model: str
    sample_rate: int
    language_code: Optional[str] = None
    prompt: Optional[str] = None
    vad_signals: Optional[bool] = None
    high_vad_sensitivity: Optional[bool] = None
    negative_frames_count: Optional[int] = None
    negative_frames_window: Optional[int] = None
    self_interrupt: bool = True
    finalize_on_segment: bool = True


class SarvamSTTServiceWithInterruptionPolicy(SarvamSTTService):
    """pipecat's SarvamSTTService, with its own barge-in made optional and
    finalized segments.

    Barge-in: Sarvam streams no partial words, only a START_SPEECH signal and
    the finished text of each segment. On START_SPEECH pipecat interrupts the
    bot itself, on the first sound, so a template's
    ``interruption.min_words`` ("haan" must not stop the bot) never gets a
    say. With ``self_interrupt=False`` the signal is still broadcast, but the
    interruption is left to the pipeline's rule, which then judges the
    finished segment when the caller pauses.

    Finalized segments: see ``_should_finalize``.
    """

    def __init__(
        self,
        *,
        self_interrupt: bool = True,
        finalize_on_segment: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._self_interrupt = self_interrupt
        self._finalize_on_segment = finalize_on_segment
        # Sarvam's own VAD said START_SPEECH and no END_SPEECH yet.
        self._sarvam_speech_open = False
        # Our VAD's view, set before pipecat's handlers can yield.
        self._vad_speaking = False
        # Sarvam's session id, logged when it first appears on a connection
        # (and again if it changes), for asking Sarvam about a slow or
        # dropped session.
        self._logged_request_id: Optional[str] = None

    def set_self_interrupt(self, enabled: bool) -> None:
        """Follow a node-level interruption override mid-call."""
        self._self_interrupt = enabled

    async def broadcast_interruption(self):
        # pipecat 1.1.0's Sarvam service calls this only on START_SPEECH.
        if self._self_interrupt:
            await super().broadcast_interruption()

    # ------------------------------------------------------------ finalized

    def _should_finalize(self) -> bool:
        """Whether the segment text Sarvam is sending now closes what the
        caller said.

        With ``vad_signals`` Sarvam sends a segment's text only after its own
        VAD has ended the segment, so nothing more is coming for it. pipecat
        does not mark it finalized, so SpeechTimeoutUserTurnStopStrategy
        waits out its STT safety timer (SARVAM_TTFS_P99 1.17 s minus the VAD
        stop_secs) on every turn, though Sarvam's final lands ~830 ms (p50)
        after the caller stops. Marked finalized, the timer is skipped; the
        ``user_speech_timeout`` floor still applies. Under smart_turn the
        turn analyzer likewise stops waiting once a marked segment lands.
        Without a local VAD pipecat already skips that timer, so nothing
        changes there.

        Trade-off: a sentence Sarvam splits at a pause (~14% of sentences in
        testing) can now end the turn on its first piece. Not marked while
        the caller may still be speaking: our VAD hears speech, or Sarvam's
        own START_SPEECH is open. A mark set while our VAD still hears speech
        would outlive the pause it was meant for: pipecat clears it only on
        the next VAD start, so a caller who pauses shorter than our
        stop_secs and keeps talking would have the whole turn closed, at the
        eventual stop, on the first piece's text. The cost is small: on
        telephony (stop_secs 0.3 s) Sarvam's final lands after our VAD stop;
        on Daily (0.95 s) the timer it skips is only ~0.2 s anyway. Never in
        flush mode (``vad_signals`` off), where the text follows our flush,
        not Sarvam's VAD. Kill switch ``BB_SARVAM_STT_FINALIZE_ON_SEGMENT``,
        read when a call's STT is built (new calls only).
        """
        return (
            self._finalize_on_segment
            and self._settings.vad_signals is True
            and not self._sarvam_speech_open
            and not self._vad_speaking
        )

    async def _handle_vad_user_started_speaking(
        self, frame: VADUserStartedSpeakingFrame
    ) -> None:
        # Before awaiting: pipecat's handler can yield (cancelling its TTFB
        # task) before it sets _user_speaking, and a segment handled in that
        # gap must already see the caller as resumed.
        self._vad_speaking = True
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_vad_user_stopped_speaking(
        self, frame: VADUserStoppedSpeakingFrame
    ) -> None:
        self._vad_speaking = False
        await super()._handle_vad_user_stopped_speaking(frame)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        # Decided at push time, after pipecat's own awaits in its message
        # handler, so a VAD start handled meanwhile is seen.
        if (
            isinstance(frame, TranscriptionFrame)
            and _SARVAM_DATA_MESSAGE.get()
            and self._should_finalize()
        ):
            frame.finalized = True
        await super().push_frame(frame, direction)

    async def _handle_message(self, message: Any) -> None:
        """Note Sarvam's speech state and session id, arm the finalized
        mark for a segment's text, then let pipecat act."""
        data = getattr(message, "data", None)
        kind = getattr(message, "type", None)
        is_data = False
        if kind == "events":
            signal = getattr(data, "signal_type", None)
            if signal == "START_SPEECH":
                self._sarvam_speech_open = True
            elif signal == "END_SPEECH":
                self._sarvam_speech_open = False
        elif kind == "data":
            request_id = getattr(data, "request_id", None)
            if request_id and request_id != self._logged_request_id:
                # Once per connection if Sarvam scopes it to the session,
                # once per segment if it scopes it to the segment.
                self._logged_request_id = request_id
                logger.info(f"Sarvam STT session request_id={request_id}")
            is_data = True
            if self._finalize_on_segment and self._settings.vad_signals is True:
                # Our VAD runs downstream (in the user aggregator); its start
                # reaches us upstream through each processor in between
                # (transcription_gate, plus transcript_collector in stream
                # mode), then our own input queue. Every hop is a queue
                # hand-off that runs on that processor's task one loop turn
                # later, so yield once per hop (plus one spare) to let a VAD
                # start already on its way land before this segment is
                # judged. Microseconds; skipped with the switch off, so the
                # frame order is as before.
                for _ in range(_VAD_START_HOPS):
                    await asyncio.sleep(0)
        token = _SARVAM_DATA_MESSAGE.set(is_data)
        try:
            await super()._handle_message(message)
        finally:
            _SARVAM_DATA_MESSAGE.reset(token)

    async def _connect(self) -> None:
        await super()._connect()
        # A new socket is a new Sarvam session with no speech open yet; an
        # END_SPEECH the old socket never sent must not block finalizing.
        # The old socket's queued message tasks have already run: pipecat's
        # _disconnect awaits the receive task's cancel and the close before
        # this, and each handler sets its state before its first await.
        self._logged_request_id = None
        self._sarvam_speech_open = False


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

    The silence window (``negative_frames_*``) is refused at template save
    for a model the template names; one that resolves from the Redis default
    is checked here and dropped with a warning, since pipecat would refuse
    the whole service and the call would have no STT at all.
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
    takes_vad_params = bool(capabilities and capabilities.supports_vad_params)
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
    negative_frames_count = config.negative_frames_count
    negative_frames_window = config.negative_frames_window
    if (
        negative_frames_count is not None or negative_frames_window is not None
    ) and not takes_vad_params:
        logger.warning(
            f"Sarvam model {config.model} does not accept a silence window; "
            f"negative_frames_count/window ignored, Sarvam's default applies"
        )
        negative_frames_count = negative_frames_window = None
    high_vad_sensitivity = config.high_vad_sensitivity
    if negative_frames_count is not None and high_vad_sensitivity:
        # The template's measured window wins over the global 2/2 setting,
        # which would cut its sentences in half.
        logger.warning(
            "Sarvam high_vad_sensitivity ignored: the template sets its own "
            f"silence window {negative_frames_count}/{negative_frames_window}"
        )
        high_vad_sensitivity = False

    logger.info(
        f"Using Sarvam STT service with model={config.model}, language={'set' if language_param else 'none'}, prompt={'set' if prompt_param else 'none'}, vad_signals={config.vad_signals}, high_vad_sensitivity={config.high_vad_sensitivity}, negative_frames={negative_frames_count}/{negative_frames_window}, finalize_on_segment={config.finalize_on_segment}"
    )

    return SarvamSTTServiceWithInterruptionPolicy(
        self_interrupt=config.self_interrupt,
        finalize_on_segment=config.finalize_on_segment,
        api_key=config.api_key,
        model=config.model,
        sample_rate=config.sample_rate,
        settings=SarvamSTTService.Settings(
            language=language_param,
            prompt=prompt_param,
            vad_signals=config.vad_signals if config.vad_signals is not None else True,
            high_vad_sensitivity=(
                high_vad_sensitivity if high_vad_sensitivity is not None else False
            ),
            negative_frames_count=negative_frames_count,
            negative_frames_window=negative_frames_window,
        ),
    )
