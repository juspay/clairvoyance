"""Pipeline creation and service initialization for voice agents."""

from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.observers.loggers.llm_log_observer import LLMLogObserver
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.observers.loggers.transcription_log_observer import (
    TranscriptionLogObserver,
)
from pipecat.observers.loggers.user_bot_latency_log_observer import (
    UserBotLatencyLogObserver,
)
from pipecat.observers.turn_tracking_observer import TurnTrackingObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.openai.base_llm import BaseOpenAILLMService
from pipecat.turns.user_mute import AlwaysUserMuteStrategy, BaseUserMuteStrategy
from pipecat.turns.user_start import (
    BaseUserTurnStartStrategy,
    MinWordsUserTurnStartStrategy,
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import setup_tracing
from app.ai.voice.agents.breeze_buddy.processors import (
    BackChannelProcessor,
    TranscriptionGateProcessor,
    UserIdleCallbackHandler,
    create_user_idle_processor,
)
from app.ai.voice.agents.breeze_buddy.stt import get_stt_service
from app.ai.voice.agents.breeze_buddy.template.interruption import (
    AccumulatingSpeechTimeoutStrategy,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    InterruptionConfig,
    InterruptionMode,
)
from app.ai.voice.agents.breeze_buddy.tts import get_tts_service
from app.core.config.dynamic import (
    BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS,
    BREEZE_BUDDY_AZURE_TEMPERATURE,
)
from app.core.config.static import (
    AZURE_BREEZE_BUDDY_OPENAI_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    ENABLE_BREEZE_BUDDY_TRACING,
    ENVIRONMENT,
)
from app.core.logger import logger


def get_observers() -> list[Any]:
    """Get pipeline observers for dev environment."""
    if ENVIRONMENT.lower() != "dev":
        return []
    return [
        MetricsLogObserver(),
        LLMLogObserver(),
        TranscriptionLogObserver(),
        UserBotLatencyLogObserver(),
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
) -> tuple[Any, AzureLLMService, Any]:
    """Create STT, LLM, and TTS services.

    Args:
        configurations: Template configuration model

    Returns:
        Tuple of (stt_service, llm_service, tts_service)
    """
    stt_language = getattr(configurations, "stt_language", None)
    soniox_context = getattr(configurations, "soniox_context", None)
    if stt_language:
        logger.info(f"Using STT language from template: {stt_language}")
    if soniox_context:
        logger.info(f"Using Soniox context from template")

    stt = await get_stt_service(
        language_hints=stt_language, soniox_context=soniox_context
    )

    # TODO: Add retry_on_timeout=True, retry_timeout_secs=3.0 to reduce P99 tail latency (500-1500ms).
    #       These are valid top-level params on BaseOpenAILLMService. Needs testing before enabling.
    # TODO: Override create_client() to add httpx connection pooling (keepalive_expiry=None,
    #       max_keepalive_connections=100) to avoid TCP+TLS cold-start on first request (50-200ms).
    #       AzureLLMService.create_client() currently creates AsyncAzureOpenAI without custom http_client.
    llm = AzureLLMService(
        api_key=AZURE_OPENAI_API_KEY,
        endpoint=AZURE_OPENAI_ENDPOINT,
        model=AZURE_BREEZE_BUDDY_OPENAI_MODEL,
        params=BaseOpenAILLMService.InputParams(
            max_completion_tokens=await BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS(),
            temperature=await BREEZE_BUDDY_AZURE_TEMPERATURE(),
            service_tier="auto",
        ),
    )

    # Extract Cartesia voice configurations from template
    cartesia_voice_config = getattr(
        configurations, "cartesia_voice_configurations", None
    )
    legacy_mira_voice_id = getattr(configurations, "mira_voice_id", None)

    if cartesia_voice_config:
        logger.info(
            f"Using Cartesia voice configurations from template: {cartesia_voice_config}"
        )

    # Extract ElevenLabs voice configurations from template
    elevenlabs_voice_config = getattr(
        configurations, "elevenlabs_voice_configurations", None
    )

    if elevenlabs_voice_config:
        logger.info(
            f"Using ElevenLabs voice configurations from template: {elevenlabs_voice_config}"
        )

    tts = await get_tts_service(
        voice_name=getattr(configurations, "tts_voice_name", None),
        mira_voice_id=legacy_mira_voice_id,
        cartesia_voice_configurations=cartesia_voice_config,
        elevenlabs_voice_configurations=elevenlabs_voice_config,
    )

    return stt, llm, tts


async def build_pipeline(
    transport: Any,
    stt: Any,
    llm: AzureLLMService,
    tts: Any,
    vad_analyzer: Optional[SileroVADAnalyzer] = None,
    configurations: Optional[ConfigurationModel] = None,
    on_user_idle_timeout: Optional[Callable[[int], Any]] = None,
) -> tuple[
    Pipeline,
    LLMContext,
    Any,
    Optional[UserIdleCallbackHandler],
    TranscriptionGateProcessor,
    BackChannelProcessor,
]:
    """Build the processing pipeline.

    Uses the universal LLMContextAggregatorPair with UserTurnStrategies:
    - Start: VADUserTurnStartStrategy (primary, ~100ms) + TranscriptionUserTurnStartStrategy
      (fallback for soft speech VAD misses, uses interim transcriptions)
    - Stop: AccumulatingSpeechTimeoutStrategy (user_speech_timeout=0.0) — triggers
      immediately when Soniox sends a finalized transcript (after its own
      max_endpoint_delay_ms semantic endpoint detection).
    - VAD runs inside the aggregator (not the transport)

    Args:
        transport: The transport instance
        stt: Speech-to-text service
        llm: LLM service
        tts: Text-to-speech service
        vad_analyzer: SileroVADAnalyzer instance for voice activity detection
        configurations: Template configuration model
        on_user_idle_timeout: Async callback to handle user idle timeout (triggers full end_conversation flow)

    Returns:
        5-tuple of (pipeline, context, context_aggregator, user_idle_callback_handler, transcription_gate)
        - pipeline: the built Pipeline instance
        - context: the LLMContext for the conversation
        - context_aggregator: LLMContextAggregatorPair for managing user/assistant turns
        - user_idle_callback_handler: resets retry count on user activity; None if idle detection is disabled
        - transcription_gate: TranscriptionGateProcessor instance wired into the pipeline
    """
    # TODO: Add a breeze-buddy-specific context summarizer.
    # Pipecat does not provide built-in summarization; implement one under
    # app/ai/voice/agents/breeze_buddy/ to manage long conversation contexts.
    context = LLMContext()

    # --- Interruption configuration ---
    # Reads template-level interruption config to select PipeCat strategies:
    #   mode=enabled (default): normal interruptions via VAD + Transcription
    #   mode=enabled + min_words: MinWordsUserTurnStartStrategy replaces Transcription
    #   mode=disabled_discard: AlwaysUserMuteStrategy drops all user frames while bot speaks
    interruption_config = (
        getattr(configurations, "interruption", None) or InterruptionConfig()
    )

    # User turn start strategies:
    # 1. VADUserTurnStartStrategy: Primary detector, fires on VAD speech detection (~100ms).
    #    Only included when vad_analyzer is provided (BREEZE_BUDDY_ENABLE_VAD=true).
    #    First-one-wins semantics — if VAD fires first, transcription fallback is skipped.
    # 2. TranscriptionUserTurnStartStrategy: Used as sole start strategy when VAD is disabled,
    #    or as fallback for soft speech that VAD misses when VAD is enabled.
    #    With use_interim=True, triggers on any interim transcription from Soniox.
    # 3. MinWordsUserTurnStartStrategy: Replaces Transcription strategy when min_words is set.
    #    Requires N words before triggering interruption while bot speaks; 1 word when bot is silent.
    # 4. AccumulatingSpeechTimeoutStrategy(0.0): Triggers immediately when Soniox
    #    sends a finalized transcript with <end> token (native semantic endpoint detection).
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

    user_turn_strategies = UserTurnStrategies(
        start=start_strategies,
        stop=[
            AccumulatingSpeechTimeoutStrategy(user_speech_timeout=0.0),
        ],
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

    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=user_turn_strategies,
            user_mute_strategies=user_mute_strategies,
            vad_analyzer=vad_analyzer,
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

    # BackChannelProcessor is always in the pipeline.
    # It is a transparent passthrough when not enabled for the current node.
    back_channel = BackChannelProcessor()

    # Create user idle processor from template configuration
    user_idle_config = getattr(configurations, "user_idle_configuration", None)
    user_idle_result = (
        create_user_idle_processor(
            enabled=user_idle_config.enabled,
            timeout=user_idle_config.timeout,
            message=user_idle_config.idle_message,
            max_retries=user_idle_config.max_retries,
            on_user_idle_timeout=on_user_idle_timeout,
        )
        if user_idle_config is not None
        else None
    )

    # Unpack result - returns (processor, callback_handler) or None
    user_idle = user_idle_result[0] if user_idle_result else None
    user_idle_callback_handler = user_idle_result[1] if user_idle_result else None

    # Store reference to user aggregator for position lookup
    user_aggregator = context_aggregator.user()

    # Order: stt → transcription_gate → back_channel → user_aggregator → llm → tts
    # Pipecat's LLMUserAggregator natively handles interruptions via
    # UserTurnStrategies — no custom response gate needed.
    pipeline_parts = [
        transport.input(),
        stt,
        transcription_gate,
        back_channel,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]

    # Insert user idle processor before user_aggregator to monitor user activity
    if user_idle:
        try:
            user_aggregator_idx = pipeline_parts.index(user_aggregator)
            pipeline_parts.insert(user_aggregator_idx, user_idle)
        except ValueError as e:
            # This should never happen since we explicitly added user_aggregator above
            logger.error(
                f"Failed to find user aggregator in pipeline: {e}. User idle detection disabled."
            )
            # Don't insert user_idle - it's safer to disable the feature than insert at wrong position

    return (
        Pipeline(pipeline_parts),
        context,
        context_aggregator,
        user_idle_callback_handler,
        transcription_gate,
        back_channel,
    )


async def create_pipeline_task(
    pipeline: Pipeline,
    conversation_id: str,
) -> PipelineTask:
    """Create and configure the pipeline task.

    Args:
        pipeline: The built pipeline
        conversation_id: Unique conversation identifier

    Returns:
        Configured PipelineTask
    """
    task_params: dict[str, Any] = {
        "params": PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            observers=get_observers(),
        ),
    }

    if ENABLE_BREEZE_BUDDY_TRACING:
        setup_tracing("breeze-buddy")
        task_params["conversation_id"] = conversation_id
        task_params["enable_tracing"] = True

    return PipelineTask(pipeline, **task_params)
