from typing import Optional

from deepgram import LiveOptions
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.soniox.stt import SonioxInputParams, SonioxSTTService
from pipecat.transcriptions.language import Language

from app.agents.voice.automatic.types import VoiceName
from app.core import config
from app.core.logger import logger


def get_provider_for_attempt(fallback_attempt: int) -> Optional[str]:
    """
    Get the STT provider to use for the given fallback attempt.

    Args:
        fallback_attempt: 0 for primary provider, 1+ for fallback providers

    Returns:
        Provider name or None if all providers exhausted
    """
    if fallback_attempt == 0:
        return config.STT_PROVIDER

    fallback_index = fallback_attempt - 1
    if fallback_index < len(config.STT_FALLBACK_PROVIDERS):
        return config.STT_FALLBACK_PROVIDERS[fallback_index]

    return None


def create_assemblyai_service():
    """Create AssemblyAI STT service instance."""
    if not config.ASSEMBLYAI_API_KEY:
        raise ValueError("ASSEMBLYAI_API_KEY is required for AssemblyAI STT service")

    logger.info("Creating AssemblyAI STT service with Silero VAD-based turn detection")
    return AssemblyAISTTService(
        api_key=config.ASSEMBLYAI_API_KEY,
        vad_force_turn_endpoint=True,
    )


def create_openai_service():
    """Create OpenAI STT service instance."""
    if not config.OPENAI_STT_API_KEY:
        raise ValueError("OPENAI_STT_API_KEY is required for OpenAI STT service")

    logger.info(
        f"Creating OpenAI STT service ({config.OPENAI_STT_MODEL}) with Silero VAD-based turn detection"
    )
    return OpenAISTTService(
        api_key=config.OPENAI_STT_API_KEY,
        model=config.OPENAI_STT_MODEL,
        language=Language.EN,
        prompt=config.AUTOMATIC_OPENAI_STT_PROMPT,
        temperature=0.0,
    )


def create_deepgram_service():
    """Create Deepgram STT service instance."""
    if not config.DEEPGRAM_API_KEY:
        raise ValueError("DEEPGRAM_API_KEY is required for Deepgram STT service")

    # Determine language configuration based on settings
    if config.DEEPGRAM_AUTO_DETECT_LANGUAGE:
        language_config = "multi"
    else:
        language_config = config.DEEPGRAM_LANGUAGE

    # Configure Deepgram with smart turn detection and audio enhancement
    live_options = LiveOptions(
        model=config.DEEPGRAM_MODEL,
        language=language_config,
        smart_format=config.DEEPGRAM_SMART_FORMAT,
        punctuate=config.DEEPGRAM_PUNCTUATE,
        endpointing=config.DEEPGRAM_ENDPOINTING,
        vad_events=config.DEEPGRAM_VAD_EVENTS,
        utterance_end_ms=config.DEEPGRAM_UTTERANCE_END_MS,
        no_delay=config.DEEPGRAM_NO_DELAY,
        interim_results=True,
        profanity_filter=config.DEEPGRAM_PROFANITY_FILTER,
        numerals=config.DEEPGRAM_NUMERALS,
        diarize=config.DEEPGRAM_DIARIZE,
    )

    logger.info(
        f"Creating Deepgram STT service with model: {config.DEEPGRAM_MODEL}, "
        f"language: {language_config} "
        f"(VAD: {config.DEEPGRAM_VAD_EVENTS}, Endpointing: {config.DEEPGRAM_ENDPOINTING})"
    )
    return DeepgramSTTService(
        api_key=config.DEEPGRAM_API_KEY, live_options=live_options
    )


def create_soniox_service():
    """Create Soniox STT service instance."""
    if not config.SONIOX_API_KEY:
        raise ValueError("SONIOX_API_KEY is required for Soniox STT service")

    # Parse language hints from comma-separated string
    language_hints = None
    if config.SONIOX_LANGUAGE_HINTS:
        lang_list = [lang.strip() for lang in config.SONIOX_LANGUAGE_HINTS.split(",")]
        language_hints = [Language(lang) for lang in lang_list if lang]

    # Configure Soniox with supported parameters only
    soniox_params = SonioxInputParams(
        model=config.SONIOX_MODEL,
        language_hints=language_hints,
        context=config.SONIOX_CONTEXT if config.SONIOX_CONTEXT else None,
        enable_non_final_tokens=config.SONIOX_ENABLE_NON_FINAL_TOKENS,
        max_non_final_tokens_duration_ms=(
            config.SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS
            if config.SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS > 0
            else None
        ),
        client_reference_id=None,
    )

    logger.info(
        f"Creating Soniox STT service with model: {config.SONIOX_MODEL}, "
        f"language_hints: {config.SONIOX_LANGUAGE_HINTS}, "
        f"VAD force endpoint: {config.SONIOX_VAD_FORCE_TURN_ENDPOINT}, "
        f"non_final_tokens: {config.SONIOX_ENABLE_NON_FINAL_TOKENS}"
    )
    return SonioxSTTService(
        api_key=config.SONIOX_API_KEY,
        params=soniox_params,
        vad_force_turn_endpoint=config.SONIOX_VAD_FORCE_TURN_ENDPOINT,
    )


def create_google_service():
    """Create Google STT service instance."""
    logger.info("Creating Google STT service with VAD-based turn detection")
    return GoogleSTTService(
        params=GoogleSTTService.InputParams(
            languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False
        ),
        credentials=config.GOOGLE_CREDENTIALS_JSON,
    )


def create_provider_service(provider: str):
    """
    Create STT service instance for the specified provider.

    Args:
        provider: STT provider name (assemblyai, openai, deepgram, soniox, google)

    Returns:
        STT service instance

    Raises:
        ValueError: If provider is invalid or missing required configuration
    """
    provider = provider.lower()

    if provider == "assemblyai":
        return create_assemblyai_service()
    elif provider == "openai":
        return create_openai_service()
    elif provider == "deepgram":
        return create_deepgram_service()
    elif provider == "soniox":
        return create_soniox_service()
    elif provider == "google":
        return create_google_service()
    else:
        raise ValueError(f"Unknown STT provider: {provider}")


def get_stt_service(voice_name: Optional[str] = None, fallback_attempt: int = 0):
    """
    Returns an STT service instance based on configuration and fallback settings.

    Args:
        voice_name: Voice name to determine STT provider override for specific voices
        fallback_attempt: 0 for primary provider, 1+ for fallback providers

    Returns:
        STT service instance

    Raises:
        ValueError: If provider configuration is invalid or all providers exhausted
    """
    # Handle MIA voice with OpenAI override (always use OpenAI, no fallback)
    if voice_name == VoiceName.MIA.value and config.ENABLE_OPENAI_FOR_MIA:
        if not config.OPENAI_STT_API_KEY:
            raise ValueError(
                "OPENAI_STT_API_KEY is required when ENABLE_OPENAI_FOR_MIA=true and voice is MIA"
            )

        logger.info(
            f"Using OpenAI STT service for MIA voice (override enabled) with model: {config.ENFORCED_OPENAI_STT_MODEL}"
        )
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.ENFORCED_OPENAI_STT_MODEL,
            language=Language.EN,
            prompt=config.AUTOMATIC_OPENAI_STT_PROMPT,
            temperature=0.0,
        )

    # Fallback mode: Try providers in sequence based on fallback_attempt
    if config.ENABLE_STT_FALLBACK:
        provider = get_provider_for_attempt(fallback_attempt)

        if not provider:
            raise ValueError("All STT providers exhausted")

        try:
            service = create_provider_service(provider)
            logger.info(f"STT Fallback: Using {provider} (attempt #{fallback_attempt})")
            return service
        except Exception as e:
            logger.error(f"STT Fallback: Failed to create {provider} service: {e}")
            # Let the calling code handle trying the next provider
            raise

    # Legacy mode: Use single configured provider (existing behavior)
    else:
        provider = config.STT_PROVIDER
        try:
            service = create_provider_service(provider)
            logger.info(f"Using configured STT provider: {provider}")
            return service
        except Exception as e:
            logger.error(f"Failed to create {provider} STT service: {e}")
            raise
