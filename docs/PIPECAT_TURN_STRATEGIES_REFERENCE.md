# Pipecat Turn Strategies Reference

This document provides a comprehensive overview of all turn strategies and turn analyzers available in Pipecat.

## Overview

Turn strategies in Pipecat are organized into three categories:
1. **Turn Start Strategies** - Determine when a user's turn begins
2. **Turn Stop Strategies** - Determine when a user's turn ends
3. **User Mute Strategies** - Control when user input should be ignored

## 1. Turn Start Strategies

Located in: `pipecat.turns.user_start`

### VADUserTurnStartStrategy (Default)
```python
from pipecat.turns.user_start import VADUserTurnStartStrategy

UserTurnStrategies(
    start=[VADUserTurnStartStrategy()]
)
```
- **Trigger**: When VAD detects speech (`VADUserStartedSpeakingFrame`)
- **Use Case**: Immediate response to voice activity
- **Pros**: Fastest detection, no processing delay
- **Cons**: Can trigger on background noise

### TranscriptionUserTurnStartStrategy (Default)
```python
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy

UserTurnStrategies(
    start=[TranscriptionUserTurnStartStrategy(use_interim=True)]
)
```
- **Trigger**: When transcription is received (final or interim)
- **Parameters**:
  - `use_interim` (bool, default=True): Use interim transcriptions for earlier detection
- **Use Case**: Fallback when VAD fails (e.g., whispered speech)
- **Pros**: Catches soft-spoken input VAD might miss
- **Cons**: Slightly slower than VAD

### MinWordsUserTurnStartStrategy
```python
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy

UserTurnStrategies(
    start=[MinWordsUserTurnStartStrategy(min_words=3, use_interim=True)]
)
```
- **Trigger**: After user speaks N words
- **Parameters**:
  - `min_words` (int): Minimum words required to trigger
  - `use_interim` (bool, default=True): Consider interim transcriptions
- **Use Case**: Prevent interruption from brief utterances like "um", "uh"
- **Behavior**:
  - Requires only 1 word if bot is not speaking
  - Requires `min_words` if interrupting bot
- **Pros**: Reduces false starts from filler words
- **Cons**: Adds latency based on speaking speed

### ExternalUserTurnStartStrategy
```python
from pipecat.turns.user_start import ExternalUserTurnStartStrategy

UserTurnStrategies(
    start=[ExternalUserTurnStartStrategy()]
)
```
- **Trigger**: External processor emits `UserStartedSpeakingFrame`
- **Use Case**: Custom turn detection logic, external services
- **Pros**: Full control over turn detection
- **Cons**: Requires external implementation
- **Note**: Disables interruptions and user speaking frames by default

---

## 2. Turn Stop Strategies

Located in: `pipecat.turns.user_stop`

### TranscriptionUserTurnStopStrategy (Default)
```python
from pipecat.turns.user_stop import TranscriptionUserTurnStopStrategy

UserTurnStrategies(
    stop=[TranscriptionUserTurnStopStrategy(timeout=0.5)]
)
```
- **Trigger**: When transcription received after VAD stops
- **Parameters**:
  - `timeout` (float, default=0.5): Wait time for delayed transcriptions
- **Use Case**: Simple turn detection without ML
- **Pros**: Fast, no model inference required
- **Cons**: Can interrupt mid-thought pauses
- **Behavior**: Waits for final transcription (not interim) and VAD to stop

### TurnAnalyzerUserTurnStopStrategy (Smart Turn) ⭐
```python
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

UserTurnStrategies(
    stop=[TurnAnalyzerUserTurnStopStrategy(
        turn_analyzer=LocalSmartTurnAnalyzerV3(),
        timeout=0.5
    )]
)
```
- **Trigger**: ML model determines turn is complete
- **Parameters**:
  - `turn_analyzer` (BaseTurnAnalyzer): Turn analyzer instance
  - `timeout` (float, default=0.5): Transcription wait after analysis
- **Use Case**: Natural conversation with mid-sentence pauses
- **Pros**: Intelligently handles thinking pauses
- **Cons**: ~100-200ms added latency for inference
- **Behavior**:
  - Continuously feeds audio to analyzer
  - On VAD stop, runs `analyze_end_of_turn()`
  - Only stops if: model says COMPLETE AND transcription received

### ExternalUserTurnStopStrategy
```python
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy

UserTurnStrategies(
    stop=[ExternalUserTurnStopStrategy(timeout=0.5)]
)
```
- **Trigger**: External processor emits `UserStoppedSpeakingFrame`
- **Parameters**:
  - `timeout` (float, default=0.5): Wait time for transcriptions
- **Use Case**: Custom turn logic, STT services with built-in turn detection
- **Pros**: Leverage external intelligence
- **Cons**: Requires external implementation
- **Note**: Disables user speaking frames by default

---

## 3. Turn Analyzers (for TurnAnalyzerUserTurnStopStrategy)

Located in: `pipecat.audio.turn`

### LocalSmartTurnAnalyzerV3 (Recommended) ⭐
```python
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

analyzer = LocalSmartTurnAnalyzerV3()
```
- **Type**: Local ONNX model (v3)
- **Analysis Window**: ~8 seconds of audio
- **Installation**: `pip install "pipecat-ai[local-smart-turn-v3]"`
- **Pros**: Latest model, best accuracy, runs locally
- **Cons**: Requires ONNX runtime, CPU usage
- **Use Case**: Production deployments

### LocalSmartTurnAnalyzerV2
```python
from pipecat.audio.turn.smart_turn.local_smart_turn_v2 import LocalSmartTurnAnalyzerV2

analyzer = LocalSmartTurnAnalyzerV2()
```
- **Type**: Local ONNX model (v2)
- **Installation**: `pip install "pipecat-ai[local-smart-turn-v2]"`
- **Use Case**: Previous generation model

### LocalSmartTurnAnalyzer (v1)
```python
from pipecat.audio.turn.smart_turn.local_smart_turn import LocalSmartTurnAnalyzer

analyzer = LocalSmartTurnAnalyzer()
```
- **Type**: Local ONNX model (v1)
- **Use Case**: Original model, legacy support

### LocalCoreMLSmartTurnAnalyzer (macOS/iOS)
```python
from pipecat.audio.turn.smart_turn.local_coreml_smart_turn import LocalCoreMLSmartTurnAnalyzer

analyzer = LocalCoreMLSmartTurnAnalyzer()
```
- **Type**: CoreML model
- **Platform**: macOS, iOS only
- **Installation**: `pip install "pipecat-ai[local-coreml-smart-turn]"`
- **Pros**: Optimized for Apple Silicon
- **Use Case**: Native macOS/iOS apps

### FalSmartTurnAnalyzer (Cloud-based)
```python
from pipecat.audio.turn.smart_turn.fal_smart_turn import FalSmartTurnAnalyzer

analyzer = FalSmartTurnAnalyzer(api_key="your-fal-key")
```
- **Type**: Cloud API (fal.ai)
- **Installation**: `pip install "pipecat-ai[fal]"`
- **Pros**: No local compute required, always latest model
- **Cons**: Network latency, API costs, requires internet
- **Use Case**: Serverless deployments, minimal local resources

### KrispVivaTurn (Krisp Integration)
```python
from pipecat.audio.turn.krisp_viva_turn import KrispVivaTurn

analyzer = KrispVivaTurn(params=KrispTurnParams(...))
```
- **Type**: Krisp Viva turn detection
- **Use Case**: Integration with Krisp noise cancellation service

---

## 4. User Mute Strategies

Located in: `pipecat.turns.user_mute`

Mute strategies control when user input should be ignored (muted) during the conversation.

### AlwaysUserMuteStrategy
```python
from pipecat.turns.user_mute import AlwaysUserMuteStrategy

LLMUserAggregatorParams(
    user_mute_strategies=[AlwaysUserMuteStrategy()]
)
```
- **Behavior**: Mutes user whenever bot is speaking
- **Use Case**: Prevent all interruptions
- **Effect**: User cannot interrupt bot at all

### FirstSpeechUserMuteStrategy
```python
from pipecat.turns.user_mute import FirstSpeechUserMuteStrategy

LLMUserAggregatorParams(
    user_mute_strategies=[FirstSpeechUserMuteStrategy()]
)
```
- **Behavior**: Mutes user only during bot's first speech
- **Use Case**: Let bot finish greeting/introduction
- **Effect**: After first bot speech completes, user can interrupt freely

### MuteUntilFirstBotCompleteUserMuteStrategy
```python
from pipecat.turns.user_mute import MuteUntilFirstBotCompleteUserMuteStrategy

LLMUserAggregatorParams(
    user_mute_strategies=[MuteUntilFirstBotCompleteUserMuteStrategy()]
)
```
- **Behavior**: Mutes user until bot completes its first turn
- **Use Case**: Ensure initial bot response is delivered completely

### FunctionCallUserMuteStrategy
```python
from pipecat.turns.user_mute import FunctionCallUserMuteStrategy

LLMUserAggregatorParams(
    user_mute_strategies=[FunctionCallUserMuteStrategy()]
)
```
- **Behavior**: Mutes user while LLM function/tool calls are executing
- **Use Case**: Prevent interruption during API calls, database queries
- **Effect**: User input ignored until function returns result

---

## Common Configuration Examples

### Example 1: Default Behavior
```python
from pipecat.turns.user_turn_strategies import UserTurnStrategies

# No need to specify - these are defaults
strategies = UserTurnStrategies()
# Equivalent to:
# start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()]
# stop=[TranscriptionUserTurnStopStrategy()]
```

### Example 2: Smart Turn Detection (Clairvoyance Implementation)
```python
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

strategies = UserTurnStrategies(
    # Use defaults for start: VAD + Transcription
    stop=[TurnAnalyzerUserTurnStopStrategy(
        turn_analyzer=LocalSmartTurnAnalyzerV3(),
        timeout=0.5
    )]
)
```

### Example 3: Prevent Premature Interruptions
```python
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

strategies = UserTurnStrategies(
    start=[MinWordsUserTurnStartStrategy(min_words=3)],
    stop=[TurnAnalyzerUserTurnStopStrategy(
        turn_analyzer=LocalSmartTurnAnalyzerV3()
    )]
)
```

### Example 4: External Control
```python
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

# Pre-configured for external control
strategies = ExternalUserTurnStrategies()
# Equivalent to:
# start=[ExternalUserTurnStartStrategy()]
# stop=[ExternalUserTurnStopStrategy()]
```

### Example 5: Cloud-based Smart Turn
```python
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.audio.turn.smart_turn.fal_smart_turn import FalSmartTurnAnalyzer

strategies = UserTurnStrategies(
    stop=[TurnAnalyzerUserTurnStopStrategy(
        turn_analyzer=FalSmartTurnAnalyzer(api_key="your-key")
    )]
)
```

### Example 6: Mute During Function Calls
```python
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_mute import FunctionCallUserMuteStrategy
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

params = LLMUserAggregatorParams(
    user_turn_strategies=UserTurnStrategies(
        stop=[TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3()
        )]
    ),
    user_mute_strategies=[FunctionCallUserMuteStrategy()],
    user_turn_stop_timeout=5.0
)
```

---

## Comparison Matrix

| Feature | VAD Start | Transcription Start | MinWords Start | Analyzer Stop | Transcription Stop | External Stop |
|---------|-----------|---------------------|----------------|---------------|-------------------|---------------|
| Latency | ⚡ Fastest | Fast | Slow | Medium | ⚡ Fastest | Variable |
| Accuracy | Low | Medium | High | ⭐ Highest | Medium | Variable |
| CPU Usage | Low | Low | Low | Medium | Low | Low |
| Natural Pauses | ❌ | ❌ | ❌ | ✅ | ❌ | Variable |
| False Starts | High | Medium | ⭐ Lowest | N/A | N/A | N/A |
| Configuration | Simple | Simple | Medium | Complex | Simple | Complex |

## Implementation Notes

1. **Multiple Start Strategies**: Can combine multiple start strategies (e.g., VAD + Transcription) - first one to trigger wins
2. **Multiple Stop Strategies**: Typically use only one stop strategy at a time
3. **Safety Timeout**: Always configure `user_turn_stop_timeout` (3-10s) as safety net
4. **Version Compatibility**: Turn strategies require pipecat >= 0.0.99
5. **Resource Management**: Turn analyzers with ONNX models need proper cleanup via `shutdown()`

## References

- Pipecat GitHub: https://github.com/pipecat-ai/pipecat
- Turn Strategies Documentation: `docs/TURN_STRATEGY_ARCHITECTURE_DESIGN.md`
- Clairvoyance Implementation: `app/ai/voice/agents/breeze_buddy/agent/turn_strategies/`
