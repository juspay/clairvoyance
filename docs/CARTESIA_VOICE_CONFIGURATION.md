# Cartesia Voice Configuration in Templates

## Overview

As of this update, templates now support per-template Cartesia voice configuration parameters. This allows you to customize voice characteristics (volume, speed, emotion, language) directly in your template configuration instead of relying solely on global Redis settings.

## Configuration Structure

### New Format (Recommended)

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

## Available Parameters

### `voice_id` (Optional)
- **Type:** `string`
- **Description:** Cartesia voice UUID
- **Example:** `"248be419-c632-4f23-adf1-5324ed7dbf1d"`
- **Default:** Falls back to global `BB_CARTESIA_VOICE_ID` from Redis

### `volume` (Optional)
- **Type:** `float`
- **Description:** Volume multiplier
- **Range:** 0.5 - 2.0 (Cartesia API constraint)
- **Example:** `1.8` (80% louder than normal)
- **Default:** Falls back to global `BB_CARTESIA_GENERATION_VOLUME` from Redis (default: 1.5)
- **TODO:** Add validation for parameter range

### `speed` (Optional)
- **Type:** `float`
- **Description:** Speed multiplier
- **Range:** 0.6 - 1.5 (Cartesia API constraint)
- **Example:** `1.2` (20% faster than normal)
- **Default:** Falls back to global `BB_CARTESIA_GENERATION_SPEED` from Redis (default: 1.0)
- **TODO:** Add validation for parameter range

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
   - Added `CartesiaVoiceConfiguration` dataclass
   - Updated `ConfigurationModel` with `cartesia_voice_configurations` field

2. **`app/ai/voice/agents/breeze_buddy/tts/__init__.py`**
   - Updated `get_cartesia_tts_service()` to accept template parameters
   - Implemented merge logic for template → Redis fallback

3. **`app/ai/voice/agents/breeze_buddy/agent/pipeline.py`**
   - Updated `create_services()` to extract and pass Cartesia configurations

### Backward Compatibility

All existing templates continue to work without modification:
- Templates without `cartesia_voice_configurations` use global Redis settings
- Templates with legacy `mira_voice_id` continue to work
- New field is completely optional

## Testing

See `cartesia-voice-config-example.json` for a complete working example demonstrating all configuration parameters.

## Future Enhancements

- [ ] Add validation for parameter ranges (volume: 0.5-2.0, speed: 0.6-1.5)
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
