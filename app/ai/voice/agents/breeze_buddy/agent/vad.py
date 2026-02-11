"""VAD (Voice Activity Detection) configuration for voice agents.

Provides VAD analyzer variants for different modes:
- Standard SileroVADAnalyzer for production
- DiagnosticSileroVADAnalyzer for dev mode (logs confidence/volume)
- STTAwareSileroVADAnalyzer for telephony (dual-signal: Silero + STT)

The dual-signal approach solves the problem where telephony audio (8kHz) has
borderline volume levels that fail the AND-gate (confidence >= threshold AND
volume >= threshold) even when the Silero model detects speech. When STT
produces interim transcriptions (proving speech is present), the volume
requirement is bypassed and only confidence is checked.
"""

import threading
import time
from typing import Optional

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.template.vad import build_default_vad_params
from app.core.config.dynamic import (
    BB_DAILY_VAD_CONFIDENCE,
    BB_DAILY_VAD_MIN_VOLUME,
    BB_DAILY_VAD_START_SECS,
    BB_DAILY_VAD_STOP_SECS,
)
from app.core.config.static import BB_ENABLE_STT_ASSISTED_VAD, ENVIRONMENT
from app.core.logger import logger

# Constants
TELEPHONY_SAMPLE_RATE = 8000
DAILY_SAMPLE_RATE = 16000

# Log VAD diagnostics roughly every ~1 second at 8kHz (31 frames * 32ms)
# and every ~1 second at 16kHz (31 frames * 32ms)
_DIAG_LOG_INTERVAL_FRAMES = 31

# If STT produced an interim transcription within this window, bypass volume check.
# 2 seconds is generous enough to cover the latency between STT receiving audio
# and producing interim tokens, while short enough to not carry stale signals.
_STT_SIGNAL_STALENESS_SECS = 2.0


class STTAwareSileroVADAnalyzer(SileroVADAnalyzer):
    """SileroVADAnalyzer that uses STT interim transcriptions as a secondary signal.

    On telephony (8kHz), the EBU R128 volume calculation combined with exponential
    smoothing (factor 0.2) creates a ramp-up barrier that delays VAD detection by
    hundreds of milliseconds. Meanwhile, STT (e.g. Soniox) receives the same audio
    independently and can produce interim transcriptions almost immediately.

    This analyzer uses a dual-signal approach:
    - Normal mode: confidence >= threshold AND volume >= threshold (standard AND-gate)
    - STT-assisted mode: When STT has recent interim transcriptions, only
      confidence >= threshold is required (volume check bypassed)

    The STTVADBridge processor (in the pipeline after STT) calls
    notify_stt_interim_transcription() whenever it sees an InterimTranscriptionFrame.
    This sets a thread-safe timestamp that _run_analyzer() checks.

    Thread safety: _run_analyzer() runs in a ThreadPoolExecutor while
    notify_stt_interim_transcription() is called from the async pipeline.
    We use a simple float timestamp (atomic on CPython) protected by a lock
    for correctness on all platforms.
    """

    def __init__(
        self, *, sample_rate: Optional[int] = None, params: Optional[VADParams] = None
    ):
        super().__init__(sample_rate=sample_rate, params=params)
        self._stt_lock = threading.Lock()
        self._last_stt_interim_time: float = 0.0
        self._stt_assist_logged = False

    def notify_stt_interim_transcription(self) -> None:
        """Called by STTVADBridge when STT produces an interim transcription.

        Thread-safe. Called from the async pipeline thread.
        """
        with self._stt_lock:
            self._last_stt_interim_time = time.monotonic()

    def _has_recent_stt_signal(self) -> bool:
        """Check if STT produced an interim transcription recently.

        Thread-safe. Called from the ThreadPoolExecutor in _run_analyzer.
        """
        with self._stt_lock:
            last_time = self._last_stt_interim_time
        if last_time == 0.0:
            return False
        return (time.monotonic() - last_time) < _STT_SIGNAL_STALENESS_SECS

    def _run_analyzer(self, buffer: bytes) -> VADState:
        """Analyze audio with STT-assisted volume bypass.

        Overrides the base _run_analyzer to modify the speaking decision:
        when STT has recent interim transcriptions, only confidence is checked.
        """
        self._vad_buffer += buffer

        num_required_bytes = self._vad_frames_num_bytes
        if len(self._vad_buffer) < num_required_bytes:
            return self._vad_state

        stt_active = self._has_recent_stt_signal()

        while len(self._vad_buffer) >= num_required_bytes:
            audio_frames = self._vad_buffer[:num_required_bytes]
            self._vad_buffer = self._vad_buffer[num_required_bytes:]

            confidence = self.voice_confidence(audio_frames)
            volume = self._get_smoothed_volume(audio_frames)
            self._prev_volume = volume  # type: ignore[bad-assignment]

            # Dual-signal decision:
            # Standard: confidence AND volume must both pass
            # STT-assisted: only confidence needs to pass (STT proves speech exists)
            confidence_passes = confidence >= self._params.confidence
            volume_passes = volume >= self._params.min_volume

            if stt_active and confidence_passes and not volume_passes:
                speaking = True
                if not self._stt_assist_logged:
                    logger.info(
                        "VAD: STT-assisted bypass active "
                        f"(conf={confidence:.3f}>={self._params.confidence}, "
                        f"vol={volume:.3f}<{self._params.min_volume})"
                    )
                    self._stt_assist_logged = True
            else:
                speaking = confidence_passes and volume_passes
                if speaking:
                    # Reset log flag so next STT-assist event gets logged
                    self._stt_assist_logged = False

            if speaking:
                match self._vad_state:
                    case VADState.QUIET:
                        self._vad_state = VADState.STARTING
                        self._vad_starting_count = 1
                    case VADState.STARTING:
                        self._vad_starting_count += 1
                    case VADState.STOPPING:
                        self._vad_state = VADState.SPEAKING
                        self._vad_stopping_count = 0
            else:
                match self._vad_state:
                    case VADState.STARTING:
                        self._vad_state = VADState.QUIET
                        self._vad_starting_count = 0
                    case VADState.SPEAKING:
                        self._vad_state = VADState.STOPPING
                        self._vad_stopping_count = 1
                    case VADState.STOPPING:
                        self._vad_stopping_count += 1

        if (
            self._vad_state == VADState.STARTING
            and self._vad_starting_count >= self._vad_start_frames
        ):
            self._vad_state = VADState.SPEAKING
            self._vad_starting_count = 0

        if (
            self._vad_state == VADState.STOPPING
            and self._vad_stopping_count >= self._vad_stop_frames
        ):
            self._vad_state = VADState.QUIET
            self._vad_stopping_count = 0

        return self._vad_state


class DiagnosticSileroVADAnalyzer(STTAwareSileroVADAnalyzer):
    """STTAwareSileroVADAnalyzer with diagnostic logging for dev mode.

    Intercepts confidence and volume calculations to log their values
    periodically, helping diagnose VAD detection issues. Also logs on
    state transitions (QUIET<->SPEAKING) for visibility into edge cases.
    """

    def __init__(
        self, *, sample_rate: Optional[int] = None, params: Optional[VADParams] = None
    ):
        super().__init__(sample_rate=sample_rate, params=params)
        self._diag_frame_counter = 0
        self._last_confidence = 0.0
        self._last_volume = 0.0
        self._last_state: Optional[VADState] = None

    def voice_confidence(self, buffer) -> float:
        conf = super().voice_confidence(buffer)
        self._last_confidence = conf
        return conf

    def _get_smoothed_volume(self, audio: bytes) -> float:
        vol = super()._get_smoothed_volume(audio)
        self._last_volume = vol
        return vol

    async def analyze_audio(self, buffer: bytes) -> VADState:
        state = await super().analyze_audio(buffer)
        self._diag_frame_counter += 1

        stt_active = self._has_recent_stt_signal()

        # Log on state transitions (most important for debugging)
        if self._last_state is not None and state != self._last_state:
            logger.debug(
                f"VAD state change: {self._last_state.name} -> {state.name} | "
                f"conf={self._last_confidence:.3f} (threshold={self._params.confidence}), "
                f"vol={self._last_volume:.3f} (threshold={self._params.min_volume}), "
                f"stt_active={stt_active}"
            )

        # Periodic logging (~every 1 second)
        if self._diag_frame_counter % _DIAG_LOG_INTERVAL_FRAMES == 0:
            confidence_passes = self._last_confidence >= self._params.confidence
            volume_passes = self._last_volume >= self._params.min_volume
            speaking = confidence_passes and (volume_passes or stt_active)
            logger.debug(
                f"VAD diag: conf={self._last_confidence:.3f}/{self._params.confidence}, "
                f"vol={self._last_volume:.3f}/{self._params.min_volume}, "
                f"speaking={speaking}, state={state.name}, stt_active={stt_active}"
            )

        self._last_state = state
        return state


async def create_daily_vad_params() -> VADParams:
    """Create VAD parameters for Daily mode from dynamic config."""
    return VADParams(
        confidence=await BB_DAILY_VAD_CONFIDENCE(),
        start_secs=await BB_DAILY_VAD_START_SECS(),
        stop_secs=await BB_DAILY_VAD_STOP_SECS(),
        min_volume=await BB_DAILY_VAD_MIN_VOLUME(),
    )


def _create_telephony_analyzer(
    sample_rate: int, params: VADParams
) -> SileroVADAnalyzer:
    """Create VAD analyzer for telephony, with STT-assisted and diagnostic variants."""
    if ENVIRONMENT.lower() == "dev":
        logger.info("Using DiagnosticSileroVADAnalyzer (dev mode, STT-aware)")
        return DiagnosticSileroVADAnalyzer(sample_rate=sample_rate, params=params)
    if BB_ENABLE_STT_ASSISTED_VAD:
        logger.info("Using STTAwareSileroVADAnalyzer for telephony")
        return STTAwareSileroVADAnalyzer(sample_rate=sample_rate, params=params)
    return SileroVADAnalyzer(sample_rate=sample_rate, params=params)


def _create_daily_analyzer(sample_rate: int, params: VADParams) -> SileroVADAnalyzer:
    """Create VAD analyzer for Daily mode (16kHz, no STT-assist needed)."""
    if ENVIRONMENT.lower() == "dev":
        logger.info("Using DiagnosticSileroVADAnalyzer (dev mode)")
        return DiagnosticSileroVADAnalyzer(sample_rate=sample_rate, params=params)
    return SileroVADAnalyzer(sample_rate=sample_rate, params=params)


async def create_vad_analyzer(
    is_daily_mode: bool,
    template: Optional[TemplateModel] = None,
) -> tuple[SileroVADAnalyzer, Optional[VADParams]]:
    """Create VAD analyzer with appropriate parameters.

    For telephony mode, uses STTAwareSileroVADAnalyzer which accepts STT interim
    transcription signals to bypass volume checks when STT proves speech is present.
    In dev mode, uses DiagnosticSileroVADAnalyzer which adds logging on top.

    Args:
        is_daily_mode: Whether this is Daily mode
        template: Template model for telephony mode VAD params

    Returns:
        Tuple of (SileroVADAnalyzer, default_vad_params for telephony or None for Daily)
    """
    if is_daily_mode:
        params = await create_daily_vad_params()
        return _create_daily_analyzer(DAILY_SAMPLE_RATE, params), None

    default_vad_params = build_default_vad_params(template)
    return (
        _create_telephony_analyzer(TELEPHONY_SAMPLE_RATE, default_vad_params),
        default_vad_params,
    )
