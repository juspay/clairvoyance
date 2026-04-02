"""
Business logic handlers for playground operations.
"""

from app.ai.voice.agents.breeze_buddy.template.types import (
    BackgroundSoundFile,
    CartesiaVoiceConfiguration,
    ElevenLabsVoiceConfiguration,
    InterruptionMode,
    KeywordMatchType,
    NoiseFilterType,
    TTSVoiceName,
)
from app.ai.voice.agents.breeze_buddy.utils.language_utils.language_detector import (
    LANGUAGE_NAMES,
    SHORT_TO_FULL_LANGUAGE_CODE,
)
from app.ai.voice.llm.types import (
    AzureLLMPlaygroundConfig,
    AzureThinkingPlaygroundConfig,
    LLMProvider,
    LLMSdk,
    VertexClaudeThinkingPlaygroundConfig,
    VertexGeminiThinkingPlaygroundConfig,
    VertexLLMPlaygroundConfig,
)


async def get_configuration_options_handler():
    # TTS voices — config_key points to which config fields array to use
    _voice_config_key_map = {
        "mira": "cartesia_voice_configurations",
        "rhea": "elevenlabs_voice_configurations",
    }
    tts_voices = [
        {
            "value": voice.value,
            "label": voice.value.capitalize(),
            **(
                {"config_key": _voice_config_key_map[voice.value]}
                if voice.value in _voice_config_key_map
                else {}
            ),
        }
        for voice in TTSVoiceName
    ]

    # Per-voice config field definitions derived from the Pydantic model schema
    def _voice_config_fields(model_class) -> list:
        schema = model_class.model_json_schema()
        fields = []
        for key, props in schema.get("properties", {}).items():
            field_type = (
                "number" if props.get("type") in ("number", "integer") else "string"
            )
            entry = {
                "key": key,
                "type": field_type,
                "label": key.replace("_", " ").title(),
                "placeholder": props.get("description", ""),
            }
            if "minimum" in props:
                entry["min"] = props["minimum"]
            if "maximum" in props:
                entry["max"] = props["maximum"]
            fields.append(entry)
        return fields

    cartesia_voice_configurations = _voice_config_fields(CartesiaVoiceConfiguration)
    elevenlabs_voice_configurations = _voice_config_fields(ElevenLabsVoiceConfiguration)

    # STT languages (Soniox supported) - dynamically from constants
    # Use full locale codes (e.g. "en-IN") as values to match what templates store
    stt_languages = [
        {"value": full_code, "label": LANGUAGE_NAMES.get(full_code, full_code)}
        for full_code in SHORT_TO_FULL_LANGUAGE_CODE.values()
    ]

    # Background sounds
    background_sounds = [
        {"value": sound.value, "label": sound.value.replace("-", " ").title()}
        for sound in BackgroundSoundFile
    ]

    # Noise filter types
    noise_filter_types = [
        {"value": filter_type.value, "label": filter_type.value.upper()}
        for filter_type in NoiseFilterType
    ]

    # Keyword match types
    keyword_match_types = [
        {"value": match_type.value, "label": match_type.value.replace("_", " ").title()}
        for match_type in KeywordMatchType
    ]

    # Interruption modes
    interruption_modes = [
        {"value": mode.value, "label": mode.value.replace("_", " ").title()}
        for mode in InterruptionMode
    ]

    # LLM provider and SDK options
    llm_providers = [
        {"value": p.value, "label": p.value.replace("_", " ").title()}
        for p in LLMProvider
    ]
    llm_sdks = [
        {"value": s.value, "label": s.value.replace("_", " ").title()} for s in LLMSdk
    ]

    # Per-provider LLM config fields — derived from Pydantic models (same as voice configs)
    llm_fields = {
        LLMProvider.AZURE.value: _voice_config_fields(AzureLLMPlaygroundConfig),
        LLMProvider.GOOGLE_VERTEX.value: _voice_config_fields(
            VertexLLMPlaygroundConfig
        ),
    }

    # Thinking fields keyed by provider (azure) or provider__sdk (google_vertex)
    llm_thinking_fields = {
        LLMProvider.AZURE.value: _voice_config_fields(AzureThinkingPlaygroundConfig),
        f"{LLMProvider.GOOGLE_VERTEX.value}__{LLMSdk.GOOGLE.value}": _voice_config_fields(
            VertexGeminiThinkingPlaygroundConfig
        ),
        f"{LLMProvider.GOOGLE_VERTEX.value}__{LLMSdk.ANTHROPIC.value}": _voice_config_fields(
            VertexClaudeThinkingPlaygroundConfig
        ),
    }

    return {
        "tts_voices": tts_voices,
        "stt_languages": stt_languages,
        "background_sounds": background_sounds,
        "noise_filter_types": noise_filter_types,
        "keyword_match_types": keyword_match_types,
        "interruption_modes": interruption_modes,
        "cartesia_voice_configurations": cartesia_voice_configurations,
        "elevenlabs_voice_configurations": elevenlabs_voice_configurations,
        "llm_providers": llm_providers,
        "llm_sdks": llm_sdks,
        "llm_fields": llm_fields,
        "llm_thinking_fields": llm_thinking_fields,
    }
