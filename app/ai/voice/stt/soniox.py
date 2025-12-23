"""Soniox STT config and builder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

from pipecat.services.soniox.stt import (
    SonioxContextGeneralItem,
    SonioxContextObject,
    SonioxContextTranslationTerm,
    SonioxInputParams,
    SonioxSTTService,
)
from pipecat.transcriptions.language import Language

from app.core.logger import logger

__all__ = ["SonioxConfig", "build_soniox_stt"]


@dataclass
class SonioxConfig:
    """Configuration for Soniox STT.

    Language hints can be provided as:
    - A comma-separated string (e.g., "en,es,fr") - will be parsed automatically
    - An iterable of strings (e.g., ["en", "es", "fr"])
    - None (no language hints)
    """

    api_key: str
    model: str
    vad_force_turn_endpoint: bool
    language_hints: Optional[str | Iterable[str]] = None
    context_json: Optional[str] = None
    enable_non_final_tokens: bool = False
    max_non_final_tokens_duration_ms: Optional[int] = None
    client_reference_id: Optional[str] = None
    log_context: str = "Soniox"
    language_hints_strict: bool = False


def _parse_soniox_context(
    context_json: Optional[str], log_context: str
) -> Optional[SonioxContextObject]:
    """Parse Soniox context JSON into :class:`SonioxContextObject`."""

    if not context_json:
        return None

    try:
        context_data = json.loads(context_json)

        general_items = context_data.get("general", [])
        text = context_data.get("text")
        terms = context_data.get("terms", [])
        translation_terms_items = context_data.get("translation_terms", [])

        general_objects = (
            [
                SonioxContextGeneralItem(key=item["key"], value=item["value"])
                for item in general_items
            ]
            if general_items
            else None
        )

        translation_terms_objects = (
            [
                SonioxContextTranslationTerm(
                    source=item["source"], target=item["target"]
                )
                for item in translation_terms_items
            ]
            if translation_terms_items
            else None
        )

        context_object = SonioxContextObject(
            general=general_objects if general_objects else None,
            text=text,
            terms=terms if terms else None,
            translation_terms=(
                translation_terms_objects if translation_terms_objects else None
            ),
        )

        logger.info(
            "Successfully parsed %s Soniox context with %d general items, %d terms, %d translation terms",
            log_context,
            len(general_objects or []),
            len(terms) if terms else 0,
            len(translation_terms_objects or []),
        )
        return context_object

    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning(
            "Failed to parse %s Soniox context: %s. Falling back to None context.",
            log_context,
            exc,
        )
        return None


def build_soniox_stt(config: SonioxConfig):
    """Create a Soniox STT service.

    Automatically handles language hints parsing:
    - If provided as a comma-separated string, it will be split and parsed
    - If provided as an iterable, it will be used directly
    - Empty/whitespace-only values are filtered out
    """
    # Parse language hints - handle both string and iterable formats
    language_hints = None
    if config.language_hints:
        if isinstance(config.language_hints, str):
            # Parse comma-separated string
            parsed_hints = [
                lang.strip()
                for lang in config.language_hints.split(",")
                if lang.strip()
            ]
            logger.debug(
                f"Parsed {len(parsed_hints)} language hints from comma-separated string"
            )
        else:
            # Already an iterable
            parsed_hints = list(config.language_hints)
            logger.debug(f"Using {len(parsed_hints)} language hints from iterable")

        if parsed_hints:
            language_hints = [Language(lang) for lang in parsed_hints if lang]

    context = _parse_soniox_context(config.context_json, config.log_context)

    soniox_params = SonioxInputParams(
        model=config.model,
        language_hints=language_hints,
        context=context,
        enable_non_final_tokens=config.enable_non_final_tokens,
        max_non_final_tokens_duration_ms=(
            config.max_non_final_tokens_duration_ms
            if config.max_non_final_tokens_duration_ms
            and config.max_non_final_tokens_duration_ms > 0
            else None
        ),
        client_reference_id=config.client_reference_id,
        language_hints_strict=config.language_hints_strict,
    )

    # Format language hints for logging
    hints_display = ""
    if config.language_hints:
        if isinstance(config.language_hints, str):
            hints_display = config.language_hints
        else:
            hints_display = ",".join(config.language_hints)

    logger.info(
        "Using %s Soniox STT service with model: %s, language_hints: %s, VAD force endpoint: %s, non_final_tokens: %s",
        config.log_context,
        config.model,
        hints_display,
        config.vad_force_turn_endpoint,
        config.enable_non_final_tokens,
    )

    return SonioxSTTService(
        api_key=config.api_key,
        params=soniox_params,
        vad_force_turn_endpoint=config.vad_force_turn_endpoint,
    )
