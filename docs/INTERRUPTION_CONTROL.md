# Interruption Control — Design Document

## Table of Contents

1. [Overview](#overview)
2. [Current State](#current-state)
3. [PipeCat Capabilities Reference](#pipecat-capabilities-reference)
4. [Interruption Modes](#interruption-modes)
5. [Phased Implementation Plan](#phased-implementation-plan)
6. [Phase 1: Template-Level Mode 1, Mode 3, Min Words](#phase-1)
7. [Phase 2: Node-Level Switching](#phase-2)
8. [Phase 3: Mode 2 — Buffered Speech](#phase-3)

---

## Overview

Control how user interruptions are handled during bot speech at template and node granularity. Three interruption modes plus a minimum-word strategy give template authors fine-grained control over conversation dynamics.

---

## Current State

### How Interruptions Work Today

**Pipeline order** (`app/ai/voice/agents/breeze_buddy/agent/pipeline.py`):
```
transport.input() → stt → TranscriptionGateProcessor → user_aggregator → llm → tts → transport.output() → assistant_aggregator
```

**Turn start detection** — dual-strategy approach:
1. `VADUserTurnStartStrategy` — fires on VAD speech detection (~100ms), only when `BREEZE_BUDDY_ENABLE_VAD=true`
2. `TranscriptionUserTurnStartStrategy` — fires on any interim transcription from Soniox STT (fallback / always-on)

**Turn stop detection**:
- `SpeechTimeoutUserTurnStopStrategy` with `user_speech_timeout=0.0` — triggers immediately when Soniox sends finalized transcript with `<end>` token

**Interruptions are always enabled** — when a user turn starts while the bot is speaking, PipeCat's `LLMUserAggregator` pushes an `InterruptionTaskFrame` upstream, cancelling bot output and processing the new user input.

### Existing Muting Mechanisms

| Mechanism | Location | Purpose |
|-----------|----------|---------|
| `mute_vad()` | `template/vad.py` | Sets VAD confidence to 1.0 (impossible to trigger) |
| `TranscriptionGateProcessor.mute()` | `processors/transcription_gate.py` | Drops all TranscriptionFrames |
| `mute_stt()` | `handlers/internal/stt.py` | Routes to VAD mute or gate mute based on config |
| `_speak_and_wait()` | `handlers/internal/builtin_dispatcher.py` | Mutes STT before bot speaks, unmutes after |
| Keyword filter | `processors/transcription_gate.py` | Drops specific transcriptions while bot is active |

These are used **ad-hoc** (e.g., builtin dispatcher messages), not as a configurable template-level feature.

### Template Configuration Model

`app/ai/voice/agents/breeze_buddy/template/types.py` — `ConfigurationModel` holds:
- `vad_config` — template-level VAD params
- `user_idle_configuration` — idle detection
- `keyword_filter` — transcription filtering
- `noise_filter` — audio input filtering

Node-level override exists only for `vad_config` today (`FlowNodeModel.vad_config`).

---

## PipeCat Capabilities Reference

### User Turn Start Strategies (`pipecat/turns/user_start/`)

| Strategy | Trigger | Key Params |
|----------|---------|------------|
| `VADUserTurnStartStrategy` | `VADUserStartedSpeakingFrame` | `enable_interruptions`, `enable_user_speaking_frames` |
| `TranscriptionUserTurnStartStrategy` | Any transcription frame | `use_interim` (default True) |
| `MinWordsUserTurnStartStrategy` | Transcription with N+ words | `min_words`, `use_interim` (default True) |
| `ExternalUserTurnStartStrategy` | External `UserStartedSpeakingFrame` | Forces `enable_interruptions=False` |

**`MinWordsUserTurnStartStrategy` behavior:**
- When bot IS speaking → requires `min_words` words to trigger interruption
- When bot is NOT speaking → requires only 1 word (always responsive)
- Word count: `len(frame.text.split())`

### User Turn Stop Strategies (`pipecat/turns/user_stop/`)

| Strategy | Mechanism | Key Params |
|----------|-----------|------------|
| `SpeechTimeoutUserTurnStopStrategy` | Silence timeout | `user_speech_timeout` (default 0.6s) |
| `TurnAnalyzerUserTurnStopStrategy` | ML model (SmartTurn v3) | `turn_analyzer` |
| `ExternalUserTurnStopStrategy` | External frame | `timeout` (0.5s) |

### User Mute Strategies (`pipecat/turns/user_mute/`)

| Strategy | Behavior |
|----------|----------|
| `AlwaysUserMuteStrategy` | Mutes user whenever bot is speaking — **prevents all interruptions, discards all frames** |
| `FirstSpeechUserMuteStrategy` | Mutes during bot's first speech only |
| `MuteUntilFirstBotCompleteUserMuteStrategy` | Mutes until bot completes first response |
| `FunctionCallUserMuteStrategy` | Mutes during function call execution |

**When muted, `LLMUserAggregator._maybe_mute_frame()` drops:**
- `InterruptionFrame`, `VADUserStartedSpeakingFrame`, `VADUserStoppedSpeakingFrame`
- `UserStartedSpeakingFrame`, `UserStoppedSpeakingFrame`
- `InputAudioRawFrame`, `InterimTranscriptionFrame`, `TranscriptionFrame`

### Key Frames

| Frame | Purpose |
|-------|---------|
| `InterruptionTaskFrame` | Pushed upstream to cancel bot output |
| `UserStartedSpeakingFrame` | Signals user turn started |
| `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` | Bot speech lifecycle |
| `UserMuteStartedFrame` / `UserMuteStoppedFrame` | Mute state change events |

### BaseUserTurnStartStrategy Constructor

```python
def __init__(self, *, enable_interruptions=True, enable_user_speaking_frames=True):
```

- `enable_interruptions=False` → user turn is detected but does NOT cancel bot speech
- `enable_user_speaking_frames=False` → no `UserStartedSpeakingFrame` emitted

---

## Interruption Modes

### Mode 1: Interruptions Enabled (default)
- User can interrupt bot at any time
- Bot stops speaking, user input processed immediately
- Current production behavior

### Mode 3: Interruptions Disabled — Discard Speech
- User cannot interrupt the bot while it's speaking
- Any user speech during bot's turn is completely discarded
- User must speak again after bot finishes
- Use case: Bot delivers critical info (legal disclaimers, order confirmations)

### Min Words: Interruptions with Threshold
- User can interrupt, but only after speaking N words
- Prevents accidental interruptions from "hmm", "ok", background noise
- When bot is NOT speaking, 1 word is enough (responsive)
- Configurable `min_words` count per template (and later per node)

### Mode 2: Interruptions Disabled — Buffer Speech (Phase 3)
- User cannot interrupt the bot while it's speaking
- Speech during bot's turn is captured and buffered
- Once bot finishes, buffered speech is processed as user input
- Use case: Bot delivers message, then handles what user said during it

---

## Phased Implementation Plan

### Phase 1: Template-Level Mode 1, Mode 3, Min Words

**Scope:**
- Add `interruption_mode` field to `ConfigurationModel` (enum: `enabled`, `disabled_discard`)
- Add `min_words` field to `ConfigurationModel` (optional int)
- Wire into pipeline creation — select appropriate PipeCat strategies based on config
- Default behavior unchanged (Mode 1)

**PipeCat mapping:**
- Mode 1 → current strategies (VAD + Transcription start, no mute strategies)
- Mode 3 → `AlwaysUserMuteStrategy` added to `user_mute_strategies`
- Min Words → replace `TranscriptionUserTurnStartStrategy` with `MinWordsUserTurnStartStrategy(min_words=N)`

**Files to modify:**
1. `app/ai/voice/agents/breeze_buddy/template/types.py` — add config fields
2. `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` — wire strategies based on config
3. Example templates — add examples showing new config

### Phase 2: Node-Level Switching ✅

**Scope:**
- Add `interruption` (InterruptionConfig) to `FlowNodeModel`
- On node transition, dynamically switch mute strategies and turn start strategies
- Hook into existing node transition system (`template/transition.py`)

**Approach — PipeCat runtime strategy switching:**
- **Turn start/stop strategies**: `UserTurnController.update_strategies()` replaces all start/stop strategies at runtime (cleanup → replace → setup lifecycle)
- **Mute strategies**: Direct manipulation of `LLMUserAggregator._params.user_mute_strategies` list with proper cleanup/setup lifecycle calls
- **Reset pattern**: Same as VAD — reset to template defaults first, then apply node override

**Files modified:**
1. `app/ai/voice/agents/breeze_buddy/template/types.py` — added `interruption` field to `FlowNodeModel`
2. `app/ai/voice/agents/breeze_buddy/template/builder.py` — attach interruption config to NodeConfig
3. `app/ai/voice/agents/breeze_buddy/template/interruption.py` — **new** dynamic strategy switching module
4. `app/ai/voice/agents/breeze_buddy/template/transition.py` — call reset + apply on transitions
5. `app/ai/voice/agents/breeze_buddy/agent/__init__.py` — store `default_interruption_config` on bot

### Phase 3: Mode 2 — Buffered Speech

**Scope:**
- Add `disabled_buffer` to interruption mode enum
- Build custom processor that buffers transcription frames while bot is speaking
- On `BotStoppedSpeakingFrame`, flush buffered transcriptions downstream

**Challenge:**
- PipeCat's `AlwaysUserMuteStrategy` drops frames entirely in `LLMUserAggregator`
- Buffer must sit BEFORE the user aggregator to capture frames before they're dropped
- Need to handle: ordering of buffered frames, partial transcriptions, timing

**Approach:**
- Custom `SpeechBufferProcessor` inserted between `TranscriptionGateProcessor` and `user_aggregator`
- In buffer mode: intercepts `TranscriptionFrame` and `InterimTranscriptionFrame`, stores them
- On `BotStoppedSpeakingFrame`: replays stored `TranscriptionFrame`(s) downstream
- Discard interim frames from buffer (only keep finals)

**Files to modify:**
1. `app/ai/voice/agents/breeze_buddy/template/types.py` — extend enum
2. New `app/ai/voice/agents/breeze_buddy/processors/speech_buffer.py`
3. `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` — insert buffer processor

---

## Phase 1 — Detailed Design

### Config Schema

```python
class InterruptionMode(str, Enum):
    ENABLED = "enabled"                    # Mode 1: default, user can interrupt
    DISABLED_DISCARD = "disabled_discard"  # Mode 3: no interruption, discard speech

class InterruptionConfig(BaseModel):
    mode: InterruptionMode = InterruptionMode.ENABLED
    min_words: Optional[int] = Field(
        None,
        description="Minimum words user must speak to trigger interruption (only applies when mode=enabled)",
        ge=1,
    )
```

Added to `ConfigurationModel`:
```python
interruption: Optional[InterruptionConfig] = Field(
    None,
    description="Interruption handling configuration",
)
```

### Pipeline Wiring

In `pipeline.py`, when building `UserTurnStrategies` and `LLMUserAggregatorParams`:

```python
interruption_config = template.configurations.interruption or InterruptionConfig()

# Build turn start strategies
if interruption_config.min_words and interruption_config.mode == InterruptionMode.ENABLED:
    user_turn_start_strategies = [MinWordsUserTurnStartStrategy(min_words=interruption_config.min_words, use_interim=True)]
    if vad_analyzer:
        # Keep VAD for turn detection but MinWords controls interruption threshold
        user_turn_start_strategies.insert(0, VADUserTurnStartStrategy())
else:
    # Current behavior
    user_turn_start_strategies = [TranscriptionUserTurnStartStrategy(use_interim=True)]
    if vad_analyzer:
        user_turn_start_strategies.insert(0, VADUserTurnStartStrategy())

# Build mute strategies
user_mute_strategies = []
if interruption_config.mode == InterruptionMode.DISABLED_DISCARD:
    user_mute_strategies.append(AlwaysUserMuteStrategy())

# Pass to aggregator
context_aggregator = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(start=user_turn_start_strategies),
        user_mute_strategies=user_mute_strategies,
        vad_analyzer=vad_analyzer,
    ),
)
```

### Template Example

```json
{
  "configurations": {
    "interruption": {
      "mode": "enabled",
      "min_words": 3
    }
  }
}
```

```json
{
  "configurations": {
    "interruption": {
      "mode": "disabled_discard"
    }
  }
}
```

---

## Phase 2 — Detailed Design

### Node-Level Config

`FlowNodeModel` gains an optional `interruption` field:
```python
class FlowNodeModel(BaseModel):
    # ... existing fields ...
    interruption: Optional[InterruptionConfig] = Field(
        None,
        description="Node-specific interruption configuration (overrides template interruption)",
    )
```

### Template Example (Node-Level)

```json
{
  "configurations": {
    "interruption": { "mode": "enabled", "min_words": 3 }
  },
  "flow": {
    "initial_node": "greeting",
    "nodes": [
      {
        "node_name": "greeting",
        "task_messages": [...]
      },
      {
        "node_name": "legal_disclaimer",
        "interruption": { "mode": "disabled_discard" },
        "task_messages": [...]
      },
      {
        "node_name": "conversation",
        "interruption": { "mode": "enabled", "min_words": 1 },
        "task_messages": [...]
      }
    ]
  }
}
```

- `greeting` — inherits template default (`enabled`, min_words=3)
- `legal_disclaimer` — node override disables interruption, discards speech
- `conversation` — node override re-enables with min_words=1

### Dynamic Switching Module (`template/interruption.py`)

**Reset → Apply pattern** (mirrors `template/vad.py`):

```
transition_handler()
  ├─ reset_vad_to_default(context)
  ├─ apply_node_vad_config(context, node_name)
  ├─ await reset_interruption_to_default(context)   ← NEW
  └─ await apply_node_interruption_config(context, node_name)  ← NEW
```

**`reset_interruption_to_default(context)`**:
1. Reads `bot.default_interruption_config` (stored at pipeline creation)
2. Calls `_apply_interruption_config()` with template defaults

**`apply_node_interruption_config(context, node_name)`**:
1. Looks up `nodes[node_name]["interruption"]` from `flow_config`
2. If present, calls `_apply_interruption_config()` with node override

**`_apply_interruption_config(user_aggregator, config, ...)`**:
1. Rebuilds `UserTurnStrategies` (start + stop) from config
2. Calls `user_aggregator._user_turn_controller.update_strategies(new_strategies)`
   - PipeCat handles cleanup → replace → setup lifecycle internally
3. Cleans up existing mute strategies, clears the list
4. If `disabled_discard` → creates new `AlwaysUserMuteStrategy`, calls `setup()`, appends
5. If switching to unmuted → explicitly sets `_user_is_muted = False`

### Lifecycle Considerations

- **Async**: Strategy switching is async (setup/cleanup are coroutines), so transition_handler `await`s
- **Race safety**: `_user_is_muted` is explicitly cleared when removing mute strategies to prevent stale mute state
- **No-op on same config**: When node has no override, reset to default is the only operation (effectively a no-op if already at default)
- **VAD independence**: VAD reset/apply is synchronous; interruption reset/apply is async — they don't interfere
