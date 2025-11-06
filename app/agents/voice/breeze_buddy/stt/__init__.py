import json
from typing import Optional

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

from app.core import config
from app.core.logger import logger


def parse_breeze_buddy_soniox_context() -> Optional[SonioxContextObject]:
    """
    Parse Breeze Buddy Soniox context from JSON environment variable into SonioxContextObject.

    Expected JSON structure:
    {
        "general": [{"key": "organisation", "value": "Juspay"}, ...],
        "text": "Breeze Buddy is an automated voice agent...",
        "terms": ["Juspay", "Breeze Buddy", "COD", ...],
        "translation_terms": [{"source": "...", "target": "..."}, ...]
    }

    Returns:
        SonioxContextObject if parsing succeeds, None otherwise
    """
    if not config.BREEZE_BUDDY_SONIOX_CONTEXT:
        return None

    try:
        # Parse JSON from environment variable
        context_data = json.loads(config.BREEZE_BUDDY_SONIOX_CONTEXT)

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
            f"Successfully parsed Breeze Buddy Soniox context with {len(general_objects or [])} general items, "
            f"{len(terms) if terms else 0} terms, "
            f"{len(translation_terms_objects or [])} translation terms"
        )
        return context_object

    except Exception as e:
        logger.warning(
            f"Failed to parse BREEZE_BUDDY_SONIOX_CONTEXT: {e}. Falling back to None context."
        )
        return None


def get_stt_service():
    """
    Returns an STT service instance based on the environment configuration.
    """
    if config.BREEZE_BUDDY_STT_SERVICE == "openai":
        logger.info("Using OpenAI STT service for Breeze Buddy voice")
        return OpenAISTTService(
            api_key=config.OPENAI_STT_API_KEY,
            model=config.OPENAI_STT_MODEL,
            language=Language.EN,
            temperature=0.0,
        )
    elif config.BREEZE_BUDDY_STT_SERVICE == "soniox":
        language_hints = None
        if config.BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS:
            lang_list = [
                lang.strip()
                for lang in config.BREEZE_BUDDY_SONIOX_LANGUAGE_HINTS.split(",")
            ]
            language_hints = [Language(lang) for lang in lang_list if lang]

        # Parse context from JSON environment variable
        context = parse_breeze_buddy_soniox_context()

        # Configure Soniox with supported parameters only
        soniox_params = SonioxInputParams(
            model=config.BREEZE_BUDDY_SONIOX_MODEL,
            language_hints=language_hints,
            context=context,
            enable_non_final_tokens=config.BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS,
            max_non_final_tokens_duration_ms=(
                config.BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS
                if config.BREEZE_BUDDY_SONIOX_MAX_NON_FINAL_TOKENS_DURATION_MS > 0
                else None
            ),
            client_reference_id=None,
        )

        return SonioxSTTService(
            api_key=config.SONIOX_API_KEY,
            params=soniox_params,
            vad_force_turn_endpoint=config.BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT,
        )

    else:
        logger.info("Using Google STT service with VAD-based turn detection")
        return GoogleSTTService(
            params=GoogleSTTService.InputParams(
                languages=[Language.EN_US, Language.EN_IN], enable_interim_results=False
            ),
            credentials=config.GOOGLE_CREDENTIALS_JSON,
        )
