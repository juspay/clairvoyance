"""Pipeline creation and service initialization for voice agents."""

from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

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
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.azure.llm import AzureLLMService
from pipecat.turns.user_start import (
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import setup_tracing
from app.ai.voice.agents.breeze_buddy.processors import (
    BusyStateKeywordFilter,
    ResponseStateGate,
    create_user_idle_processor,
)
from app.ai.voice.agents.breeze_buddy.stt import get_stt_service
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.ai.voice.agents.breeze_buddy.tts import get_tts_service
from app.core.config.dynamic import (
    BB_ENABLE_RESPONSE_GATE,
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
    if stt_language:
        logger.info(f"Using STT language from template: {stt_language}")

    stt = await get_stt_service(language_hints=stt_language)

    llm = AzureLLMService(
        api_key=AZURE_OPENAI_API_KEY,
        endpoint=AZURE_OPENAI_ENDPOINT,
        model=AZURE_BREEZE_BUDDY_OPENAI_MODEL,
        max_completion_tokens=await BREEZE_BUDDY_AZURE_MAX_COMPLETION_TOKENS(),
        temperature=await BREEZE_BUDDY_AZURE_TEMPERATURE(),
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
    configurations: Optional[ConfigurationModel] = None,
    vad_analyzer: Optional[SileroVADAnalyzer] = None,
) -> tuple[Pipeline, LLMContext, Any]:
    """Build the processing pipeline.

    Args:
        transport: The transport instance
        stt: Speech-to-text service
        llm: LLM service
        tts: Text-to-speech service
        configurations: Template configuration model
        vad_analyzer: VAD analyzer instance (runs inside the user aggregator)

    Returns:
        Tuple of (pipeline, context, context_aggregator)
    """
    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer,
            user_turn_strategies=UserTurnStrategies(
                start=[
                    VADUserTurnStartStrategy(enable_interruptions=False),
                    TranscriptionUserTurnStartStrategy(use_interim=False),
                ],
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0),
                ],
            ),
        ),
    )

    response_gate = ResponseStateGate() if await BB_ENABLE_RESPONSE_GATE() else None

    # Create user idle processor from template configuration
    user_idle_config = getattr(configurations, "user_idle_configuration", None)
    user_idle = (
        create_user_idle_processor(
            enabled=user_idle_config.enabled,
            timeout=user_idle_config.timeout,
            message=user_idle_config.idle_message,
        )
        if user_idle_config is not None
        else None
    )

    # Create keyword filter if enabled in template configuration
    keyword_filter = None
    keyword_filter_config = getattr(configurations, "keyword_filter_config", None)
    if keyword_filter_config and keyword_filter_config.enabled:
        logger.info(
            f"Keyword filter enabled: keywords={keyword_filter_config.keywords}, "
            f"match_mode={keyword_filter_config.match_mode}"
        )
        keyword_filter = BusyStateKeywordFilter(
            keywords=keyword_filter_config.keywords,
            match_mode=keyword_filter_config.match_mode,
            case_sensitive=keyword_filter_config.case_sensitive,
            remove_punctuation=keyword_filter_config.remove_punctuation,
        )

    # Store reference to user aggregator for position lookup
    user_aggregator = context_aggregator.user()

    pipeline_parts = [
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]

    # Insert processors at position 2 (after stt).
    # keyword_filter MUST come before response_gate so filtered transcriptions
    # never reach the gate (which would trigger unnecessary interruptions).
    # Pipeline order: transport.input() -> stt -> keyword_filter -> response_gate -> user_aggregator
    insert_position = 2

    if response_gate:
        pipeline_parts.insert(insert_position, response_gate)

    if keyword_filter:
        pipeline_parts.insert(insert_position, keyword_filter)

    # Insert user idle processor before user_aggregator (context_aggregator.user()) to monitor user activity
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

    return Pipeline(pipeline_parts), context, context_aggregator


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
            allow_interruptions=False,
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
