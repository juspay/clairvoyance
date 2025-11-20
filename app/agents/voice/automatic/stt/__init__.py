import json
from typing import Optional

from deepgram import LiveOptions
from pipecat.services.assemblyai.stt import AssemblyAISTTService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.stt import CommitStrategy, ElevenLabsRealtimeSTTService
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.soniox.stt import (
    SonioxContextGeneralItem,
    SonioxContextObject,
    SonioxContextTranslationTerm,
    SonioxInputParams,
    SonioxSTTService,
)
from pipecat.transcriptions.language import Language

from app.agents.voice.automatic.types import VoiceName
from app.core import config
from app.core.logger import logger


def parse_soniox_context() -> Optional[SonioxContextObject]:
    """
    Parse Soniox context from JSON environment variable into SonioxContextObject.

    Expected JSON structure:
    {
        "general": [{"key": "organisation", "value": "Juspay"}, ...],
        "text": "User Persona: D2C ecommerce merchants...",
        "terms": ["Juspay", "Breeze Automatic", "PSR", ...],
        "translation_terms": [{"source": "...", "target": "..."}, ...]
    }

    Returns:
        SonioxContextObject if parsing succeeds, None otherwise
    """
    if not config.SONIOX_CONTEXT:
        return None

    try:
        # Parse JSON from environment variable
        context_data = json.loads(config.SONIOX_CONTEXT)

        # Extract fields from JSON
        general_items = context_data.get("general", [])
        text = context_data.get("text")
        terms = context_data.get("terms", [])
        translation_terms_items = context_data.get("translation_terms", [])

        # Convert general items to SonioxContextGeneralItem objects
        general_objects = None
        if general_items:
            general_objects = [
                SonioxContextGeneralItem(key=item["key"], value=item["value"])
                for item in general_items
            ]

        # Convert translation_terms items to SonioxContextTranslationTerm objects
        translation_terms_objects = None
        if translation_terms_items:
            translation_terms_objects = [
                SonioxContextTranslationTerm(
                    source=item["source"], target=item["target"]
                )
                for item in translation_terms_items
            ]

        # Create and return SonioxContextObject
        context_object = SonioxContextObject(
            general=general_objects if general_objects else None,
            text=text,
            terms=terms if terms else None,
            translation_terms=(
                translation_terms_objects if translation_terms_objects else None
            ),
        )

        logger.info(
            f"Successfully parsed Soniox context with {len(general_objects or [])} general items, "
            f"{len(terms) if terms else 0} terms, "
            f"{len(translation_terms_objects or [])} translation terms"
        )
        return context_object

    except Exception as e:
        logger.warning(
            f"Failed to parse SONIOX_CONTEXT: {e}. Falling back to None context."
        )
        return None


def get_stt_service(voice_name: Optional[str] = None):
    """
    Returns an STT service instance based on the environment configuration.

    Args:
        voice_name: Voice name to determine STT provider override for specific voices
    """
    # Check for MIA voice with OpenAI override
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
            # Optimized prompt for business analytics voice agent
            prompt=config.AUTOMATIC_OPENAI_STT_PROMPT,
            temperature=0.0,  # Deterministic output for consistency
        )

    # Default behavior - use configured STT provider
    if config.STT_PROVIDER == "assemblyai":
        if not config.ASSEMBLYAI_API_KEY:
            raise ValueError(
                "ASSEMBLYAI_API_KEY is required when STT_PROVIDER=assemblyai"
            )

        logger.info("Using AssemblyAI STT service with Silero VAD-based turn detection")
        return AssemblyAISTTService(
            api_key=config.ASSEMBLYAI_API_KEY,
            # Use Silero VAD for turn detection instead of AssemblyAI's built-in turn detection
            vad_force_turn_endpoint=True,
            # No connection_params needed since we're using VAD for turn detection
        )
    elif config.STT_PROVIDER == "openai":
        if not config.OPENAI_STT_API_KEY:
            raise ValueError(
                "OPENAI_STT_API_KEY or OPENAI_API_KEY is required when STT_PROVIDER=openai"
            )

        logger.info(
            f"Using OpenAI STT service ({config.OPENAI_STT_MODEL}) with Silero VAD-based turn detection"
        )
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            # Optimized prompt for business analytics voice agent
            prompt=config.AUTOMATIC_OPENAI_STT_PROMPT,
            temperature=0.0,  # Deterministic output for consistency
        )
    elif config.STT_PROVIDER == "deepgram":
        if not config.DEEPGRAM_API_KEY:
            raise ValueError("DEEPGRAM_API_KEY is required when STT_PROVIDER=deepgram")

        # Determine language configuration based on settings
        if config.DEEPGRAM_AUTO_DETECT_LANGUAGE:
            language_config = "multi"  # Automatic detection
        else:
            language_config = (
                config.DEEPGRAM_LANGUAGE
            )  # Single language (current behavior)

        # Configure Deepgram with smart turn detection and audio enhancement
        live_options = LiveOptions(
            model=config.DEEPGRAM_MODEL,
            language=language_config,
            smart_format=config.DEEPGRAM_SMART_FORMAT,
            punctuate=config.DEEPGRAM_PUNCTUATE,
            endpointing=config.DEEPGRAM_ENDPOINTING,  # Smart turn detection
            vad_events=config.DEEPGRAM_VAD_EVENTS,  # Built-in VAD
            utterance_end_ms=config.DEEPGRAM_UTTERANCE_END_MS,
            no_delay=config.DEEPGRAM_NO_DELAY,  # Real-time processing
            interim_results=True,
            profanity_filter=config.DEEPGRAM_PROFANITY_FILTER,
            # Enhanced for Indian English and business terms
            numerals=config.DEEPGRAM_NUMERALS,  # Better number recognition
            diarize=config.DEEPGRAM_DIARIZE,  # Speaker identification
        )

        logger.info(
            f"Using Deepgram STT service with model: {config.DEEPGRAM_MODEL}, "
            f"language: {language_config} "
            f"(VAD: {config.DEEPGRAM_VAD_EVENTS}, Endpointing: {config.DEEPGRAM_ENDPOINTING})"
        )
        return DeepgramSTTService(
            api_key=config.DEEPGRAM_API_KEY, live_options=live_options
        )
    elif config.STT_PROVIDER == "soniox":
        if not config.SONIOX_API_KEY:
            raise ValueError("SONIOX_API_KEY is required when STT_PROVIDER=soniox")

        # Parse language hints from comma-separated string
        language_hints = None
        if config.SONIOX_LANGUAGE_HINTS:
            lang_list = [
                lang.strip() for lang in config.SONIOX_LANGUAGE_HINTS.split(",")
            ]
            language_hints = [Language(lang) for lang in lang_list if lang]

        # Parse context from JSON environment variable
        context = parse_soniox_context()

        # Configure Soniox with supported parameters only
        soniox_params = SonioxInputParams(
            model=config.SONIOX_MODEL,
            language_hints=language_hints,
            context=context,
            enable_non_final_tokens=config.SONIOX_ENABLE_NON_FINAL_TOKENS,
            max_non_final_tokens_duration_ms=(
                config.SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS
                if config.SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS > 0
                else None
            ),
            client_reference_id=None,
        )

        logger.info(
            f"Using Soniox STT service with model: {config.SONIOX_MODEL}, "
            f"language_hints: {config.SONIOX_LANGUAGE_HINTS}, "
            f"VAD force endpoint: {config.SONIOX_VAD_FORCE_TURN_ENDPOINT}, "
            f"non_final_tokens: {config.SONIOX_ENABLE_NON_FINAL_TOKENS}"
        )
        return SonioxSTTService(
            api_key=config.SONIOX_API_KEY,
            params=soniox_params,
            vad_force_turn_endpoint=config.SONIOX_VAD_FORCE_TURN_ENDPOINT,
        )
    elif config.STT_PROVIDER == "elevenlabs":
        if not config.ELEVENLABS_API_KEY:
            raise ValueError(
                "ELEVENLABS_API_KEY is required when STT_PROVIDER=elevenlabs"
            )

        logger.info(
            f"Using ElevenLabs Realtime STT service with model: {config.ELEVENLABS_STT_MODEL}"
        )
        return ElevenLabsRealtimeSTTService(
            api_key=config.ELEVENLABS_API_KEY,
            model=config.ELEVENLABS_STT_MODEL,
            params=ElevenLabsRealtimeSTTService.InputParams(
                language_code=config.ELEVENLABS_STT_LANGUAGE,
                commit_strategy=(
                    CommitStrategy.VAD
                    if config.ELEVENLABS_STT_COMMIT_STRATEGY == "vad"
                    else CommitStrategy.MANUAL
                ),
                vad_silence_threshold_secs=config.ELEVENLABS_STT_VAD_SILENCE_THRESHOLD,
                vad_threshold=config.ELEVENLABS_STT_VAD_THRESHOLD,
            ),
        )
    else:  # Default to Google STT
        logger.info("Using Google STT service with VAD-based turn detection")

        return GoogleSTTService(
            params=GoogleSTTService.InputParams(
                languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False
            ),
            credentials=config.GOOGLE_CREDENTIALS_JSON,
        )
