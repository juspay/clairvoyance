"""Utility functions for call manager."""

import base64
import json
from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.transformation_function import (
    TEMPLATE_FUNCTION_REGISTRY,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    LEGACY_VOICE_TO_PROVIDER,
    TemplateModel,
    TTSConfig,
    TTSProvider,
)
from app.ai.voice.agents.breeze_buddy.tts import generate_audio
from app.ai.voice.agents.breeze_buddy.utils.common import (
    _gemini_realtime_config,
    greeting_has_variables,
)
from app.ai.voice.llm.realtime.gemini.opening_line import generate_opening_line_mulaw
from app.core.config.dynamic import LEAD_GREETING_CACHE_TTL_SECONDS
from app.core.logger import logger
from app.services.redis.client import get_redis_service


def _resolve_greeting_text(
    initial_greeting: str, payload: dict, template: TemplateModel
) -> str:
    """Apply payload transformation functions and {placeholder} substitution.

    Shared by the TTS greeting path and the realtime opening-line path so
    both resolve the exact same text for the same lead.
    """
    resolved_payload = {}
    expected_schema = template.expected_payload_schema or {}

    for key, value in (payload or {}).items():
        resolved_value = value

        # Check if there are transformation functions for this field
        if key in expected_schema and isinstance(expected_schema[key], dict):
            field_schema = expected_schema[key]
            function_names = None
            raw = field_schema.get("function")
            if isinstance(raw, list):
                function_names = raw
            elif isinstance(raw, str):
                function_names = [raw]
            fn_params = {}
            raw_params = field_schema.get("params")
            if isinstance(raw_params, dict):
                fn_params = raw_params

            if function_names:
                for fn_name in function_names:
                    if fn_name not in TEMPLATE_FUNCTION_REGISTRY:
                        logger.warning(
                            f"Unknown transformation function '{fn_name}' "
                            f"for field '{key}', skipping"
                        )
                        continue
                    try:
                        func = TEMPLATE_FUNCTION_REGISTRY[fn_name]
                        if resolved_value is None or resolved_value == "":
                            resolved_value = func(**fn_params) if fn_params else func()
                        else:
                            resolved_value = (
                                func(resolved_value, **fn_params)
                                if fn_params
                                else func(resolved_value)
                            )
                        # Log key + function only: resolved values carry
                        # lead payload data (names, order ids) — no PII.
                        logger.info(f"Applied function '{fn_name}' to field '{key}'")
                    except Exception as e:
                        logger.opt(exception=e).warning(
                            f"Error applying function '{fn_name}' to field "
                            f"'{key}': {type(e).__name__}"
                        )

        resolved_payload[key] = resolved_value

    resolved_greeting = initial_greeting
    for key, value in resolved_payload.items():
        placeholder = f"{{{key}}}"
        if value is not None and isinstance(value, (str, int, float, bool)):
            resolved_greeting = resolved_greeting.replace(placeholder, str(value))
    return resolved_greeting


async def prepare_and_store_initial_greeting(
    lead_id: str,
    payload: dict,
    template: TemplateModel,
    generate_realtime_opening_line: bool = False,
) -> Optional[str]:
    """
    Synthesize and store initial greeting audio in Redis.

    Handles both static (template-level) and dynamic (lead-level) greetings.
    Static greetings are cached per template, dynamic greetings are per-lead.

    For Gemini Live (realtime) templates this delegates to
    ensure_realtime_opening_line_cached ONLY when
    generate_realtime_opening_line is True (dispatch worker, pre-dial);
    otherwise it is a read-only skip (connect-time fallbacks must never
    generate while the customer is on the line).

    Args:
        lead_id: The lead ID for dynamic greeting key
        payload: Lead payload for variable substitution
        template: Template with initial greeting configuration
        generate_realtime_opening_line: Pre-dial only — let a Gemini-realtime
            cache miss regenerate the opening line here

    Returns:
        The resolved greeting text if successful, None otherwise
    """
    if (
        not template
        or not template.configurations
        or not template.configurations.initial_greeting
    ):
        return None

    # Gemini Live templates own their greeting audio: a per-template opening
    # line generated with the call's Live voice
    # (ensure_realtime_opening_line_cached — static greetings only,
    # produced at template save / pre-dial). Synthesizing TTS here instead
    # would produce a different voice, and for variable-free greetings the
    # static template key it writes is checked FIRST at playback — it would
    # permanently shadow the Live-generated audio.
    #
    # Only the dispatch worker sets generate_realtime_opening_line (pre-dial,
    # bounded by its wait_for): a cache miss then regenerates BEFORE the
    # phone rings. The connect-time fallbacks keep the default False —
    # generating with the customer already on the line is seconds of dead
    # air, worse than the LLM speaking the opening itself.
    if _gemini_realtime_config(template) is not None:
        if generate_realtime_opening_line:
            return await ensure_realtime_opening_line_cached(template)
        return None

    try:
        initial_greeting = template.configurations.initial_greeting
        redis = await get_redis_service()

        # Check if greeting has variables
        has_variables = greeting_has_variables(initial_greeting)

        if has_variables:
            # Dynamic greeting - check if already exists for this lead
            lead_greeting_key = f"greeting:{lead_id}"
            existing_data = await redis.get(lead_greeting_key)
            if existing_data:
                # Parse JSON object containing both audio and text
                try:
                    greeting_data = json.loads(existing_data)
                    logger.info(f"Using existing dynamic greeting for lead {lead_id}")
                    return greeting_data.get("text")
                except json.JSONDecodeError:
                    # Legacy format - just audio bytes, no text
                    logger.info(
                        f"Using existing dynamic greeting audio for lead {lead_id} (legacy format)"
                    )
                    return None

            # Synthesize per lead with resolved variables
            # (shared resolver: payload transformation functions + substitution)
            resolved_greeting = _resolve_greeting_text(
                initial_greeting, payload or {}, template
            )

            # Build voice config: check payload override first, then template config
            voice_config = None
            p = payload or {}
            payload_provider = p.get("tts_provider") or LEGACY_VOICE_TO_PROVIDER.get(
                (p.get("tts_voice_name") or "").lower()
            )
            if payload_provider:
                try:
                    voice_config = TTSConfig(provider=TTSProvider(payload_provider))
                    logger.info(
                        f"Using TTS provider '{payload_provider}' from payload for greeting (lead {lead_id})"
                    )
                except ValueError:
                    logger.warning(
                        f"Invalid TTS provider '{payload_provider}' in payload, falling back to template config"
                    )

            # Fall back to template tts_configuration (resolve_voice_config handles None)
            if not voice_config and template.configurations.tts_configuration:
                voice_config = template.configurations.tts_configuration

            logger.info(
                f"Synthesizing dynamic greeting for lead {lead_id}: {resolved_greeting[:50]}..."
            )
            greeting_audio = await generate_audio(
                text=resolved_greeting,
                voice_config=voice_config,
                configurations=template.configurations,
            )

            # Store audio and text as single JSON object in Redis (temporary, deleted after use)
            greeting_data = {
                "audio": base64.b64encode(greeting_audio).decode("utf-8"),
                "text": resolved_greeting,
            }
            await redis.setex(
                key=lead_greeting_key,
                value=json.dumps(greeting_data),
                ttl_seconds=await LEAD_GREETING_CACHE_TTL_SECONDS(),
            )
            logger.info(f"Stored dynamic greeting in Redis for lead {lead_id}")
            return resolved_greeting

        else:
            # Static greeting - check if template audio already exists
            template_audio_key = f"greeting:template:{template.id}"
            existing_audio = await redis.get(template_audio_key)

            if existing_audio:
                logger.info(
                    f"Using existing static greeting audio for template {template.id}"
                )
                # For static greetings, the greeting text is the initial_greeting itself
                return initial_greeting

            logger.info(
                f"Synthesizing static greeting for template {template.id}: {initial_greeting[:50]}..."
            )
            greeting_audio = await generate_audio(
                text=initial_greeting,
                voice_config=template.configurations.tts_configuration,
                configurations=template.configurations,
            )
            await redis.set(
                key=template_audio_key,
                value=base64.b64encode(greeting_audio).decode("utf-8"),
            )
            logger.info(
                f"Stored static greeting audio in Redis for template {template.id}"
            )
            # For static greetings, return the initial_greeting text
            return initial_greeting

    except Exception as e:
        logger.error(
            f"Failed to synthesize/store greeting audio for lead {lead_id}: {e}"
        )
        # Continue without greeting audio - not a fatal error
        return None


async def ensure_realtime_opening_line_cached(
    template: TemplateModel,
    force: bool = False,
) -> Optional[str]:
    """Ensure a Gemini Live template's opening-line audio is cached.

    Per-TEMPLATE (greetings are static-only, no ``{placeholders}``): the audio
    is generated once with the call's Live model/voice/language and reused by
    every call. It lives in the SAME persistent static-template key the TTS
    path uses (``greeting:template:{id}``, raw base64 mulaw) — freshness
    comes from PUT/DELETE invalidation, not TTL. Since greetings are static,
    the text for playback/LLM-context is simply
    ``configurations.initial_greeting``; the cache stores audio only.

    ``force=True`` skips the cache check and regenerates — used by the
    template-save path so voice/model/greeting edits always re-synthesize.
    The dispatch worker AWAITS the cache check (and, on a miss, generation)
    before make_call; a hit is a single Redis GET, so the pre-dial wait is
    ~milliseconds for every call after the first. Generation is bounded by
    the generator's 30s timeout and blocks only that worker's dial.

    Automatic for every Gemini realtime template with a non-empty
    ``initial_greeting`` — no flag. Variable greetings are rejected at
    template save; a stored template that still has one skips pre-play here
    (LLM speaks first). Never raises; returns None on every skip/failure so
    the call fails open.

    Returns:
        The opening-line text if audio is cached (or was just generated),
        None otherwise.
    """
    configs = getattr(template, "configurations", None)
    initial_greeting = getattr(configs, "initial_greeting", None)
    if not initial_greeting:
        return None
    realtime = _gemini_realtime_config(template)
    if realtime is None:
        return None
    # Static-only by design: variable greetings are rejected at template
    # save. One stored before that enforcement skips pre-play (LLM speaks
    # first) rather than rendering per-lead.
    if greeting_has_variables(initial_greeting):
        logger.warning(
            f"opening-line: template {template.id} has a variable greeting "
            "(static text only is supported); skipping pre-play — edit the "
            "template's initial_greeting to re-enable"
        )
        return None

    template_key = f"greeting:template:{template.id}"

    if not force:
        try:
            if await (await get_redis_service()).get(template_key):
                return initial_greeting
        except Exception as e:  # noqa: BLE001 - fail open to LLM-speaks-first
            logger.opt(exception=e).warning(
                f"opening-line: cache check failed for template {template.id} "
                f"({type(e).__name__}); regenerating"
            )

    try:
        mulaw_audio = await generate_opening_line_mulaw(initial_greeting, realtime)
        if not mulaw_audio:
            logger.warning(
                f"opening-line: no audio generated for template {template.id}; "
                "LLM will speak first"
            )
            return None

        await (await get_redis_service()).set(
            key=template_key,
            value=base64.b64encode(mulaw_audio).decode("utf-8"),
        )
        # Log ids/sizes only — keep greeting text out of logs.
        logger.info(
            f"Stored realtime opening line for template {template.id} "
            f"({len(mulaw_audio)} bytes mulaw)"
        )
        return initial_greeting

    except Exception as e:  # noqa: BLE001 - fail open to LLM-speaks-first
        logger.opt(exception=e).warning(
            f"realtime opening-line preparation failed for template "
            f"{template.id}: {type(e).__name__}"
        )
        return None
