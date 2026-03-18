import asyncio
from dataclasses import dataclass
from typing import Optional

from pipecat.transcriptions.language import Language

from app.ai.voice.stt import (
    DeepgramConfig,
    SarvamConfig,
    SonioxConfig,
    build_deepgram_stt,
    build_google_stt,
    build_openai_stt,
    build_sarvam_stt,
    build_soniox_stt,
)
from app.core.config.dynamic import (
    BB_SARVAM_STT_HIGH_VAD_SENSITIVITY,
    BB_SARVAM_STT_LANGUAGE_CODE,
    BB_SARVAM_STT_MODEL,
    BB_SARVAM_STT_PROMPT,
    BB_SARVAM_STT_VAD_SIGNALS,
)
from app.core.config.static import (
    BREEZE_BUDDY_DEEPGRAM_AUTO_DETECT_LANGUAGE,
    BREEZE_BUDDY_DEEPGRAM_DIARIZE,
    BREEZE_BUDDY_DEEPGRAM_ENDPOINTING,
    BREEZE_BUDDY_DEEPGRAM_INTERIM_RESULTS,
    BREEZE_BUDDY_DEEPGRAM_LANGUAGE,
    BREEZE_BUDDY_DEEPGRAM_MODEL,
    BREEZE_BUDDY_DEEPGRAM_NO_DELAY,
    BREEZE_BUDDY_DEEPGRAM_NUMERALS,
    BREEZE_BUDDY_DEEPGRAM_PROFANITY_FILTER,
    BREEZE_BUDDY_DEEPGRAM_PUNCTUATE,
    BREEZE_BUDDY_DEEPGRAM_SMART_FORMAT,
    BREEZE_BUDDY_DEEPGRAM_UTTERANCE_END_MS,
    BREEZE_BUDDY_DEEPGRAM_VAD_EVENTS,
    BREEZE_BUDDY_SONIOX_CONTEXT,
    BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS,
    BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS,
    BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS,
    BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS,
    BREEZE_BUDDY_SONIOX_MODEL,
    BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
    BREEZE_BUDDY_STT_SERVICE,
    DEEPGRAM_API_KEY,
    ENABLE_BREEZE_BUDDY_STT_FALLBACK,
    GOOGLE_CREDENTIALS_JSON,
    OPENAI_STT_API_KEY,
    OPENAI_STT_MODEL,
    SAMPLE_RATE,
    SARVAM_API_KEY,
    SONIOX_API_KEY,
)
from app.core.logger import logger
from app.services.slack import slack_alert

# Background task references to prevent premature GC of fire-and-forget tasks
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine as a fire-and-forget task, preventing GC."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@dataclass
class STTServiceResult:
    """Result of STT service initialization.

    Attributes:
        service: The primary STT service instance
        provider: Name of the active STT provider ("soniox", "deepgram", "sarvam", "openai", "google")
        is_fallback: True if this provider was selected as a fallback due to primary provider failure
        fallback_service: Pre-built Deepgram STT for mid-call hot-swap via ServiceSwitcher.
                         Only populated for HALF-OPEN probe calls.
        is_probe_call: True if this call is a HALF-OPEN circuit breaker probe.
                      The agent must call record_success()/release_probe() on completion.
    """

    service: object
    provider: str
    is_fallback: bool = False
    fallback_service: Optional[object] = None
    is_probe_call: bool = False


def _build_deepgram_fallback(*, vad_events: Optional[bool] = None) -> object:
    """Build Deepgram STT service using Breeze Buddy-specific config.

    Args:
        vad_events: Override for Deepgram server-side VAD events.
                    When None, uses the BREEZE_BUDDY_DEEPGRAM_VAD_EVENTS env var.
                    Set to False for mid-call ServiceSwitcher fallback to prevent
                    spurious InterruptionTaskFrames that cancel the greeting.

    Returns:
        Deepgram STT service instance

    Raises:
        ValueError: If DEEPGRAM_API_KEY is not configured
    """
    if not DEEPGRAM_API_KEY:
        raise ValueError("DEEPGRAM_API_KEY is required for Deepgram STT fallback")

    effective_vad_events = (
        vad_events if vad_events is not None else BREEZE_BUDDY_DEEPGRAM_VAD_EVENTS
    )

    return build_deepgram_stt(
        DeepgramConfig(
            api_key=DEEPGRAM_API_KEY,
            model=BREEZE_BUDDY_DEEPGRAM_MODEL,
            language=BREEZE_BUDDY_DEEPGRAM_LANGUAGE,
            auto_detect_language=BREEZE_BUDDY_DEEPGRAM_AUTO_DETECT_LANGUAGE,
            smart_format=BREEZE_BUDDY_DEEPGRAM_SMART_FORMAT,
            punctuate=BREEZE_BUDDY_DEEPGRAM_PUNCTUATE,
            endpointing=BREEZE_BUDDY_DEEPGRAM_ENDPOINTING,
            vad_events=effective_vad_events,
            utterance_end_ms=BREEZE_BUDDY_DEEPGRAM_UTTERANCE_END_MS,
            no_delay=BREEZE_BUDDY_DEEPGRAM_NO_DELAY,
            interim_results=BREEZE_BUDDY_DEEPGRAM_INTERIM_RESULTS,
            profanity_filter=BREEZE_BUDDY_DEEPGRAM_PROFANITY_FILTER,
            numerals=BREEZE_BUDDY_DEEPGRAM_NUMERALS,
            diarize=BREEZE_BUDDY_DEEPGRAM_DIARIZE,
        )
    )


def _build_soniox(
    language_hints: Optional[str] = None,
    soniox_context: Optional[str] = None,
) -> object:
    """Build Soniox STT service using Breeze Buddy-specific config.

    Extracted to avoid duplication between CLOSED and HALF-OPEN probe paths.
    Disables reconnect_on_error when fallback is enabled so a single error
    triggers immediate circuit breaker recording instead of 3× retry (12s dead air).
    """
    effective_context = (
        soniox_context if soniox_context is not None else BREEZE_BUDDY_SONIOX_CONTEXT
    )
    if soniox_context:
        logger.info("Using template-specific Soniox context")

    # SONIOX_API_KEY is validated by the caller before invoking _build_soniox,
    # but assert here for type narrowing (pyrefly/mypy).
    assert SONIOX_API_KEY is not None, "SONIOX_API_KEY must be set"

    return build_soniox_stt(
        SonioxConfig(
            api_key=SONIOX_API_KEY,
            model=BREEZE_BUDDY_SONIOX_MODEL,
            vad_force_turn_endpoint=BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
            language_hints=(
                language_hints
                if language_hints is not None
                else BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS
            ),
            context_json=effective_context,
            enable_non_final_tokens=BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS,
            max_non_final_tokens_duration_ms=BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS,
            max_endpoint_delay_ms=BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS,
            log_context="Breeze Buddy",
            language_hints_strict=True if language_hints else False,
            # Disable auto-reconnect when fallback is enabled AND Deepgram
            # is actually configured, so a single error triggers immediate
            # recording instead of retrying 3× (12s dead air) before a
            # fatal ErrorFrame. If DEEPGRAM_API_KEY is missing, keep
            # Soniox resilient with auto-reconnect.
            reconnect_on_error=not (
                ENABLE_BREEZE_BUDDY_STT_FALLBACK and DEEPGRAM_API_KEY
            ),
        )
    )


async def _send_soniox_failure_alert(error: Exception, fallback_used: str) -> None:
    """Send Slack alert when Soniox STT fails at startup. Fire-and-forget."""
    try:
        await slack_alert.send(
            title="🚨 Soniox Failed at Startup (Breeze Buddy)",
            fields=[
                {"name": "Fallback", "value": fallback_used.capitalize()},
            ],
            sections=[
                {
                    "title": "What Happened",
                    "text": (
                        f"Soniox STT failed to initialize for a Breeze Buddy call. "
                        f"This call is using {fallback_used.capitalize()} instead.\n"
                        f"```{str(error)[:500]}```"
                    ),
                }
            ],
            fallback_text=f"Soniox startup failure — {fallback_used} used for this call",
        )
    except Exception as alert_err:
        logger.warning(f"Failed to send Soniox failure Slack alert: {alert_err}")


async def get_stt_service(
    language_hints: Optional[str] = None, soniox_context: Optional[str] = None
) -> STTServiceResult:
    """
    Returns an STT service instance based on the environment configuration,
    with automatic Deepgram fallback when Soniox fails.

    Args:
        language_hints: Optional language codes (e.g., "en,hi") for STT language recognition.
                       Used with soniox STT service.
        soniox_context: Optional Soniox STT context for speech recognition domain adaptation.
                       If provided, overrides BREEZE_BUDDY_SONIOX_CONTEXT env var.

    Returns:
        STTServiceResult with the service instance, provider name, and fallback status.
        When ENABLE_BREEZE_BUDDY_STT_FALLBACK is true and primary is Soniox,
        fallback_service will contain a pre-built Deepgram STT for mid-call hot-swap.
    """
    if BREEZE_BUDDY_STT_SERVICE == "sarvam":
        if not SARVAM_API_KEY:
            raise ValueError(
                "SARVAM_API_KEY is required when BREEZE_BUDDY_STT_SERVICE=sarvam"
            )

        # Get Breeze Buddy-specific dynamic config values from Redis
        bb_sarvam_stt_model = await BB_SARVAM_STT_MODEL()
        bb_sarvam_stt_language_code = await BB_SARVAM_STT_LANGUAGE_CODE()
        bb_sarvam_stt_prompt = await BB_SARVAM_STT_PROMPT()
        bb_sarvam_stt_vad_signals = await BB_SARVAM_STT_VAD_SIGNALS()
        bb_sarvam_stt_high_vad_sensitivity = await BB_SARVAM_STT_HIGH_VAD_SENSITIVITY()

        # Pass raw config values - model-specific logic is handled internally by build_sarvam_stt
        return STTServiceResult(
            service=build_sarvam_stt(
                SarvamConfig(
                    api_key=SARVAM_API_KEY,
                    model=bb_sarvam_stt_model,
                    sample_rate=SAMPLE_RATE,
                    language_code=bb_sarvam_stt_language_code,
                    prompt=bb_sarvam_stt_prompt,
                    vad_signals=bb_sarvam_stt_vad_signals,
                    high_vad_sensitivity=bb_sarvam_stt_high_vad_sensitivity,
                )
            ),
            provider="sarvam",
        )
    elif BREEZE_BUDDY_STT_SERVICE == "openai":
        if not OPENAI_STT_API_KEY:
            raise ValueError(
                "OPENAI_STT_API_KEY is required when BREEZE_BUDDY_STT_SERVICE=openai"
            )
        logger.info("Using OpenAI STT service for Breeze Buddy voice")
        return STTServiceResult(
            service=build_openai_stt(
                api_key=OPENAI_STT_API_KEY,
                model=OPENAI_STT_MODEL,
                language=Language.EN,
                temperature=0.0,
            ),
            provider="openai",
        )
    elif BREEZE_BUDDY_STT_SERVICE == "soniox":
        if not SONIOX_API_KEY:
            raise ValueError(
                "SONIOX_API_KEY is required when BREEZE_BUDDY_STT_SERVICE=soniox"
            )

        # ── Circuit breaker routing (when fallback is enabled) ────────────
        # OPEN     → Deepgram only (skip Soniox entirely)
        # HALF-OPEN → probe call gets ServiceSwitcher; others get Deepgram
        # CLOSED   → Soniox only (no idle Deepgram WS)
        if ENABLE_BREEZE_BUDDY_STT_FALLBACK:
            from app.ai.voice.agents.breeze_buddy.stt.fallback import (
                CircuitState,
                stt_circuit_breaker,
            )

            circuit_state = await stt_circuit_breaker.get_state()
            logger.info(f"STT circuit breaker state: {circuit_state.value}")

            # ── OPEN: Deepgram only, skip Soniox entirely ─────────────
            if circuit_state == CircuitState.OPEN:
                logger.info("Circuit breaker OPEN — using Deepgram as primary STT")
                try:
                    return STTServiceResult(
                        service=_build_deepgram_fallback(vad_events=False),
                        provider="deepgram",
                        is_fallback=True,
                    )
                except Exception as dg_err:
                    logger.error(f"Deepgram init failed while circuit OPEN: {dg_err}")
                    raise

            # ── HALF-OPEN: single probe call with ServiceSwitcher ─────
            if circuit_state == CircuitState.HALF_OPEN:
                probe_acquired = await stt_circuit_breaker.try_acquire_probe()
                if not probe_acquired:
                    # Another call is already probing — use Deepgram
                    logger.info(
                        "Circuit breaker HALF-OPEN but probe lock held — "
                        "using Deepgram for this call"
                    )
                    try:
                        return STTServiceResult(
                            service=_build_deepgram_fallback(vad_events=False),
                            provider="deepgram",
                            is_fallback=True,
                        )
                    except Exception as dg_err:
                        logger.error(f"Deepgram init failed during HALF-OPEN: {dg_err}")
                        raise

                # Probe call: Soniox primary + Deepgram fallback in ServiceSwitcher
                logger.info(
                    "Circuit breaker HALF-OPEN probe — building Soniox + Deepgram"
                )
                try:
                    stt_service = _build_soniox(language_hints, soniox_context)
                    fallback = _build_deepgram_fallback(vad_events=False)
                    logger.info(
                        "Probe call: Soniox primary + Deepgram fallback "
                        "(ServiceSwitcher, vad_events=False)"
                    )
                    return STTServiceResult(
                        service=stt_service,
                        provider="soniox",
                        fallback_service=fallback,
                        is_probe_call=True,
                    )
                except Exception as probe_err:
                    logger.error(
                        f"Probe call Soniox init failed: {probe_err}. "
                        f"Recording failure and falling back to Deepgram."
                    )
                    # Init-time failure during probe: record + release lock
                    _fire_and_forget(_send_soniox_failure_alert(probe_err, "deepgram"))
                    await stt_circuit_breaker.record_failure()
                    await stt_circuit_breaker.release_probe()
                    try:
                        return STTServiceResult(
                            service=_build_deepgram_fallback(vad_events=False),
                            provider="deepgram",
                            is_fallback=True,
                        )
                    except Exception as dg_err:
                        raise probe_err from dg_err

        # ── CLOSED (or fallback disabled): Soniox only ────────────────
        try:
            stt_service = _build_soniox(language_hints, soniox_context)

            return STTServiceResult(
                service=stt_service,
                provider="soniox",
            )

        except Exception as soniox_err:
            logger.error(
                f"Soniox STT initialization failed, falling back to Deepgram: {soniox_err}"
            )

            # Fire-and-forget Slack alert
            _fire_and_forget(_send_soniox_failure_alert(soniox_err, "deepgram"))

            # Record failure in circuit breaker (may trip for future calls)
            if ENABLE_BREEZE_BUDDY_STT_FALLBACK:
                from app.ai.voice.agents.breeze_buddy.stt.fallback import (
                    stt_circuit_breaker,
                )

                await stt_circuit_breaker.record_failure()

            # Fallback to Deepgram
            try:
                deepgram_service = _build_deepgram_fallback(vad_events=False)
                logger.info(
                    "Successfully initialized Deepgram STT as fallback for Soniox failure"
                )
                return STTServiceResult(
                    service=deepgram_service,
                    provider="deepgram",
                    is_fallback=True,
                )
            except Exception as deepgram_err:
                logger.error(
                    f"Deepgram fallback also failed: {deepgram_err}. "
                    f"Original Soniox error: {soniox_err}"
                )
                # Re-raise the original Soniox error — both providers are down
                raise soniox_err from deepgram_err

    elif BREEZE_BUDDY_STT_SERVICE == "deepgram":
        # Direct Deepgram selection (not as fallback)
        logger.info("Using Deepgram STT service for Breeze Buddy voice")
        return STTServiceResult(
            service=_build_deepgram_fallback(vad_events=False),
            provider="deepgram",
        )
    else:
        logger.info("Using Google STT service with VAD-based turn detection")
        return STTServiceResult(
            service=build_google_stt(credentials_json=GOOGLE_CREDENTIALS_JSON),
            provider="google",
        )
