"""
TTS Health Observer

Monitors TTS service health with provider-specific error detection strategies.
Tracks audio frame production and detects failures to trigger automatic fallback.

Provider-Specific Detection:
- ElevenLabs: Silent failures (empty audio, no ErrorFrame) → detect via audio bytes
- Cartesia: Explicit errors (sends ErrorFrame) → detect via ErrorFrame + audio validation

Detection Methods:
1. ErrorFrame from TTS service → immediate failure
2. TTSStoppedFrame without audio → failure
3. TTSStoppedFrame with empty/minimal audio (<threshold) → failure
4. Timeout (no response within threshold) → failure
"""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.tts_service import TTSService

from app.core.config.dynamic import (
    BB_TTS_FAILURE_THRESHOLD,
    BB_TTS_FALLBACK_ENABLED,
    BB_TTS_SERVICE,
)
from app.services.live_config.store import set_config


class TTSProvider(Enum):
    """Supported TTS providers."""

    ELEVENLABS = "elevenlabs"
    CARTESIA = "cartesia"


class FailureReason(Enum):
    """Types of TTS failures for logging and debugging."""

    ERROR_FRAME = "error_frame"
    NO_AUDIO = "no_audio"
    EMPTY_AUDIO = "empty_audio"
    TIMEOUT = "timeout"
    PIPELINE_ERROR = "pipeline_error"


@dataclass
class TTSRequestState:
    """State for tracking a single TTS request."""

    started_at: float
    provider: TTSProvider
    audio_received: bool = False
    audio_frame_count: int = 0
    total_audio_bytes: int = 0

    @property
    def elapsed_seconds(self) -> float:
        """Time elapsed since request started."""
        return time.time() - self.started_at


# Bidirectional fallback mapping
TTS_SWAP_MAP = {
    "elevenlabs": "cartesia",
    "cartesia": "elevenlabs",
}

# Provider-specific configuration
# For 8kHz 16-bit mono (Twilio output): 16,000 bytes/second
# Silent failures typically return 0 bytes
PROVIDER_CONFIG = {
    TTSProvider.ELEVENLABS: {
        "min_audio_bytes": 300,
    },
    TTSProvider.CARTESIA: {
        "min_audio_bytes": 300,
    },
}

# Minimum time (seconds) before TTS should produce audio
# If TTSStoppedFrame comes before this, it's likely an interruption, not a failure
# This prevents false positives when user interrupts quickly
MIN_TTS_DURATION_FOR_FAILURE = 0.3  # 300ms


class TTSHealthObserver(BaseObserver):
    """
    Monitors TTS health with provider-aware error detection.

    ElevenLabs Detection:
    - Does NOT send ErrorFrame for credential/quota issues
    - Fails silently with empty audio
    - Detection: Check audio bytes threshold

    Cartesia Detection:
    - Sends explicit ErrorFrame on failures
    - Detection: ErrorFrame + audio validation as backup
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._current_request: Optional[TTSRequestState] = None
        self._failure_counts: Dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._fallback_triggered = False

        logger.info("[TTS Health] Observer initialized")

    def _detect_provider(self, source_name: str) -> TTSProvider:
        """Detect TTS provider from source class name."""
        name_lower = source_name.lower()
        if "elevenlabs" in name_lower:
            return TTSProvider.ELEVENLABS
        elif "cartesia" in name_lower:
            return TTSProvider.CARTESIA
        else:
            # Default to ElevenLabs detection strategy (more conservative)
            return TTSProvider.ELEVENLABS

    def _get_min_audio_threshold(self, provider: TTSProvider) -> int:
        """Get minimum audio bytes threshold for provider."""
        return PROVIDER_CONFIG[provider]["min_audio_bytes"]

    async def on_push_frame(self, data: FramePushed):
        """Process frame events to track TTS health."""
        frame = data.frame
        source = data.source
        is_from_tts = isinstance(source, TTSService)

        # Handle ErrorFrame from TTS
        if isinstance(frame, ErrorFrame):
            await self._handle_error_frame(frame, is_from_tts)
            return

        # Only process TTS frames from TTS services
        if not is_from_tts:
            return

        provider = self._detect_provider(source.__class__.__name__)

        if isinstance(frame, TTSStartedFrame):
            await self._handle_tts_started(provider)
        elif isinstance(frame, TTSAudioRawFrame):
            await self._handle_audio_frame(frame)
        elif isinstance(frame, TTSStoppedFrame):
            await self._handle_tts_stopped()

    async def _handle_error_frame(self, frame: ErrorFrame, is_from_tts: bool):
        """Handle ErrorFrame - immediate failure for explicit errors."""
        async with self._lock:
            if self._current_request is None or not is_from_tts:
                return
            request = self._current_request
            self._current_request = None

        error_msg = getattr(frame, "error", str(frame))
        provider_name = request.provider.value

        logger.error(f"[TTS Health] ❌ ERROR FRAME - {provider_name}: {error_msg}")
        await self._record_failure(provider_name, FailureReason.ERROR_FRAME)

    async def _handle_tts_started(self, provider: TTSProvider):
        """Handle TTSStartedFrame - begin tracking new request."""
        async with self._lock:
            self._current_request = TTSRequestState(
                started_at=time.time(),
                provider=provider,
            )
        logger.info(f"[TTS Health] 🎙️ TTS STARTED - {provider.value}")

    async def _handle_audio_frame(self, frame: TTSAudioRawFrame):
        """Handle TTSAudioRawFrame - track audio reception."""
        audio_bytes = getattr(frame, "audio", b"")
        audio_size = len(audio_bytes)

        async with self._lock:
            if self._current_request is None:
                return

            request = self._current_request
            request.audio_frame_count += 1
            request.total_audio_bytes += audio_size

            if not request.audio_received:
                request.audio_received = True
                elapsed = time.time() - request.started_at

                if audio_size > 0:
                    logger.info(
                        f"[TTS Health] 🔊 FIRST AUDIO - {request.provider.value} "
                        f"({elapsed:.2f}s, {audio_size} bytes)"
                    )
                else:
                    logger.warning(
                        f"[TTS Health] ⚠️ EMPTY FIRST FRAME - {request.provider.value}"
                    )

    async def _handle_tts_stopped(self):
        """Handle TTSStoppedFrame - evaluate success or failure."""
        async with self._lock:
            if self._current_request is None:
                return
            request = self._current_request
            self._current_request = None

        provider_name = request.provider.value
        min_threshold = self._get_min_audio_threshold(request.provider)
        elapsed = request.elapsed_seconds

        # Always log bytes for comparison/tuning
        logger.info(
            f"[TTS Health] 📊 STATS - {provider_name}: "
            f"{request.total_audio_bytes} bytes, {request.audio_frame_count} frames, "
            f"{elapsed:.2f}s (threshold: {min_threshold})"
        )

        # Case 1: Real audio received - SUCCESS
        if request.audio_received and request.total_audio_bytes >= min_threshold:
            self._failure_counts[provider_name] = 0
            logger.info(
                f"[TTS Health] ✅ TTS COMPLETE - {provider_name} "
                f"({request.audio_frame_count} frames, {request.total_audio_bytes} bytes, {elapsed:.2f}s)"
            )
            return

        # Case 2: Empty/minimal audio - FAILURE (ElevenLabs silent failure pattern)
        if request.audio_received and request.total_audio_bytes < min_threshold:
            logger.warning(
                f"[TTS Health] ❌ TTS FAILED - {provider_name} "
                f"(empty audio: {request.total_audio_bytes} < {min_threshold} bytes)"
            )
            await self._record_failure(provider_name, FailureReason.EMPTY_AUDIO)
            return

        # Case 3: No audio at all
        # Only count as failure if enough time passed (>300ms)
        # Quick stops are likely user interruptions, not failures
        if elapsed < MIN_TTS_DURATION_FOR_FAILURE:
            logger.debug(
                f"[TTS Health] TTS stopped quickly ({elapsed:.2f}s) - likely interruption, not failure"
            )
            return

        # Case 4: No audio after reasonable time - FAILURE
        logger.warning(
            f"[TTS Health] ❌ TTS FAILED - {provider_name} (no audio after {elapsed:.2f}s)"
        )
        await self._record_failure(provider_name, FailureReason.NO_AUDIO)

    async def check_timeouts(self, timeout_seconds: float) -> int:
        """Check for timed-out TTS requests. Returns 1 if timeout, 0 otherwise."""
        async with self._lock:
            if self._current_request is None:
                return 0

            elapsed = time.time() - self._current_request.started_at
            if elapsed <= timeout_seconds:
                return 0

            provider_name = self._current_request.provider.value
            self._current_request = None

        logger.warning(f"[TTS Health] ⏱️ TIMEOUT - {provider_name} ({elapsed:.1f}s)")
        await self._record_failure(provider_name, FailureReason.TIMEOUT)
        return 1

    async def record_pipeline_error(self, error_msg: str):
        """Record TTS failure from pipeline-level error detection."""
        async with self._lock:
            provider_name = (
                self._current_request.provider.value
                if self._current_request
                else "unknown"
            )
            self._current_request = None

        logger.error(f"[TTS Health] ❌ PIPELINE ERROR - {provider_name}: {error_msg}")
        await self._record_failure(provider_name, FailureReason.PIPELINE_ERROR)

    async def _record_failure(self, provider_name: str, reason: FailureReason):
        """Record a TTS failure and potentially trigger fallback."""
        self._failure_counts[provider_name] = (
            self._failure_counts.get(provider_name, 0) + 1
        )
        count = self._failure_counts[provider_name]
        threshold = await BB_TTS_FAILURE_THRESHOLD()

        logger.warning(
            f"[TTS Health] 📊 FAILURE #{count}/{threshold} - {provider_name} ({reason.value})"
        )

        if count >= threshold:
            await self._check_and_trigger_fallback(provider_name)

    async def _check_and_trigger_fallback(self, failed_provider: str):
        """Check if fallback should be triggered."""
        fallback_enabled = await BB_TTS_FALLBACK_ENABLED()

        if fallback_enabled:
            logger.error(f"[TTS Health] 🚨 BOTH SERVICES FAILING - {failed_provider}")
            await self._send_both_failing_alert(failed_provider)
            self._failure_counts[failed_provider] = 0
        elif not self._fallback_triggered:
            logger.warning(f"[TTS Health] 🔄 TRIGGERING FALLBACK - {failed_provider}")
            await self._trigger_fallback(failed_provider)

    async def _trigger_fallback(self, failed_service: str):
        """Enable fallback in Redis and notify."""
        alt_service = TTS_SWAP_MAP.get(failed_service, "unknown")

        # Set the failed provider FIRST to avoid race conditions
        await set_config("BB_TTS_FAILED_PROVIDER", failed_service)
        # THEN enable the fallback flag
        success = await set_config("BB_TTS_FALLBACK_ENABLED", True)
        if success:
            self._fallback_triggered = True
            self._failure_counts[failed_service] = 0
            logger.warning(
                f"🔄 TTS FALLBACK TRIGGERED: {failed_service} → {alt_service}"
            )
            await self._send_fallback_alert(failed_service, alt_service)
        else:
            logger.error("[TTS Health] Failed to update Redis for fallback!")

    async def _send_fallback_alert(self, failed_service: str, alt_service: str):
        """Send Slack alert when TTS fallback is triggered."""
        try:
            from app.services.slack import slack_alert

            threshold = await BB_TTS_FAILURE_THRESHOLD()

            await slack_alert.send(
                title="🔄 TTS Fallback Triggered",
                fields=[
                    {"name": "Failed Service", "value": failed_service.upper()},
                    {"name": "Fallback Service", "value": alt_service.upper()},
                    {"name": "Failure Threshold", "value": str(threshold)},
                ],
                sections=[
                    {
                        "title": "Recovery",
                        "text": "Set `BB_TTS_FALLBACK_ENABLED=false` once primary TTS is restored.",
                    }
                ],
                fallback_text=f"TTS Fallback: {failed_service} → {alt_service}",
            )
        except Exception as e:
            logger.error(f"[TTS Health] Slack alert failed: {e}")

    async def _send_both_failing_alert(self, current_service: str):
        """Send critical Slack alert when BOTH TTS services are failing."""
        try:
            from app.services.slack import slack_alert

            configured = await BB_TTS_SERVICE()
            alt_service = TTS_SWAP_MAP.get(configured, "unknown")

            await slack_alert.send(
                title="🚨 CRITICAL: Both TTS Services Failing",
                fields=[
                    {"name": "Primary", "value": configured.upper()},
                    {"name": "Fallback", "value": alt_service.upper()},
                ],
                sections=[
                    {
                        "title": "Action Required",
                        "text": "⚠️ Check ElevenLabs AND Cartesia immediately.",
                    }
                ],
                fallback_text="CRITICAL: Both TTS services failing!",
            )
        except Exception as e:
            logger.error(f"[TTS Health] Critical alert failed: {e}")

    async def get_health_status(self) -> dict:
        """Get current health status for monitoring."""
        async with self._lock:
            return {
                "is_tracking": self._current_request is not None,
                "current_provider": (
                    self._current_request.provider.value
                    if self._current_request
                    else None
                ),
                "failure_counts": dict(self._failure_counts),
                "fallback_triggered": self._fallback_triggered,
            }

    def reset(self):
        """Reset observer state."""
        self._current_request = None
        self._fallback_triggered = False
        logger.info("[TTS Health] Observer reset")


# ============================================================================
# Module utilities
# ============================================================================

_tts_health_observer: Optional[TTSHealthObserver] = None


def get_tts_health_observer() -> TTSHealthObserver:
    """Get or create the global TTS health observer."""
    global _tts_health_observer
    if _tts_health_observer is None:
        _tts_health_observer = TTSHealthObserver()
    return _tts_health_observer


async def start_tts_health_check_loop(
    observer: TTSHealthObserver, check_interval: float = 1.0
):
    """Start background loop that checks for TTS timeouts."""
    from app.core.config.dynamic import BB_TTS_AUDIO_TIMEOUT_SECONDS

    logger.info(
        f"[TTS Health] Timeout check loop started (interval: {check_interval}s)"
    )

    while True:
        try:
            timeout = await BB_TTS_AUDIO_TIMEOUT_SECONDS()
            await observer.check_timeouts(timeout)
        except Exception as e:
            logger.error(f"[TTS Health] Check loop error: {e}")
        await asyncio.sleep(check_interval)
