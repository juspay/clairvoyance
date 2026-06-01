"""Pipeline creation and service initialization for voice agents."""

from datetime import datetime
from typing import Any, Callable, Literal, Optional
from zoneinfo import ZoneInfo

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
    LocalSmartTurnAnalyzerV3,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.observers.loggers.llm_log_observer import LLMLogObserver
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.observers.loggers.transcription_log_observer import (
    TranscriptionLogObserver,
)
from pipecat.observers.turn_tracking_observer import TurnTrackingObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi import (
    RTVIFunctionCallReportLevel,
    RTVIObserverParams,
)
from pipecat.turns.user_mute import AlwaysUserMuteStrategy, BaseUserMuteStrategy
from pipecat.turns.user_start import (
    BaseUserTurnStartStrategy,
    MinWordsUserTurnStartStrategy,
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import (
    BaseUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.ai.voice.agents.breeze_buddy.llm import get_llm_service
from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import setup_tracing
from app.ai.voice.agents.breeze_buddy.processors import (
    TranscriptCollectorProcessor,
    TranscriptionGateProcessor,
    UserIdleCallbackHandler,
)
from app.ai.voice.agents.breeze_buddy.stt import get_stt_service
from app.ai.voice.agents.breeze_buddy.template.interruption import (
    AccumulatingSpeechTimeoutStrategy,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    InterruptionConfig,
    InterruptionMode,
    SmartTurnConfig,
    TurnDetectionMode,
)
from app.ai.voice.agents.breeze_buddy.template.vad import TELEPHONY_SAMPLE_RATE
from app.ai.voice.agents.breeze_buddy.tts import (
    TTSServiceResult,
    get_tts_service_with_fallback,
    resolve_voice_config,
)
from app.ai.voice.llm.realtime import get_realtime_llm_service
from app.core.config.static import (
    ENABLE_BREEZE_BUDDY_DAILY_EVENTS,
    ENABLE_BREEZE_BUDDY_TRACING,
    ENVIRONMENT,
)
from app.core.logger import logger


def get_observers() -> list[Any]:
    """Get pipeline observers for dev environment.

    Note: pipecat's ``UserBotLatencyObserver`` is intentionally not attached.
    Its per-turn ``on_latency_measured`` event is anchored on
    ``VADUserStoppedSpeakingFrame``, which is only emitted when a VAD
    analyzer is wired into the user aggregator. Production runs with
    ``BREEZE_BUDDY_ENABLE_VAD=false``, so the observer would emit at most a
    single greeting-latency event per call — misleading as a "latency"
    signal. See TODO.md §2 for follow-up.
    """
    if ENVIRONMENT.lower() != "dev":
        return []
    return [
        MetricsLogObserver(),
        LLMLogObserver(),
        TranscriptionLogObserver(),
        TurnTrackingObserver(),
    ]


def generate_conversation_id(payload: Optional[dict]) -> str:
    """Generate a trace name for Langfuse display from lead payload."""
    ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    timestamp = ist_time.strftime("%Y-%m-%d_%H-%M-%S")
    if payload is None:
        return f"unknown-unknown-{timestamp}"
    customer_name = payload.get("customer_name", "unknown")
    shop_name = payload.get("shop_name", "unknown")
    return f"{customer_name}-{shop_name}-{timestamp}"


async def create_services(
    configurations: Optional[ConfigurationModel],
    include_llm: bool = True,
) -> tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Create STT, LLM, and TTS services.

    Args:
        configurations: Template configuration model
        include_llm: When False, skip LLM creation (stream mode). LLM will be None.

    Returns:
        Tuple of (stt_service, llm_service_or_None, tts_service). For realtime
        / speech-to-speech LLMs, both ``stt_service`` and ``tts_service`` are
        ``None`` because the realtime LLM handles audio in/out natively.
    """
    llm_config = getattr(configurations, "llm_configurations", None)
    is_realtime = bool(include_llm and llm_config and llm_config.realtime is not None)

    if is_realtime:
        # Realtime / speech-to-speech: one service replaces STT + LLM + TTS.
        # Skip the separate text-LLM, STT, and TTS services entirely so the
        # pipeline can wire the realtime service directly between transport
        # input/output and the context aggregators.
        assert llm_config is not None  # narrowed by is_realtime
        realtime_llm = await get_realtime_llm_service(llm_config)
        logger.info(
            "[REALTIME] Skipping separate STT and TTS service creation; "
            "realtime LLM handles audio natively"
        )
        return None, realtime_llm, None

    stt_configuration = getattr(configurations, "stt_configuration", None)

    if stt_configuration:
        logger.info(
            f"Using template STT configuration: provider={stt_configuration.provider.value}"
        )
        stt_result = await get_stt_service(stt_configuration=stt_configuration)
    else:
        # Legacy path: build from scattered fields
        stt_language = getattr(configurations, "stt_language", None)
        if isinstance(stt_language, list):
            stt_language = ",".join(stt_language)
        soniox_context = getattr(configurations, "soniox_context", None)
        if stt_language:
            logger.info(f"Using STT language from template: {stt_language}")
        if soniox_context:
            logger.info("Using Soniox context from template")
        stt_result = await get_stt_service(
            language_hints=stt_language, soniox_context=soniox_context
        )

    if include_llm:
        llm = await get_llm_service(llm_config)
    else:
        llm = None
        logger.info("[STREAM] Skipping LLM service creation")

    template_voice_config = getattr(configurations, "tts_configuration", None)
    voice_config_overrides = getattr(
        configurations, "tts_configuration_overrides", None
    )
    voice_config = await resolve_voice_config(
        template_voice_config, voice_config_overrides
    )
    logger.info(f"Resolved voice config: provider={voice_config.provider.value}")
    tts_result: TTSServiceResult = await get_tts_service_with_fallback(voice_config)

    return stt_result, llm, tts_result


def _wire_user_idle_event(
    user_aggregator: Any, handler: Optional[UserIdleCallbackHandler]
) -> None:
    """Bind the on_user_turn_idle event on the aggregator to the handler.

    Pipecat 1.0 owns the idle timer inside ``LLMUserAggregator`` and emits
    ``on_user_turn_idle`` when it fires; the handler decides whether to
    nudge the user, retry, or end the call.
    """
    if handler is None:
        return
    idle_handler = handler  # narrow Optional for the closure below

    @user_aggregator.event_handler("on_user_turn_idle")
    async def _on_user_turn_idle(aggregator: Any) -> None:
        await idle_handler.handle_user_idle(aggregator)


async def build_pipeline(
    transport: Any,
    stt: Optional[Any],
    llm: Optional[Any],
    tts: Optional[Any],
    vad_analyzer: Optional[SileroVADAnalyzer] = None,
    configurations: Optional[ConfigurationModel] = None,
    on_user_idle_timeout: Optional[Callable[[int], Any]] = None,
    mode: Literal["agent", "stream"] = "agent",
) -> tuple[
    Pipeline,
    LLMContext,
    Any,
    Optional[UserIdleCallbackHandler],
    Optional[TranscriptionGateProcessor],
    Optional[TranscriptCollectorProcessor],
]:
    """Build the processing pipeline.

    Uses the universal LLMContextAggregatorPair with UserTurnStrategies:
    - Start: VADUserTurnStartStrategy (primary, ~100ms) + TranscriptionUserTurnStartStrategy
      (fallback for soft speech VAD misses, uses interim transcriptions)
    - Stop: AccumulatingSpeechTimeoutStrategy (user_speech_timeout=0.0) — triggers
      immediately when Soniox sends a finalized transcript (after its own
      max_endpoint_delay_ms semantic endpoint detection).
    - VAD runs inside the aggregator (not the transport)

    Stream mode (mode="stream") reuses the same aggregator + turn strategies
    (so client gets high-quality user-started/stopped speaking events) but
    skips the LLM service, the assistant aggregator, and user-idle monitoring.
    A TranscriptCollectorProcessor is inserted after the user aggregator to
    capture transcriptions for DB storage. Client drives TTS via TTSSpeakFrame.

    Args:
        transport: The transport instance
        stt: Speech-to-text service (None in realtime mode — the realtime
            LLM handles audio input natively)
        llm: LLM service (None in stream mode)
        tts: Text-to-speech service (None in realtime mode — the realtime
            LLM handles audio output natively)
        vad_analyzer: SileroVADAnalyzer instance for voice activity detection
        configurations: Template configuration model
        on_user_idle_timeout: Async callback to handle user idle timeout (triggers full end_conversation flow)
        mode: "agent" for full LLM-driven flow, "stream" for STT/TTS-only with turn events

    Returns:
        6-tuple of (pipeline, context, context_aggregator, user_idle_callback_handler, transcription_gate, transcript_collector)
        - pipeline: the built Pipeline instance
        - context: the LLMContext for the conversation
        - context_aggregator: LLMContextAggregatorPair for managing user/assistant turns
        - user_idle_callback_handler: resets retry count on user activity; None if idle detection is disabled or stream mode
        - transcription_gate: TranscriptionGateProcessor instance wired into the pipeline
        - transcript_collector: TranscriptCollectorProcessor instance (stream mode only, None in agent mode)
    """
    is_stream = mode == "stream"
    # Realtime / speech-to-speech: when create_services returns no STT and no
    # TTS but a single LLM, we wire a minimal pipeline. The realtime LLM does
    # its own audio in/out, transcription, and turn detection, so all of BB's
    # STT-side scaffolding (TranscriptionGateProcessor, smart-turn analyzer,
    # MinWords/Transcription user-turn-start strategies, AccumulatingSpeech
    # TimeoutStrategy) is irrelevant or duplicative and is skipped.
    is_realtime = stt is None and tts is None and llm is not None and not is_stream

    # TODO: Add a breeze-buddy-specific context summarizer.
    # Pipecat does not provide built-in summarization; implement one under
    # app/ai/voice/agents/breeze_buddy/ to manage long conversation contexts.
    context = LLMContext()

    # User-idle resolution is shared between realtime and standard pipelines:
    # both paths use the same aggregator-driven idle detection (timer lives
    # inside LLMUserAggregator; handler fires on on_user_turn_idle). Stream
    # mode skips idle entirely — client drives the turn lifecycle.
    user_idle_config = getattr(configurations, "user_idle_configuration", None)
    user_idle_enabled = (
        user_idle_config is not None
        and getattr(user_idle_config, "enabled", False)
        and not is_stream
    )
    user_idle_timeout = (
        float(user_idle_config.timeout)
        if user_idle_enabled and user_idle_config is not None
        else 0.0
    )
    user_idle_callback_handler: Optional[UserIdleCallbackHandler] = (
        UserIdleCallbackHandler(
            idle_message=user_idle_config.idle_message,
            max_retries=user_idle_config.max_retries,
            on_user_idle_timeout=on_user_idle_timeout,
        )
        if user_idle_enabled and user_idle_config is not None
        else None
    )

    if is_realtime:
        # Aggregator with no custom strategies; the realtime LLM emits
        # TranscriptionFrame on its own based on server-side STT, and emits
        # UserStartedSpeakingFrame / UserStoppedSpeakingFrame from server-side
        # turn detection. user_idle still works because on_user_turn_idle
        # fires on the aggregator regardless of upstream source.
        context_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(user_idle_timeout=user_idle_timeout),
        )
        user_aggregator = context_aggregator.user()
        _wire_user_idle_event(user_aggregator, user_idle_callback_handler)

        # Pipeline order (realtime):
        #   input → user_aggregator → realtime_llm → output → assistant_aggregator
        pipeline_parts: list[Any] = [
            transport.input(),
            user_aggregator,
            llm,
            transport.output(),
            context_aggregator.assistant(),
        ]
        logger.info(
            "[REALTIME] Built pipeline without STT/TTS/transcription_gate; "
            f"user_idle_enabled={user_idle_enabled}"
        )
        return (
            Pipeline(pipeline_parts),
            context,
            context_aggregator,
            user_idle_callback_handler,
            None,  # no TranscriptionGateProcessor in realtime mode
            None,  # no TranscriptCollectorProcessor (stream-mode only)
        )

    # --- Interruption configuration ---
    # Reads template-level interruption config to select PipeCat strategies:
    #   mode=enabled (default): normal interruptions via VAD + Transcription
    #   mode=enabled + min_words: MinWordsUserTurnStartStrategy replaces Transcription
    #   mode=disabled_discard: AlwaysUserMuteStrategy drops all user frames while bot speaks
    interruption_config = (
        getattr(configurations, "interruption", None) or InterruptionConfig()
    )

    # --- Turn detection (from stt_configuration) ---
    stt_config = getattr(configurations, "stt_configuration", None)
    turn_detection_mode = (
        stt_config.turn_detection if stt_config else TurnDetectionMode.STT_NATIVE
    )
    # STT_NATIVE fires immediately (0.0s). Only TIMEOUT honours configured value.
    # The STTConfiguration validator already enforces this, but be explicit here.
    user_speech_timeout = (
        stt_config.user_speech_timeout
        if stt_config and turn_detection_mode == TurnDetectionMode.TIMEOUT
        else 0.0
    )

    # SmartTurn mode: auto-create Silero VAD (stop_secs=0.2) as trigger if no
    # external VAD was provided.  SmartTurn needs is_speech signal from VAD.
    # When no external VAD exists this is the telephony path (8 kHz sample rate).
    if turn_detection_mode == TurnDetectionMode.SMART_TURN and vad_analyzer is None:
        vad_analyzer = SileroVADAnalyzer(
            sample_rate=TELEPHONY_SAMPLE_RATE,
            params=VADParams(stop_secs=0.2),
        )
        logger.info(
            "SmartTurn mode: auto-created Silero VAD (stop_secs=0.2) as trigger"
        )

    # --- User turn start strategies ---
    start_strategies: list[BaseUserTurnStartStrategy] = []
    if vad_analyzer is not None:
        start_strategies.append(VADUserTurnStartStrategy())

    if (
        interruption_config.min_words
        and interruption_config.mode == InterruptionMode.ENABLED
    ):
        start_strategies.append(
            MinWordsUserTurnStartStrategy(
                min_words=interruption_config.min_words, use_interim=True
            )
        )
        logger.info(
            f"Interruption: min_words={interruption_config.min_words} strategy enabled"
        )
    else:
        start_strategies.append(TranscriptionUserTurnStartStrategy(use_interim=True))

    # --- User turn stop strategy (driven by stt_configuration.turn_detection) ---
    stop_strategies: list[BaseUserTurnStopStrategy] = []
    if turn_detection_mode == TurnDetectionMode.SMART_TURN:
        st = (
            stt_config.smart_turn
            if stt_config and stt_config.smart_turn
            else SmartTurnConfig()
        )
        smart_turn_analyzer = LocalSmartTurnAnalyzerV3(
            cpu_count=st.cpu_count,
            params=SmartTurnParams(
                stop_secs=st.stop_secs,
                pre_speech_ms=st.pre_speech_ms,
                max_duration_secs=st.max_duration_secs,
            ),
        )
        stop_strategies = [
            TurnAnalyzerUserTurnStopStrategy(turn_analyzer=smart_turn_analyzer)
        ]
        logger.info(
            "Turn detection: SmartTurn ML (stop_secs={}, pre_speech_ms={}, "
            "max_duration_secs={}, cpu_count={})",
            st.stop_secs,
            st.pre_speech_ms,
            st.max_duration_secs,
            st.cpu_count,
        )
    else:
        # TIMEOUT and STT_NATIVE are the same strategy — only the timeout differs.
        # STT_NATIVE uses 0.0 (fires immediately on finalized transcript, e.g. Soniox <end> token).
        # TIMEOUT uses user_speech_timeout (waits after last transcript, resets on each new one).
        stop_strategies = [
            AccumulatingSpeechTimeoutStrategy(user_speech_timeout=user_speech_timeout)
        ]
        logger.info(
            f"Turn detection: {turn_detection_mode.value} (user_speech_timeout={user_speech_timeout}s)"
        )

    user_turn_strategies = UserTurnStrategies(
        start=start_strategies,
        stop=stop_strategies,
    )

    # User mute strategies:
    # disabled_discard → AlwaysUserMuteStrategy: drops all user frames while bot speaks,
    # including InterruptionFrame, VAD frames, transcription frames, and raw audio.
    user_mute_strategies: list[BaseUserMuteStrategy] = []
    if interruption_config.mode == InterruptionMode.DISABLED_DISCARD:
        user_mute_strategies.append(AlwaysUserMuteStrategy())
        logger.info("Interruption: mode=disabled_discard — user muted while bot speaks")
    else:
        logger.info(
            f"Interruption: mode={interruption_config.mode.value} — interruptions enabled"
        )

    # The aggregator owns the idle timer (user_idle_timeout above); the
    # callback handler is wired via on_user_turn_idle below. Both were
    # resolved before the realtime branch.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=user_turn_strategies,
            user_mute_strategies=user_mute_strategies,
            vad_analyzer=vad_analyzer,
            user_idle_timeout=user_idle_timeout,
        ),
    )

    # TranscriptionGateProcessor is always in the pipeline.
    # It is a transparent passthrough when neither mute nor keyword filter is active.
    keyword_filter_config = getattr(configurations, "keyword_filter", None)
    transcription_gate = TranscriptionGateProcessor(
        keyword_filter_config=keyword_filter_config
    )
    if keyword_filter_config and keyword_filter_config.enabled:
        logger.info(
            f"TranscriptionGate: keyword filter enabled with "
            f"{len(keyword_filter_config.keywords)} keyword(s), "
            f"match_type={keyword_filter_config.match_type.value}"
        )

    # Store reference to user aggregator for position lookup
    user_aggregator = context_aggregator.user()
    _wire_user_idle_event(user_aggregator, user_idle_callback_handler)

    # Stream mode: insert TranscriptCollectorProcessor to capture user + bot
    # TTS text for DB storage (no LLMContext to pull from at end-of-conversation).
    transcript_collector: Optional[TranscriptCollectorProcessor] = None
    if is_stream:
        transcript_collector = TranscriptCollectorProcessor()

    # Pipeline order:
    #   agent mode:  input → stt → gate → user_aggregator → llm → tts → output → assistant_aggregator
    #   stream mode: input → stt → gate → transcript_collector → user_aggregator → tts → output
    #
    # Collector sits BEFORE user_aggregator because the aggregator swallows
    # TranscriptionFrame (handled internally, not pushed downstream). TTSSpeakFrames
    # injected via task.queue_frame() enter at the top and flow through the collector
    # (captured as "assistant") before hitting TTS.
    # Pipecat's LLMUserAggregator natively handles interruptions via
    # UserTurnStrategies — no custom response gate needed.
    # Note: RTVIProcessor is added automatically by PipelineTask (pipecat v0.0.102+)
    # when enable_rtvi=True (default). No need to add it to the pipeline manually.
    pipeline_parts: list[Any] = [transport.input(), stt, transcription_gate]
    if is_stream:
        assert transcript_collector is not None
        pipeline_parts.append(transcript_collector)
    pipeline_parts.append(user_aggregator)
    if is_stream:
        pipeline_parts.extend([tts, transport.output()])
    else:
        pipeline_parts.extend(
            [llm, tts, transport.output(), context_aggregator.assistant()]
        )

    return (
        Pipeline(pipeline_parts),
        context,
        context_aggregator,
        user_idle_callback_handler,
        transcription_gate,
        transcript_collector,
    )


async def create_pipeline_task(
    pipeline: Pipeline,
    conversation_id: str,
    is_daily_mode: bool = False,
) -> PipelineTask:
    """Create and configure the pipeline task.

    Args:
        pipeline: The built pipeline
        conversation_id: Unique conversation identifier
        is_daily_mode: When True, configures RTVIObserver params for real-time event emission

    Returns:
        Configured PipelineTask
    """
    # Pipecat v0.0.102+ automatically adds RTVIProcessor and RTVIObserver
    # when enable_rtvi=True (default). We just configure the observer params.
    emit_daily_events = is_daily_mode and ENABLE_BREEZE_BUDDY_DAILY_EVENTS
    rtvi_params = (
        RTVIObserverParams(
            user_transcription_enabled=True,
            user_speaking_enabled=True,
            user_mute_enabled=True,
            user_audio_level_enabled=True,
            bot_llm_enabled=True,
            bot_tts_enabled=True,
            bot_speaking_enabled=True,
            bot_output_enabled=True,
            bot_audio_level_enabled=True,
            metrics_enabled=True,
            function_call_report_level={
                "*": RTVIFunctionCallReportLevel.FULL,
            },
        )
        if emit_daily_events
        else None
    )

    if emit_daily_events:
        logger.info("RTVI daily events enabled with full function call reporting")

    task_params: dict[str, Any] = {
        "params": PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        "observers": get_observers(),
        "enable_rtvi": emit_daily_events,
        "rtvi_observer_params": rtvi_params,
    }

    if ENABLE_BREEZE_BUDDY_TRACING:
        setup_tracing("breeze-buddy")
        task_params["conversation_id"] = conversation_id
        task_params["enable_tracing"] = True

    return PipelineTask(pipeline, **task_params)
