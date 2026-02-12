# Modular Turn Strategy System - Usage Guide

## Overview

The modular turn strategy system allows you to configure intelligent conversation turn-taking behavior through template JSON configuration, with support for all 15+ Pipecat turn strategies.

## Key Features

✅ **Template-Based Configuration** - Configure strategies via JSON in your template
✅ **Full Strategy Support** - All Pipecat start/stop/mute/analyzer strategies available
✅ **Modular & Extensible** - Add new strategies without code changes
✅ **Configuration Layers** - Template → Redis → Code defaults
✅ **Type-Safe** - Comprehensive validation with clear error messages
✅ **Backward Compatible** - Existing Redis-based config still works

## Quick Start

### 1. Basic Smart Turn Configuration

Add to your template JSON:

```json
{
  "configurations": {
    "turn_strategy_config": {
      "enabled": true,
      "stop_strategies": [
        {
          "type": "turn_analyzer",
          "params": {
            "analyzer": "local_smart_turn_v3",
            "timeout": 0.5
          }
        }
      ]
    }
  }
}
```

This enables ML-based turn detection with natural pause handling.

### 2. Prevent False Starts (Filter "um", "uh")

```json
{
  "configurations": {
    "turn_strategy_config": {
      "enabled": true,
      "start_strategies": [
        {
          "type": "min_words",
          "params": {
            "min_words": 3,
            "use_interim": true
          }
        }
      ],
      "stop_strategies": [
        {
          "type": "turn_analyzer",
          "params": {
            "analyzer": "local_smart_turn_v3"
          }
        }
      ]
    }
  }
}
```

Requires user to speak at least 3 words before turn starts.

### 3. Mute During Function Calls

```json
{
  "configurations": {
    "turn_strategy_config": {
      "enabled": true,
      "stop_strategies": [
        {
          "type": "turn_analyzer",
          "params": {
            "analyzer": "local_smart_turn_v3"
          }
        }
      ],
      "mute_strategies": [
        {
          "type": "function_call"
        }
      ]
    }
  }
}
```

User input is ignored while LLM function calls execute.

## Configuration Reference

### Top-Level Structure

```json
{
  "turn_strategy_config": {
    "enabled": true,                    // Enable custom strategies
    "start_strategies": [...],          // Optional, defaults to VAD + Transcription
    "stop_strategies": [...],           // Optional, defaults to Transcription
    "mute_strategies": [...],           // Optional, defaults to no muting
    "user_turn_stop_timeout": 5.0       // Safety timeout (1.0-30.0 seconds)
  }
}
```

### Start Strategies

Determine when user turn begins:

#### 1. VAD (Voice Activity Detection)

```json
{
  "type": "vad",
  "params": {}
}
```

- Triggers on speech detection
- Fastest response time
- May trigger on background noise

#### 2. Transcription

```json
{
  "type": "transcription",
  "params": {
    "use_interim": true  // Use interim transcriptions (default: true)
  }
}
```

- Triggers on first transcription
- Catches soft speech VAD might miss
- Slightly slower than VAD

#### 3. Min Words

```json
{
  "type": "min_words",
  "params": {
    "min_words": 3,      // Minimum words required (default: 3)
    "use_interim": true  // Use interim transcriptions (default: true)
  }
}
```

- Triggers after N words spoken
- Prevents false starts from filler words
- Best for reducing interruptions

#### 4. External

```json
{
  "type": "external",
  "params": {}
}
```

- External processor controls turn start
- For custom logic or external services

### Stop Strategies

Determine when user turn ends:

#### 1. Transcription-Based (Simple)

```json
{
  "type": "transcription",
  "params": {
    "timeout": 0.5  // Transcription wait time (default: 0.5)
  }
}
```

- Stops on transcription after VAD silence
- Fast but may interrupt mid-thought
- Good for debugging or when ML overhead unwanted

#### 2. Turn Analyzer (Smart Turn) ⭐ Recommended

```json
{
  "type": "turn_analyzer",
  "params": {
    "analyzer": "local_smart_turn_v3",  // Analyzer type
    "timeout": 0.5,                      // Transcription wait time
    "analyzer_params": {}                // Analyzer-specific params
  }
}
```

**Available Analyzers:**

- `local_smart_turn_v3` ⭐ - Latest ONNX model (recommended)
- `local_smart_turn_v2` - Previous ONNX model
- `local_smart_turn_v1` - Original ONNX model
- `local_coreml` - CoreML model (macOS/iOS only)
- `fal` - Cloud-based (requires API key)
- `krisp_viva` - Krisp integration

**Dependencies:**

```bash
# Install required analyzer
pip install 'pipecat-ai[local-smart-turn-v3]'  # For v3
pip install 'pipecat-ai[fal]'                  # For Fal
pip install 'pipecat-ai[local-coreml-smart-turn]'  # For CoreML
```

**Cloud Analyzer (Fal) Example:**

```json
{
  "type": "turn_analyzer",
  "params": {
    "analyzer": "fal",
    "timeout": 0.5,
    "analyzer_params": {
      "api_key": "${FAL_API_KEY}"  // Environment variable interpolation
    }
  }
}
```

#### 3. External

```json
{
  "type": "external",
  "params": {
    "timeout": 0.5
  }
}
```

- External processor controls turn stop
- For custom logic or STT with built-in turn detection

### Mute Strategies

Control when user input is ignored:

#### 1. Always

```json
{
  "type": "always"
}
```

- Mutes user whenever bot speaks
- Prevents all interruptions

#### 2. First Speech

```json
{
  "type": "first_speech"
}
```

- Mutes only during bot's first speech
- Allows interruptions after greeting

#### 3. Function Call

```json
{
  "type": "function_call"
}
```

- Mutes during LLM function/tool execution
- Prevents interrupting API calls

#### 4. Mute Until First Bot Complete

```json
{
  "type": "mute_until_first_bot_complete"
}
```

- Mutes until bot completes first turn
- Similar to first_speech

### Keyword Filter Configuration

Control which keywords are filtered when bot is busy:

**Keyword Filter** - State-aware filtering

```json
{
  "keyword_filter_config": {
    "enabled": true,
    "keywords": ["hello", "hey", "hola"],
    "match_mode": "exact",
    "case_sensitive": false,
    "remove_punctuation": true
  }
}
```

**How it works:**
- Filters configured keywords ONLY when bot is busy (speaking, processing LLM, or executing functions)
- Allows keywords when bot is idle (e.g., initial "hello" greeting)
- Prevents false triggers from repeated greetings during processing delays

**Configuration Options:**

- **keywords**: List of keywords to filter (e.g., `["hello", "hey", "are you there"]`)
- **match_mode**:
  - `"exact"` - Exact word match (default, recommended)
  - `"contains"` - Substring match (more aggressive)
- **case_sensitive**: Whether matching is case-sensitive (default: `false`)
- **remove_punctuation**: Remove punctuation before matching (default: `true`)

**Use Cases:**

1. **Prevent Impatient Greetings**
   ```json
   {
     "keyword_filter_config": {
       "enabled": true,
       "keywords": ["hello", "hey", "anybody there"],
       "match_mode": "exact"
     }
   }
   ```
   Filters "hello", "hey" when bot is processing, but allows initial greeting.

2. **Filter Checking Phrases**
   ```json
   {
     "keyword_filter_config": {
       "enabled": true,
       "keywords": ["are you there", "hello", "can you hear me"],
       "match_mode": "contains"
     }
   }
   ```
   Filters phrases like "are you there?" during API calls or long processing.

**Important Notes:**
- Only filters **final** transcriptions (not interim)
- Bot must be busy for filtering to activate
- First greeting at call start is never filtered (bot is idle)
- Logs filtered keywords for debugging

### Timeout Configuration

```json
{
  "user_turn_stop_timeout": 5.0  // 1.0 to 30.0 seconds
}
```

Safety timeout to force-stop user turn:

- **3-4s**: Fast-paced sales, quick responses
- **5-7s**: General conversations (recommended)
- **7-10s**: Elderly users, accessibility, thoughtful responses

## Use Case Examples

### Fast-Paced Sales

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "start_strategies": [{"type": "vad"}],
    "stop_strategies": [{
      "type": "turn_analyzer",
      "params": {"analyzer": "local_smart_turn_v3", "timeout": 0.3}
    }],
    "user_turn_stop_timeout": 3.0
  }
}
```

### Customer Support (Natural Pauses)

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "start_strategies": [
      {"type": "min_words", "params": {"min_words": 3}}
    ],
    "stop_strategies": [{
      "type": "turn_analyzer",
      "params": {"analyzer": "local_smart_turn_v3"}
    }],
    "user_turn_stop_timeout": 7.0
  }
}
```

### Elderly/Healthcare

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "stop_strategies": [{
      "type": "turn_analyzer",
      "params": {"analyzer": "local_smart_turn_v3"}
    }],
    "user_turn_stop_timeout": 10.0
  }
}
```

### Information Delivery (No Interruptions)

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "mute_strategies": [{"type": "always"}]
  }
}
```

### API-Heavy Conversations

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "stop_strategies": [{
      "type": "turn_analyzer",
      "params": {"analyzer": "local_smart_turn_v3"}
    }],
    "mute_strategies": [{"type": "function_call"}]
  }
}
```

### Prevent Repeated Greetings

```json
{
  "keyword_filter_config": {
    "enabled": true,
    "keywords": ["hello", "hey", "hola", "anybody there"],
    "match_mode": "exact",
    "case_sensitive": false,
    "remove_punctuation": true
  },
  "turn_strategy_config": {
    "enabled": true,
    "stop_strategies": [{
      "type": "turn_analyzer",
      "params": {"analyzer": "local_smart_turn_v3"}
    }],
    "user_turn_stop_timeout": 5.0
  }
}
```

## Configuration Priority

Configuration is resolved in this order (highest to lowest):

1. **Template JSON** - `turn_strategy_config` in template
2. **Code Defaults** - Pipecat defaults (VAD + Transcription start, Transcription stop)

### Configuration via Template

Turn strategies are configured via template JSON:

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "stop_strategies": [{
      "type": "turn_analyzer",
      "params": {"analyzer": "local_smart_turn_v3"}
    }]
  }
}
```

## Validation and Error Handling

### Config Validation

Invalid configurations are rejected with clear error messages:

```json
{
  "stop_strategies": [
    {
      "type": "turn_analyzer",
      "params": {
        "analyzer": "invalid_analyzer"  // ❌ Error: Invalid analyzer type
      }
    }
  ]
}
```

### Missing Dependencies

If analyzer dependencies aren't installed:

```
Turn analyzer 'local_smart_turn_v3' is not available.

To use this analyzer, install the required dependencies:
    pip install 'pipecat-ai[local-smart-turn-v3]'

Alternatively, use a different analyzer type in your template configuration.
```

### Graceful Fallback

If configuration is invalid, system falls back to Redis config or defaults.

## Testing Strategies

### 1. Test Individual Strategy

Create test template with single strategy:

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "stop_strategies": [{
      "type": "transcription",
      "params": {"timeout": 0.5}
    }]
  }
}
```

### 2. A/B Testing

Create two templates with different strategies:

- Template A: Smart turn with 5s timeout
- Template B: Smart turn with 3s timeout

Compare conversation quality and latency.

### 3. Debugging

Disable strategies to test baseline:

```json
{
  "turn_strategy_config": {
    "enabled": false  // Uses pipecat defaults
  }
}
```

## Best Practices

1. **Start Simple** - Begin with default smart turn, adjust based on feedback
2. **Monitor Latency** - Smart turn adds ~100-200ms, adjust if needed
3. **User Segmentation** - Different strategies for different user types
4. **Test Combinations** - Some strategies work better together
5. **Log Strategy Usage** - Track which strategies perform best

## Troubleshooting

### Bot Interrupts Mid-Sentence

**Solution**: Increase timeout or use turn analyzer

```json
{
  "stop_strategies": [{
    "type": "turn_analyzer",
    "params": {"analyzer": "local_smart_turn_v3"}
  }],
  "user_turn_stop_timeout": 7.0
}
```

### Bot Waits Too Long

**Solution**: Decrease timeout or use simpler strategy

```json
{
  "stop_strategies": [{
    "type": "transcription",
    "params": {"timeout": 0.3}
  }],
  "user_turn_stop_timeout": 3.0
}
```

### Too Many False Starts

**Solution**: Use min_words strategy

```json
{
  "start_strategies": [{
    "type": "min_words",
    "params": {"min_words": 3}
  }]
}
```

### Import Errors

**Solution**: Install required dependencies

```bash
pip install 'pipecat-ai[local-smart-turn-v3]'
```

### Keyword Filter Not Working

**Problem**: Initial greeting "hello" is being filtered

**Solution**: This is expected behavior - keyword filter only activates when bot is busy. If initial "hello" is filtered, the bot is likely still processing. Check if:
- Bot is speaking or processing when "hello" arrives
- Initial greeting is configured correctly

**Problem**: Keywords not being filtered during delays

**Solution**: Ensure bot state is being tracked correctly:

```json
{
  "keyword_filter_config": {
    "enabled": true,
    "keywords": ["hello", "hey"],
    "match_mode": "exact"
  }
}
```

Check logs for "BusyStateKeywordFilter: Filtered transcription" messages.

**Problem**: Too many false positives (legitimate phrases filtered)

**Solution**: Switch from "contains" to "exact" match mode:

```json
{
  "keyword_filter_config": {
    "match_mode": "exact"  // More precise than "contains"
  }
}
```

## Advanced Topics

### Multiple Start Strategies

You can combine multiple start strategies (first to trigger wins):

```json
{
  "start_strategies": [
    {"type": "vad"},
    {"type": "min_words", "params": {"min_words": 2}}
  ]
}
```

### Multiple Mute Strategies

Combine muting conditions:

```json
{
  "mute_strategies": [
    {"type": "first_speech"},
    {"type": "function_call"}
  ]
}
```

### Custom Timeout Per Template

Different templates can have different timeouts:

```json
// Sales template
{"user_turn_stop_timeout": 3.0}

// Support template
{"user_turn_stop_timeout": 7.0}
```

## Examples Repository

See `/app/ai/voice/agents/breeze_buddy/examples/templates/turn-strategy-examples.json` for 10+ complete configuration examples covering:

- Simple smart turn
- False start prevention
- Cloud-based analyzers
- Muting strategies
- User-specific configurations
- External control
- And more!

## References

- **Architecture Design**: `/docs/TURN_STRATEGY_ARCHITECTURE_DESIGN.md`
- **Pipecat Strategies Reference**: `/docs/PIPECAT_TURN_STRATEGIES_REFERENCE.md`
- **User Guide**: `/docs/TURN_STRATEGIES.md`
- **Example Templates**: `/app/ai/voice/agents/breeze_buddy/examples/templates/turn-strategy-examples.json`

## Support

For issues or questions:
1. Check logs for validation errors
2. Review example configurations
3. Verify dependencies are installed
4. Test with simpler configuration first
