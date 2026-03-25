# Voice Configuration in Templates

## Overview

Templates support per-template voice configuration parameters for multiple TTS providers (Cartesia and ElevenLabs). This allows you to customize voice characteristics directly in your template configuration instead of relying solely on global Redis settings. Additionally, an LLM-based TTS provider selection mechanism (`TTSSelectionConfig`) can dynamically choose the optimal provider based on lead payload data.

## Configuration Structure

### Cartesia Configuration (Recommended)

```json
{
  "configurations": {
    "tts_voice_name": "mira",
    "cartesia_voice_configurations": {
      "voice_id": "248be419-c632-4f23-adf1-5324ed7dbf1d",
      "volume": 1.8,
      "speed": 1.2,
      "emotion": "excited",
      "language": "hi"
    }
  }
}
```

### ElevenLabs Configuration

```json
{
  "configurations": {
    "tts_voice_name": "rhea",
    "elevenlabs_voice_configurations": {
      "voice_id": "your-elevenlabs-voice-id",
      "model_id": "eleven_flash_v2_5",
      "speed": 1.0,
      "language": "en"
    }
  }
}
```

### LLM-Based TTS Provider Selection (TTSSelectionConfig)

When enabled, uses Gemini to analyze the lead payload and dynamically select the optimal TTS provider based on rules defined in a prompt.

```json
{
  "configurations": {
    "tts_selection_config": {
      "enabled": true,
      "prompt": "Based on the customer's address and region, decide the TTS provider. For Hindi-speaking regions (North India), use 'elevenlabs'. For South Indian regions or if unsure, use 'cartesia'.",
      "providers": ["elevenlabs", "cartesia"]
    },
    "cartesia_voice_configurations": { "emotion": "neutral" },
    "elevenlabs_voice_configurations": { "model_id": "eleven_flash_v2_5" }
  }
}
```

### Legacy Format (Still Supported)

```json
{
  "configurations": {
    "tts_voice_name": "mira",
    "mira_voice_id": "248be419-c632-4f23-adf1-5324ed7dbf1d"
  }
}
```

**Note:** The new `cartesia_voice_configurations.voice_id` takes precedence over the legacy `mira_voice_id` field.

## Cartesia Parameters (`cartesia_voice_configurations`)

### `voice_id` (Optional)
- **Type:** `string`
- **Description:** Cartesia voice UUID
- **Example:** `"248be419-c632-4f23-adf1-5324ed7dbf1d"`
- **Default:** Falls back to global `BB_CARTESIA_VOICE_ID` from Redis

### `volume` (Optional)
- **Type:** `float`
- **Description:** Volume multiplier
- **Range:** 0.5 - 2.0 (validated via Pydantic `ge`/`le` constraints)
- **Example:** `1.8` (80% louder than normal)
- **Default:** Falls back to global `BB_CARTESIA_GENERATION_VOLUME` from Redis (default: 1.5)

### `speed` (Optional)
- **Type:** `float`
- **Description:** Speed multiplier
- **Range:** 0.6 - 1.5 (validated via Pydantic `ge`/`le` constraints)
- **Example:** `1.2` (20% faster than normal)
- **Default:** Falls back to global `BB_CARTESIA_GENERATION_SPEED` from Redis (default: 1.0)

### `emotion` (Optional)
- **Type:** `string`
- **Description:** Voice emotion/tone
- **Supported Values:**
  - `"neutral"` - Standard, professional tone
  - `"excited"` - Enthusiastic, energetic tone
  - `"happy"` - Cheerful, positive tone
  - `"sad"` - Somber, empathetic tone
  - `"angry"` - Firm, serious tone
  - `"fearful"` - Cautious, concerned tone
  - And others supported by Cartesia API
- **Example:** `"excited"`
- **Default:** Falls back to global `BB_CARTESIA_GENERATION_EMOTION` from Redis (default: "neutral")
- **TODO:** Add validation against known Cartesia emotions

### `language` (Optional)
- **Type:** `string`
- **Description:** TTS language code (separate from STT language)
- **Supported Values:**
  - `"en"` - English
  - `"hi"` - Hindi
  - `"es"` - Spanish
  - `"fr"` - French
  - And others supported by Cartesia API
- **Example:** `"hi"`
- **Default:** Falls back to global `BB_CARTESIA_LANGUAGE` from Redis (default: "en")
- **Note:** This is separate from `stt_language` which controls speech recognition
- **TODO:** Add validation for language codes

## ElevenLabs Parameters (`elevenlabs_voice_configurations`)

### `voice_id` (Optional)
- **Type:** `string`
- **Description:** ElevenLabs voice ID
- **Default:** Falls back to global `BB_ELEVENLABS_VOICE_ID` from Redis

### `model_id` (Optional)
- **Type:** `string`
- **Description:** ElevenLabs model ID
- **Example:** `"eleven_flash_v2_5"`
- **Default:** Falls back to global `BB_ELEVENLABS_MODEL_ID` from Redis

### `speed` (Optional)
- **Type:** `float`
- **Description:** Speed multiplier
- **Range:** 0.7 - 1.2 (validated via Pydantic `ge`/`le` constraints, where 1.0 is default)
- **Default:** Falls back to global `BB_ELEVENLABS_VOICE_SPEED` from Redis

### `language` (Optional)
- **Type:** `string`
- **Description:** TTS language code (e.g., `"en"`, `"hi"`)
- **Default:** `EN_IN` (English - India)
- **Note:** Unlike `voice_id`, `model_id`, and `speed`, the language default is hardcoded to `EN_IN` in the service layer (`tts/__init__.py`) rather than loaded from Redis/config. If no language is specified in the template, `EN_IN` is always used.
- **TODO:** Add validation for language codes

## TTS Selection Parameters (`tts_selection_config`)

### `enabled` (Optional)
- **Type:** `bool`
- **Description:** Whether LLM-based TTS provider selection is active
- **Default:** `false`

### `prompt` (Required)
- **Type:** `string`
- **Description:** Prompt template for Gemini to decide which TTS provider to use. The lead payload is appended to this prompt automatically.

### `providers` (Required)
- **Type:** `list[string]`
- **Description:** Allowed TTS providers the LLM can choose from
- **Supported Values:** `"elevenlabs"`, `"cartesia"`
- **Constraint:** Must contain at least one provider

## Configuration Precedence

The system follows this priority order:

1. **Template-level values** (`cartesia_voice_configurations`) - Highest priority
2. **Legacy template value** (`mira_voice_id`) - For backward compatibility
3. **Global Redis defaults** (`BB_CARTESIA_*`) - Fallback when template doesn't specify

This ensures:
- ✅ Full backward compatibility with existing templates
- ✅ Fine-grained per-template control
- ✅ Sensible defaults when not specified

## Example Use Cases

### Use Case 1: High-Energy Sales Call (English)
```json
{
  "configurations": {
    "tts_voice_name": "mira",
    "cartesia_voice_configurations": {
      "volume": 1.7,
      "speed": 1.3,
      "emotion": "excited",
      "language": "en"
    }
  }
}
```

### Use Case 2: Calm Support Call (Hindi)
```json
{
  "configurations": {
    "tts_voice_name": "mira",
    "cartesia_voice_configurations": {
      "volume": 1.2,
      "speed": 0.9,
      "emotion": "neutral",
      "language": "hi"
    }
  }
}
```

### Use Case 3: Custom Voice with Default Settings
```json
{
  "configurations": {
    "tts_voice_name": "mira",
    "cartesia_voice_configurations": {
      "voice_id": "custom-voice-uuid-here"
    }
  }
}
```
Only `voice_id` is specified; other parameters fall back to Redis defaults.

### Use Case 4: Multilingual Template
```json
{
  "configurations": {
    "tts_voice_name": "mira",
    "stt_language": "hi",
    "cartesia_voice_configurations": {
      "language": "hi",
      "emotion": "happy",
      "speed": 1.0
    }
  }
}
```
Both STT (speech recognition) and TTS (speech synthesis) set to Hindi.

## Implementation Details

### Files Modified

1. **`app/ai/voice/agents/breeze_buddy/template/types.py`**
   - `CartesiaVoiceConfiguration` Pydantic model with `ge`/`le` validators for volume and speed
   - `ElevenLabsVoiceConfiguration` Pydantic model with `ge`/`le` validators for speed
   - `TTSSelectionConfig` Pydantic model for LLM-based provider selection
   - `ConfigurationModel` updated with `cartesia_voice_configurations`, `elevenlabs_voice_configurations`, and `tts_selection_config` fields

2. **`app/ai/voice/agents/breeze_buddy/tts/__init__.py`**
   - `get_cartesia_tts_service()` accepts template parameters, merges with Redis defaults
   - `get_elevenlabs_tts_service()` accepts template parameters, merges with Redis defaults
   - `get_tts_service()` routes to the correct provider based on voice name or config

3. **`app/ai/voice/agents/breeze_buddy/agent/pipeline.py`**
   - Updated `create_services()` to extract and pass voice configurations to TTS factories

### Backward Compatibility

All existing templates continue to work without modification:
- Templates without `cartesia_voice_configurations` use global Redis settings
- Templates with legacy `mira_voice_id` continue to work
- New field is completely optional

## Testing

See [`cartesia-voice-config-example.json`](../../../app/ai/voice/agents/breeze_buddy/examples/templates/cartesia-voice-config-example.json) for a complete working example demonstrating all configuration parameters.

## Future Enhancements

- [x] ~~Add validation for parameter ranges (volume: 0.5-2.0, speed: 0.6-1.5)~~ - Implemented via Pydantic `ge`/`le` validators in `types.py`
- [ ] Add validation for emotion strings against known Cartesia emotions
- [ ] Add validation for language codes
- [ ] Support multiple emotions (Cartesia API supports this)
- [ ] Per-template model selection (currently hardcoded to "sonic-3")
- [ ] Per-template sentence aggregation control

## Questions?

For implementation details or questions, refer to:
- Template type definitions: `app/ai/voice/agents/breeze_buddy/template/types.py`
- TTS service factory: `app/ai/voice/agents/breeze_buddy/tts/__init__.py`
- Pipeline creation: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`
