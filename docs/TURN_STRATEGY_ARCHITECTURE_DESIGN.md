# Modular Turn Strategy Architecture - Design Document

## Overview

This document outlines the design for a fully modular, template-configurable turn strategy system that supports all Pipecat turn strategies.

## Goals

1. **Complete Strategy Support**: All Pipecat turn strategies available
2. **Template-Level Configuration**: Each template can define its own strategies
3. **Modular & Extensible**: Easy to add new strategies without code changes
4. **Multiple Configuration Layers**: Template → Redis → Code defaults
5. **Type-Safe**: Proper validation and error handling
6. **Backward Compatible**: Existing templates continue to work

## Architecture Design

### 1. Configuration Layers (Priority Order)

```
Template JSON Config (highest priority)
    ↓
Redis Dynamic Config
    ↓
Code Defaults (lowest priority)
```

### 2. Template Configuration Schema

```json
{
  "configurations": {
    "turn_strategy_config": {
      "enabled": true,
      "start_strategies": [
        {
          "type": "vad",
          "params": {}
        },
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
            "analyzer": "local_smart_turn_v3",
            "timeout": 0.5,
            "analyzer_params": {}
          }
        }
      ],
      "mute_strategies": [
        {
          "type": "function_call"
        }
      ],
      "user_turn_stop_timeout": 5.0
    }
  }
}
```

### 3. Component Architecture

```
┌─────────────────────────────────────────────────┐
│         Template Configuration                   │
│  (turn_strategy_config in template JSON)        │
└─────────────────┬───────────────────────────────┘
                  │
                  ↓
┌─────────────────────────────────────────────────┐
│      TurnStrategyConfigBuilder                   │
│  - Parse template config                         │
│  - Merge with Redis defaults                     │
│  - Validate configuration                        │
└─────────────────┬───────────────────────────────┘
                  │
                  ↓
┌─────────────────────────────────────────────────┐
│         Strategy Factories                       │
│  ┌───────────────────────────────────────────┐  │
│  │ StartStrategyFactory                      │  │
│  │  - create_vad_strategy()                  │  │
│  │  - create_transcription_strategy()        │  │
│  │  - create_min_words_strategy()            │  │
│  │  - create_external_strategy()             │  │
│  └───────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────┐  │
│  │ StopStrategyFactory                       │  │
│  │  - create_transcription_strategy()        │  │
│  │  - create_turn_analyzer_strategy()        │  │
│  │  - create_external_strategy()             │  │
│  └───────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────┐  │
│  │ TurnAnalyzerFactory                       │  │
│  │  - create_local_smart_turn_v3()           │  │
│  │  - create_local_smart_turn_v2()           │  │
│  │  - create_local_coreml()                  │  │
│  │  - create_fal_analyzer()                  │  │
│  │  - create_krisp_viva()                    │  │
│  └───────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────┐  │
│  │ MuteStrategyFactory                       │  │
│  │  - create_always_mute()                   │  │
│  │  - create_first_speech_mute()             │  │
│  │  - create_function_call_mute()            │  │
│  │  - create_mute_until_first_bot()          │  │
│  └───────────────────────────────────────────┘  │
└─────────────────┬───────────────────────────────┘
                  │
                  ↓
┌─────────────────────────────────────────────────┐
│      UserTurnStrategies Instance                 │
│  (Ready to use in LLMUserAggregatorParams)      │
└─────────────────────────────────────────────────┘
```

### 4. File Structure

```
app/ai/voice/agents/breeze_buddy/
├── agent/
│   ├── turn_strategies/
│   │   ├── __init__.py
│   │   ├── config.py              # Config models & builder
│   │   ├── factories/
│   │   │   ├── __init__.py
│   │   │   ├── start_factory.py   # Start strategy factory
│   │   │   ├── stop_factory.py    # Stop strategy factory
│   │   │   ├── analyzer_factory.py # Turn analyzer factory
│   │   │   └── mute_factory.py    # Mute strategy factory
│   │   ├── builder.py             # Main strategy builder
│   │   └── validators.py          # Config validation
│   └── turn_strategies.py         # Legacy, for backward compat
├── template/
│   └── types.py                   # Add TurnStrategyConfig models
└── examples/
    └── templates/
        └── turn-strategy-examples.json  # Configuration examples
```

### 5. Type Definitions

```python
# In template/types.py

class StartStrategyType(str, Enum):
    VAD = "vad"
    TRANSCRIPTION = "transcription"
    MIN_WORDS = "min_words"
    EXTERNAL = "external"

class StopStrategyType(str, Enum):
    TRANSCRIPTION = "transcription"
    TURN_ANALYZER = "turn_analyzer"
    EXTERNAL = "external"

class TurnAnalyzerType(str, Enum):
    LOCAL_SMART_TURN_V3 = "local_smart_turn_v3"
    LOCAL_SMART_TURN_V2 = "local_smart_turn_v2"
    LOCAL_SMART_TURN_V1 = "local_smart_turn_v1"
    LOCAL_COREML = "local_coreml"
    FAL = "fal"
    KRISP_VIVA = "krisp_viva"

class MuteStrategyType(str, Enum):
    ALWAYS = "always"
    FIRST_SPEECH = "first_speech"
    FUNCTION_CALL = "function_call"
    MUTE_UNTIL_FIRST_BOT = "mute_until_first_bot_complete"

class StartStrategyConfig(BaseModel):
    type: StartStrategyType
    params: Dict[str, Any] = {}

class StopStrategyConfig(BaseModel):
    type: StopStrategyType
    params: Dict[str, Any] = {}

class MuteStrategyConfig(BaseModel):
    type: MuteStrategyType

class TurnStrategyConfigModel(BaseModel):
    enabled: bool = False
    start_strategies: Optional[List[StartStrategyConfig]] = None
    stop_strategies: Optional[List[StopStrategyConfig]] = None
    mute_strategies: Optional[List[MuteStrategyConfig]] = None
    user_turn_stop_timeout: float = 5.0
```

### 6. Redis Configuration (Global Defaults)

```python
# In app/core/config/dynamic.py

async def BB_DEFAULT_TURN_STRATEGY_CONFIG() -> dict:
    """Returns default turn strategy config from Redis"""
    default = {
        "enabled": False,
        "start_strategies": [
            {"type": "vad", "params": {}},
            {"type": "transcription", "params": {"use_interim": True}}
        ],
        "stop_strategies": [
            {"type": "transcription", "params": {"timeout": 0.5}}
        ],
        "mute_strategies": [],
        "user_turn_stop_timeout": 5.0
    }
    return await get_config("BB_DEFAULT_TURN_STRATEGY_CONFIG", default, dict)
```

### 7. Factory Implementation Pattern

```python
# Example: start_factory.py

class StartStrategyFactory:
    """Factory for creating turn start strategies."""

    @staticmethod
    def create(config: StartStrategyConfig) -> BaseUserTurnStartStrategy:
        """Create a start strategy from configuration."""
        if config.type == StartStrategyType.VAD:
            return StartStrategyFactory.create_vad_strategy(**config.params)
        elif config.type == StartStrategyType.MIN_WORDS:
            return StartStrategyFactory.create_min_words_strategy(**config.params)
        # ... etc
        else:
            raise ValueError(f"Unknown start strategy type: {config.type}")

    @staticmethod
    def create_vad_strategy(**kwargs) -> VADUserTurnStartStrategy:
        """Create VAD-based start strategy."""
        return VADUserTurnStartStrategy(**kwargs)

    @staticmethod
    def create_min_words_strategy(
        min_words: int = 3,
        use_interim: bool = True,
        **kwargs
    ) -> MinWordsUserTurnStartStrategy:
        """Create minimum words start strategy."""
        return MinWordsUserTurnStartStrategy(
            min_words=min_words,
            use_interim=use_interim,
            **kwargs
        )
```

### 8. Main Builder Usage

```python
# In pipeline.py

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.builder import (
    build_turn_strategies_from_template
)

# Build strategies from template config
turn_strategies, user_turn_stop_timeout = await build_turn_strategies_from_template(
    template=template,
    is_daily_mode=is_daily_mode
)

if turn_strategies:
    user_params_dict["user_turn_strategies"] = turn_strategies
    user_params_dict["user_turn_stop_timeout"] = user_turn_stop_timeout
```

## Configuration Examples

### Example 1: Simple Smart Turn (Current Implementation)

```json
{
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
```

### Example 2: Prevent False Starts + Smart Turn

```json
{
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
          "analyzer": "local_smart_turn_v3",
          "timeout": 0.5
        }
      }
    ],
    "user_turn_stop_timeout": 7.0
  }
}
```

### Example 3: Cloud-Based with Muting

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "stop_strategies": [
      {
        "type": "turn_analyzer",
        "params": {
          "analyzer": "fal",
          "timeout": 0.5,
          "analyzer_params": {
            "api_key": "${FAL_API_KEY}"
          }
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
```

### Example 4: No Interruptions During Bot Speech

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "mute_strategies": [
      {
        "type": "always"
      }
    ]
  }
}
```

### Example 5: External Control

```json
{
  "turn_strategy_config": {
    "enabled": true,
    "start_strategies": [
      {
        "type": "external"
      }
    ],
    "stop_strategies": [
      {
        "type": "external",
        "params": {
          "timeout": 0.5
        }
      }
    ]
  }
}
```

## Implementation Phases

### Phase 1: Core Infrastructure
- [ ] Create type definitions in template/types.py
- [ ] Create factory base classes
- [ ] Create config builder and validators

### Phase 2: Strategy Factories
- [ ] Implement StartStrategyFactory
- [ ] Implement StopStrategyFactory
- [ ] Implement TurnAnalyzerFactory
- [ ] Implement MuteStrategyFactory

### Phase 3: Integration
- [ ] Update template model to include turn_strategy_config
- [ ] Update pipeline.py to use new builder
- [ ] Add Redis default configuration
- [ ] Backward compatibility handling

### Phase 4: Testing & Documentation
- [ ] Create example templates
- [ ] Update documentation
- [ ] Test all strategy combinations
- [ ] Integration testing

## Backward Compatibility

Existing behavior is preserved:
1. If no turn_strategy_config in template → use pipecat defaults
2. Old templates without turn strategy config continue to function
3. Templates can opt-in to turn strategies by adding configuration

## Benefits

1. **Flexibility**: Any combination of strategies per template
2. **No Code Changes**: Add strategies via JSON config
3. **Easy Testing**: Test different strategies without deployment
4. **Per-Use-Case**: Different strategies for different scenarios
5. **Future-Proof**: Easy to add new strategies as Pipecat evolves

## Migration Path

1. **Deploy**: New code is deployed
2. **Test**: Use new config in test templates
3. **Migrate**: Gradually migrate templates to new config
4. **Deprecate**: Eventually deprecate old Redis-only config

## Security Considerations

1. **API Keys**: Support environment variable interpolation (${VAR_NAME})
2. **Validation**: Strict config validation to prevent malformed configs
3. **Resource Limits**: Prevent excessive strategy combinations
4. **Error Handling**: Graceful fallback on invalid configs

## Performance Considerations

1. **Lazy Loading**: Only import strategies that are configured
2. **Caching**: Cache analyzer instances per session
3. **Resource Management**: Proper cleanup of analyzer resources
4. **Metrics**: Track which strategies are used most

## Open Questions

1. Should we support strategy hot-reloading during a session?
2. Should we add a strategy testing/simulation mode?
3. Should we expose strategy metrics via API?
4. Should we support custom user-defined strategies?
