# Turn Strategies in Clairvoyance

## Overview

Turn strategies provide intelligent turn-taking management in voice conversations. Instead of relying purely on Voice Activity Detection (VAD) to determine when a user has stopped speaking, turn strategies use machine learning models to understand when a user has actually finished their conversational turn.

## What Problem Does This Solve?

**Without Turn Strategies:**
- Bot interrupts user on every brief pause (e.g., 200ms of silence)
- User cannot take natural thinking breaks mid-sentence
- Conversation feels rushed and robotic
- Natural speech patterns like "I want to... hmm... book a flight" get interrupted

**With Turn Strategies:**
- Bot waits for natural conversational breaks
- Handles mid-sentence pauses gracefully
- Understands when user is thinking vs when they're done speaking
- More natural, human-like conversation flow
- Still has timeout safety to prevent infinite waiting

## Architecture

### Two Layers of Speech Detection

#### Layer 1: VAD Analyzer (Low-Level)
- **Purpose**: Detect presence/absence of speech in audio
- **Current Implementation**: `SileroVADAnalyzer` with configurable `VADParams`
- **Output**:
  - `VADUserStartedSpeakingFrame` - when speech detected
  - `VADUserStoppedSpeakingFrame` - after silence duration (e.g., 0.2s)
- **Limitation**: Can't distinguish mid-sentence pauses from end-of-turn

#### Layer 2: Turn Strategies (High-Level)
- **Purpose**: Determine if user has truly finished their conversational turn
- **Implementation**: ML-based turn analyzer (LocalSmartTurnAnalyzerV3)
- **Intelligence**: Analyzes speech patterns, intonation, prosody
- **Safety**: Includes timeout mechanism to prevent infinite waiting

### Turn Strategy Components

1. **Start Strategies**: Determine when user turn begins
   - Default: VAD detection + first transcription

2. **Stop Strategies**: Determine when user turn ends
   - Default (without smart turn): Final transcription after VAD stop
   - Smart Turn (with ML): Analyzer determines turn completeness

3. **Turn Controller**: Manages turn lifecycle with timeout safety net

## Configuration

Turn strategies are configured via **template JSON configuration**. This provides fine-grained control over all strategy types.

### Template Configuration

Add a `turn_strategy_config` section to your template JSON:

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "user_turn_stop_timeout": 5.0,
    "stop_strategies": [
      {
        "type": "turn_analyzer",
        "analyzer": "local_smart_turn_v3",
        "timeout": 0.5
      }
    ]
  }
}
```

### Key Configuration Options

#### enabled
- **Type**: Boolean
- **Default**: `false`
- **Description**: Enable turn strategies for this template
- **Recommendation**: Enable for more natural conversations

#### user_turn_stop_timeout
- **Type**: Float (seconds)
- **Default**: `5.0`
- **Description**: Maximum time to wait before force-stopping user turn (safety timeout)
- **Range**: 3.0 - 10.0 seconds
- **Recommendation**:
  - 3-5s for faster responses with risk of premature cutoff
  - 5-7s for natural pauses (recommended)
  - 7-10s for users who need more thinking time

**Important**: This timeout is the safety net. If the ML model keeps saying the turn is incomplete, this timeout will eventually end the turn anyway.

#### stop_strategies
- **Type**: Array of strategy configurations
- **Description**: Strategies to determine when user has finished speaking
- **Analyzer timeout**: Time to wait for transcription after turn analysis (0.3-1.0s)

For comprehensive configuration options including all 15+ strategy types, see:
- `docs/MODULAR_TURN_STRATEGY_USAGE.md` - Usage guide with 12+ examples
- `docs/PIPECAT_TURN_STRATEGIES_REFERENCE.md` - Complete reference
- `app/ai/voice/agents/breeze_buddy/examples/templates/turn-strategy-examples.json` - Example templates

## How It Works

### Flow Diagram

```
Audio Input
    │
    ↓
Transport → SileroVADAnalyzer
    │           │
    │           ├→ VADUserStartedSpeakingFrame
    │           └→ VADUserStoppedSpeakingFrame (after 0.2s silence)
    ↓
STT Service → TranscriptionFrame
    │
    ↓
LLMUserAggregator
    │
    ├─→ UserTurnController
    │     │
    │     ├─→ Start Strategies
    │     │     └─→ Triggers on VAD/Transcription → User Turn Started
    │     │
    │     └─→ Stop Strategies
    │           │
    │           └─→ TurnAnalyzerUserTurnStopStrategy
    │                 ├─ Continuously feeds audio to ML model
    │                 ├─ On VAD stop: calls analyzer.analyze_end_of_turn()
    │                 ├─ Model returns: COMPLETE or INCOMPLETE
    │                 ├─ Waits for transcription (timeout: 0.5s)
    │                 └─ Triggers stop only if:
    │                       1. Model says COMPLETE
    │                       2. Transcription received
    │
    │     └─→ Safety Timeout (5s)
    │           └─→ Force stops turn if no activity
    │
    ├─→ Accumulate transcriptions during active turn
    │
    └─→ On turn stopped: Push to LLM
```

### Step-by-Step Process

1. **User starts speaking**
   - VAD detects speech → `VADUserStartedSpeakingFrame`
   - Start strategy triggers → User turn begins
   - Aggregator starts collecting transcriptions

2. **User pauses mid-sentence** (e.g., "I want to... hmm...")
   - VAD detects silence → `VADUserStoppedSpeakingFrame`
   - Turn analyzer runs ML inference on audio
   - Model returns: `INCOMPLETE`
   - System continues waiting (resets timeout)

3. **User continues speaking** ("...book a flight")
   - VAD detects speech again → `VADUserStartedSpeakingFrame`
   - Timeout resets
   - Aggregator continues collecting

4. **User finishes turn** ("...to London")
   - VAD detects silence → `VADUserStoppedSpeakingFrame`
   - Turn analyzer runs ML inference
   - Model returns: `COMPLETE`
   - System waits for final transcription (max 0.5s)
   - Transcription received
   - **Turn stops → LLM triggered**

5. **Safety timeout scenario**
   - If no activity for 5 seconds (configurable)
   - Turn is force-stopped regardless of ML model
   - Prevents infinite waiting

### Timeout Behavior

The timeout mechanism has important nuances:

- **Resets on ANY activity**: VAD frames, transcriptions, turn events
- **Only counts silence**: Timer only runs when user is NOT speaking
- **Prevents mid-speech cutoff**: Won't fire if VAD detects active speech
- **Configurable**: Adjust based on your user base's speaking patterns

Example timeline:
```
T=0s:   User starts speaking
T=2s:   User pauses (VAD stop) - Timer starts, ML says INCOMPLETE
T=2.5s: User continues - Timer resets
T=5s:   User pauses again - Timer starts, ML says INCOMPLETE
T=10s:  Timeout! (5s elapsed since last activity) - Turn force-stopped
```

## Implementation Details

### Key Files

1. **`app/ai/voice/agents/breeze_buddy/agent/turn_strategies/`** - Modular turn strategy system
   - `builder.py` - Builds strategies from template configuration
   - `config.py` - Pydantic models for all strategy types
   - `validators.py` - Configuration validation
   - `factories/` - Strategy factories (start, stop, analyzer, mute)

2. **`app/ai/voice/agents/breeze_buddy/agent/pipeline.py`**
   - Builds turn strategies from template
   - Passes strategies to LLMUserAggregatorParams
   - Requires pipecat >= 0.0.99

3. **`app/ai/voice/agents/breeze_buddy/template/types.py`**
   - Template configuration models
   - Turn strategy configuration schema

### Resource Management

The `LocalSmartTurnAnalyzer` wrapper ensures proper ONNX model cleanup:
- Inherits from `LocalSmartTurnAnalyzerV3`
- Implements `shutdown()` for resource cleanup
- Prevents memory leaks from ONNX session

## Testing & Validation

### Manual Testing

1. **Enable turn strategies in your template JSON**:
   ```json
   {
     "turn_strategy_config": {
       "enabled": true,
       "user_turn_stop_timeout": 5.0,
       "stop_strategies": [
         {
           "type": "turn_analyzer",
           "analyzer": "local_smart_turn_v3",
           "timeout": 0.5
         }
       ]
     }
   }
   ```

2. **Test scenarios**:

   **Scenario A: Mid-sentence pause**
   - Say: "I want to... [pause 1s] ...book a flight"
   - Expected: Bot waits for you to continue, doesn't interrupt

   **Scenario B: Finished speaking**
   - Say: "I want to book a flight to London"
   - Expected: Bot responds within 1-2s after you finish

   **Scenario C: Long thinking pause**
   - Say: "I want to..." [pause 6s]
   - Expected: Bot responds after timeout (5s by default)

### Monitoring

Check logs for turn strategy events:
```bash
grep "Smart turn detection enabled" /path/to/logs
grep "User started speaking" /path/to/logs
grep "User stopped speaking" /path/to/logs
```

### Performance Metrics

Expected latencies with smart turn enabled:
- **Mid-sentence pause**: No interruption, waits for continuation
- **End of turn**: Response within 1-2s (VAD stop + ML inference + transcription)
- **Timeout scenario**: Response after 5s of inactivity

## Troubleshooting

### Bot Still Interrupts Mid-Sentence

**Possible causes**:
1. Turn strategies not enabled: Check `enabled: true` in template `turn_strategy_config`
2. Timeout too low: Increase `user_turn_stop_timeout` to 7s in template
3. VAD too sensitive: Adjust VAD `stop_secs` in VAD configuration
4. No stop strategies configured: Add a `turn_analyzer` stop strategy

### Bot Waits Too Long to Respond

**Possible causes**:
1. Timeout too high: Reduce `user_turn_stop_timeout` to 3s in template
2. ML model incorrectly detecting incomplete: This is expected behavior for natural pauses
3. Analyzer timeout too high: Reduce analyzer `timeout` to 0.3s in stop strategy config

### Version Requirements

Turn strategies require **pipecat >= 0.0.99**. Ensure you have the correct version installed:
```bash
pip install --upgrade "pipecat-ai[local-smart-turn-v3]"
```

## Best Practices

1. **Start conservative**: Begin with default timeouts and adjust based on user feedback

2. **Monitor latency**: Watch for increased response times (smart turn adds ~100-200ms)

3. **A/B test**: Compare conversations with/without smart turn to measure quality

4. **User segment tuning**: Different user bases may need different timeout values:
   - Fast-paced sales: 3-4s timeout
   - Customer support: 5-7s timeout
   - Elderly users: 7-10s timeout

5. **Always have timeout**: Never disable the safety timeout - prevents system hangs

## Future Enhancements

Potential future improvements:
- Dynamic timeout adjustment based on user speaking patterns
- Confidence scores from ML model for better decision making
- Per-user timeout customization
- Integration with other turn start strategies (e.g., minimum words)

## References

- Pipecat Turn Strategies Documentation: `/root/.claude/projects/-home-user-clairvoyance/memory/turn-strategies-architecture.md`
- LocalSmartTurnAnalyzerV3 Model: Built on ONNX, analyzes ~8s of audio for turn completion
- Research: Turn-taking in conversation is a fundamental aspect of human communication
