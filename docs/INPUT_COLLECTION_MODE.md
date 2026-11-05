# Input Collection Mode — Research, Findings & Implementation Plan

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Current Architecture Deep Dive](#2-current-architecture-deep-dive)
   - [Pipeline Architecture](#21-pipeline-architecture)
   - [STT & Endpoint Detection](#22-stt--endpoint-detection)
   - [Turn-Taking System](#23-turn-taking-system)
   - [Interruption Control](#24-interruption-control)
   - [Node & Template Architecture](#25-node--template-architecture)
   - [PipeCat Frame Processor System](#26-pipecat-frame-processor-system)
   - [Direct TTS Without LLM](#27-direct-tts-without-llm)
3. [Soniox Endpoint Detection — Full Chain Analysis](#3-soniox-endpoint-detection--full-chain-analysis)
4. [Key Discovery: Fallback Mode Accumulation](#4-key-discovery-fallback-mode-accumulation)
5. [Implementation Plan (Phased)](#5-implementation-plan-phased)
   - [Phase 1: Configurable Turn Accumulation](#phase-1-configurable-turn-accumulation-foundation)
   - [Phase 2: Back-Channeling During Accumulation](#phase-2-back-channeling-during-accumulation)
   - [Phase 3: LLM-Driven Collection Intelligence](#phase-3-llm-driven-collection-intelligence)
6. [Edge Cases & Solutions](#6-edge-cases--solutions)
7. [File Reference](#7-file-reference)

---

## 1. Problem Statement

### Scenario
Nodes like "Update Number" require users to dictate sequences of values (phone numbers, account numbers, addresses). Users naturally speak in segments with pauses:

```
User: "9 8 7"  [pause]  "6 5 4"  [pause]  "3 2 1 0"
```

### Current Behavior (Broken)
1. User says "9 8 7" → Soniox detects semantic endpoint → finalizes transcript
2. `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)` fires immediately
3. LLM receives "9 8 7" and responds: "I have the first 3 digits. What are the remaining ones?"
4. While LLM/TTS is speaking, user says "6 5 4" → treated as interruption
5. Cycle repeats — user never gets to complete the full number

### Desired Behavior
- Segments spoken with natural pauses merge into a single input
- Agent gives soft back-channel acknowledgments ("okay", "got it") without interrupting
- LLM processes the full accumulated data once the user is done
- User can ask conversational questions mid-collection ("are you listening?") and get intelligent responses

### Design Philosophy
- **LLM-driven intelligence** — the LLM handles classification, completion detection, and conversational responses. No regex/if-else classification in custom code.
- **Minimal framework code** — configure the pipeline environment so the LLM can work effectively.
- **Node-level configuration** — collection mode is per-node, not template-wide.
- **No VAD dependency** — VAD is disabled in production. Solution must work purely with transcription-based turn detection.

---

## 2. Current Architecture Deep Dive

### 2.1 Pipeline Architecture

```
transport.input() → stt → TranscriptionGateProcessor → [user_idle] → user_aggregator → llm → tts → transport.output() → assistant_aggregator
```

**Key components:**
- **transport.input()**: Raw audio from telephony (8kHz) or web (16kHz)
- **stt**: Speech-to-text service (Soniox, Deepgram, OpenAI, Google, Sarvam)
- **TranscriptionGateProcessor**: Filters/mutes transcriptions (hard mute + keyword filter)
- **user_aggregator** (`LLMUserAggregator`): Accumulates user text, manages turn detection, triggers LLM
- **llm**: Azure OpenAI LLM service
- **tts**: Text-to-speech (Google Cloud, Cartesia, ElevenLabs)
- **assistant_aggregator**: Tracks bot responses, manages context

**Pipeline construction**: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`

### 2.2 STT & Endpoint Detection

#### Soniox (Primary Production STT)

**Two modes controlled by `vad_force_turn_endpoint`:**

| Setting | Mode | Behavior |
|---------|------|----------|
| `vad_force_turn_endpoint=True` | VAD-forced | External VAD sends finalize message to Soniox; Soniox returns tokens immediately |
| `vad_force_turn_endpoint=False` | Native semantic | Soniox detects speech boundaries autonomously; `max_endpoint_delay_ms` controls delay |

**Native semantic mode (production default when VAD disabled):**
- Soniox analyzes audio stream semantically to detect natural speech boundaries
- After detecting end-of-speech, waits up to `max_endpoint_delay_ms` (500-3000ms, default 500ms in our config) before sending `<end>` token
- Produces a finalized `TranscriptionFrame` with accumulated text
- Trade-off: longer delay = better accuracy but higher latency

**Configuration:**
- `BREEZE_BUDDY_SONIOX_MAX_ENDPOINT_DELAY_MS`: default 500ms
- `BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT`: default false
- `BREEZE_BUDDY_SONIOX_ENABLE_NON_FINAL_TOKENS`: streaming interim tokens
- `BREEZE_BUDDY_SONIOX_CONTEXT`: Business-specific vocabulary context

**Custom service**: `app/ai/voice/stt/soniox/service.py` — extends PipeCat's `SonioxSTTService` to inject `max_endpoint_delay_ms` into WebSocket config. This is set at connection time and **cannot be changed at runtime** without reconnecting.

**Token flow:**
```
Audio → Soniox WebSocket
  ├─ Non-final tokens → InterimTranscriptionFrame (streaming)
  ├─ Final tokens → accumulated in buffer
  └─ <end> token → TranscriptionFrame(finalized=True) with full text
```

#### Deepgram (Alternative)
- `endpointing`: Enable built-in endpoint detection
- `utterance_end_ms`: Silence threshold (default 1000ms)
- `no_delay`: Minimize processing latency
- Also not changeable at runtime without reconnection

#### Runtime Changeability Summary

| Parameter | Runtime Change? | Mechanism |
|-----------|----------------|-----------|
| Soniox `max_endpoint_delay_ms` | **No** | Baked into WebSocket config at connect time |
| Deepgram `endpointing`/`utterance_end_ms` | **No** | Set at LiveOptions creation |
| VAD params (`stop_secs`, `confidence`) | **Yes** | `vad_analyzer.set_params()` |
| Interruption strategies | **Yes** | `update_strategies()` |
| `user_speech_timeout` | **Yes** | Rebuild strategy at node transition |
| Direct TTS (TTSSpeakFrame) | **Yes** | Push frame anytime |

### 2.3 Turn-Taking System

#### Turn Start Strategies

Detect when the user begins speaking:

1. **`VADUserTurnStartStrategy`** — fires on `VADUserStartedSpeakingFrame` (~100ms). Only when VAD enabled.
2. **`TranscriptionUserTurnStartStrategy`** — fires on `InterimTranscriptionFrame` (with `use_interim=True`). Primary strategy when VAD disabled.
3. **`MinWordsUserTurnStartStrategy`** — requires N words to trigger interruption while bot speaks, 1 word when bot silent. Replaces TranscriptionUserTurnStartStrategy when configured.

**Production (no VAD):** Only TranscriptionUserTurnStartStrategy (or MinWords if configured).

#### Turn Stop Strategies

Detect when the user finishes speaking:

**`SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)`** — the only stop strategy used.

**Two operating modes:**

| Mode | Condition | Behavior |
|------|-----------|----------|
| **VAD mode** | `_vad_stopped_time` is set | Timer starts at VAD stop. Finalized transcript checks if timeout elapsed. |
| **Fallback mode (no VAD)** | `_vad_stopped_time` is None | Timer **RESETS on every new transcript** (interim or finalized). Fires after timeout with no new transcripts. |

**Current config:** `user_speech_timeout=0.0` — fires immediately on finalized transcript. This is **hardcoded** in both `pipeline.py` (line 245) and `interruption.py` (line 159).

#### User Mute Strategies

Control what happens to user input while bot speaks:

1. **`AlwaysUserMuteStrategy`** — drops all user frames while bot speaks (used in `disabled_discard` mode)
2. **`FirstSpeechUserMuteStrategy`** — mutes only during bot's first response
3. **`FunctionCallUserMuteStrategy`** — mutes during function execution

#### LLM User Aggregator (`LLMUserAggregator`)

Orchestrates turn detection:
1. Receives frames from pipeline
2. Checks mute state via mute strategies
3. Passes frames to `UserTurnController` (which runs start/stop strategies)
4. On turn start: pushes `InterruptionFrame` to cancel bot output
5. Accumulates `TranscriptionFrame` text in `_aggregation` list
6. On turn stop: concatenates text, adds `{"role": "user", "content": text}` to context, pushes `LLMContextFrame` to trigger LLM

**Key method:** `push_aggregation()` — the point where accumulated text becomes an LLM message.

#### Runtime Strategy Switching

Already supported via `UserTurnController.update_strategies()`:
```python
async def update_strategies(self, strategies: UserTurnStrategies):
    await self._cleanup_strategies()
    self._user_turn_strategies = strategies
    await self._setup_strategies()
```

Used by `interruption.py` during node transitions.

### 2.4 Interruption Control

#### Two-Phase Implementation (Complete)

**Phase 1 — Template-level** (commit 5302640):
```python
class InterruptionMode(str, Enum):
    ENABLED = "enabled"              # User can interrupt
    DISABLED_DISCARD = "disabled_discard"  # User speech dropped while bot speaks

class InterruptionConfig(BaseModel):
    mode: InterruptionMode = InterruptionMode.ENABLED
    min_words: Optional[int] = None
```

**Phase 2 — Node-level** (commit eeea9dc):
- `FlowNodeModel.interruption` field for per-node overrides
- Dynamic strategy switching via reset-then-apply pattern
- Functions: `reset_interruption_to_default()`, `apply_node_interruption_config()`

**Phase 3 — Buffered speech** (designed, not implemented):
- Mode `DISABLED_BUFFER`: capture user speech during bot turn, replay after

#### Reset-Then-Apply Pattern

On every node transition:
```python
# In transition_handler():
await reset_interruption_to_default(context)       # Reset to template defaults
await apply_node_interruption_config(context, node)  # Apply node overrides
```

This pattern is used identically for VAD config and will be used for input collection config.

### 2.5 Node & Template Architecture

#### Template Structure
```
Template
├── configurations (ConfigurationModel)
│   ├── TTS, STT, VAD, Interruption settings
│   ├── Keyword filter, User idle, Background sound
│   └── Greetings, Transfer number, etc.
├── flow
│   ├── initial_node (string)
│   ├── nodes (array of FlowNodeModel)
│   ├── global_functions (array)
│   └── end_conversation_callbacks
├── expected_payload_schema
└── secrets
```

#### Node Structure (`FlowNodeModel`)
```python
class FlowNodeModel(BaseModel):
    node_name: str
    task_messages: List[TaskMessage]       # LLM system/assistant prompts
    role_messages: List[TaskMessage]       # Role context
    functions: List[FlowFunction]          # LLM-callable functions
    pre_actions: List[FlowAction]          # Before node starts
    post_actions: List[FlowAction]         # After transition to node
    vad_config: Optional[VadConfig]        # Node-level VAD override
    interruption: Optional[InterruptionConfig]  # Node-level interruption override
```

#### Flow & State Machine
- Nodes are states; functions trigger transitions
- `transition_handler()` executes: hooks → record exit → reset configs → apply configs → create node → record entry
- LLM calls functions defined in node's `functions` array
- Functions can have `transition_to` target node and `hooks` for side effects

#### Configuration Override Pattern
- Template-level config stored as `bot.default_*` on bot instance
- Node-level config stored in `bot.flow_config["nodes"][node_name]`
- On transition: always reset to defaults, then apply node overrides
- Only non-None values override defaults

#### Extensibility Points
1. **Pre/Post Actions**: Handler functions in `handlers/internal/`
2. **Global Functions**: HTTP, builtin, or custom adapters via `GlobalFunctionRegistry`
3. **Hooks**: Fire-and-forget async operations (DB updates, HTTP calls)
4. **Processors**: Custom `FrameProcessor` subclasses inserted in pipeline
5. **Node config overrides**: Per-node behavior via reset-then-apply pattern

### 2.6 PipeCat Frame Processor System

#### Base Class: `FrameProcessor`

**Key overridable methods:**
- `process_frame(frame, direction)` — called for every frame. Override to intercept/modify/suppress.
- `push_frame(frame, direction)` — pushes frame to next processor (DOWNSTREAM) or previous (UPSTREAM)
- `queue_frame(frame, direction, callback)` — queues frame for processing

**Frame types:**

| Category | Priority | Cancelled by interruptions? | Examples |
|----------|----------|---------------------------|----------|
| SystemFrame | Highest | No | StartFrame, InterruptionFrame, UserStarted/StoppedSpeaking, BotStarted/StoppedSpeaking |
| DataFrame | Normal | Yes | TranscriptionFrame, InterimTranscriptionFrame, TTSSpeakFrame, LLMMessagesAppendFrame |
| ControlFrame | Normal | Yes | TTSStarted/StoppedFrame, LLMFullResponseStart/EndFrame |

**Special: UninterruptibleFrame** mixin — preserved during interruptions (FunctionCallResultFrame, EndFrame).

#### Common Patterns

**Selective suppression:**
```python
async def process_frame(self, frame, direction):
    await super().process_frame(frame, direction)
    if should_suppress(frame):
        return  # Frame dropped
    await self.push_frame(frame, direction)
```

**State tracking:**
```python
if isinstance(frame, BotStartedSpeakingFrame):
    self._bot_speaking = True
elif isinstance(frame, BotStoppedSpeakingFrame):
    self._bot_speaking = False
```

**Frame injection:**
```python
await self.push_frame(TTSSpeakFrame(text="okay"), FrameDirection.DOWNSTREAM)
```

### 2.7 Direct TTS Without LLM

Multiple ways to make the bot speak without triggering an LLM call:

| Method | LLM Triggered | Context Pollution | Use Case |
|--------|--------------|-------------------|----------|
| `TTSSpeakFrame(text, append_to_context=False)` | No | No | Dynamic text via TTS |
| `pre_tts_message` in global functions | No | No | Handler notification text |
| `play_audio_sound` handler | No | No | Pre-recorded audio files |
| Initial greeting (pre-synthesized) | No | No | Call opening |
| Direct `transport.output().write_audio_frame()` | No | No | Raw audio streaming |
| `LLMMessagesAppendFrame(run_llm=False)` | No | Yes (adds to context) | Silent context update |

**`TTSSpeakFrame` is the recommended approach for back-channeling:**
```python
await task.queue_frame(TTSSpeakFrame(text="okay", append_to_context=False))
```

---

## 3. Soniox Endpoint Detection — Full Chain Analysis

### Complete Chain: Speech End → Bot Response

```
┌─────────────────────────────────────────────────────────────────────┐
│ TIMELINE: User stops speaking → Bot responds                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  t=0ms    User stops speaking                                       │
│     │                                                               │
│     ├─── [SONIOX PROCESSING WINDOW]                                 │
│     │    Soniox detects semantic speech boundary                    │
│     │    Waits up to max_endpoint_delay_ms (e.g., 500ms)           │
│     │    Sends <end> token                                          │
│     │                                                               │
│  t≈500ms  TranscriptionFrame(finalized=True) arrives at PipeCat    │
│     │                                                               │
│     ├─── [PIPECAT TURN DETECTION WINDOW]                            │
│     │    SpeechTimeoutUserTurnStopStrategy evaluates                │
│     │    Waits user_speech_timeout (currently 0.0s)                 │
│     │                                                               │
│  t≈500ms  Turn ends → LLM aggregator pushes context                │
│     │                                                               │
│     ├─── [LLM GENERATION]                                           │
│     │    LLM processes context, generates response                  │
│     │                                                               │
│  t≈1500ms Bot starts speaking (TTS)                                 │
│                                                                     │
│  TOTAL: max_endpoint_delay_ms + user_speech_timeout + LLM latency   │
│         These are SEQUENTIAL, not overlapping                       │
└─────────────────────────────────────────────────────────────────────┘
```

### How Soniox Endpoint Detection Interacts With Input Collection

**The critical insight:** Soniox's `max_endpoint_delay_ms` is the FIRST gate. It determines how quickly Soniox finalizes a transcript segment after the user pauses. PipeCat's `user_speech_timeout` is the SECOND gate — it determines how long to wait after receiving a finalized transcript before ending the user's turn.

**For collection mode, both gates matter:**

```
Without collection mode (current):
  "9 8 7" → [Soniox: 500ms] → finalized → [PipeCat: 0ms] → LLM responds immediately

With collection mode (Phase 1, user_speech_timeout=3.0s):
  "9 8 7" → [Soniox: 500ms] → finalized → [PipeCat: timer starts 3s]
  "6 5 4" → [Soniox: 500ms] → finalized → [PipeCat: timer RESETS 3s]  ← KEY!
  "3 2 1 0" → [Soniox: 500ms] → finalized → [PipeCat: timer RESETS 3s]
  [3s silence] → turn ends → LLM receives: "9 8 7 6 5 4 3 2 1 0"
```

**Soniox will still finalize each segment independently** (it detects the pause as a semantic boundary). We cannot change this without reconnecting. But that's fine — PipeCat's `user_speech_timeout` in fallback mode (no VAD) accumulates these segments naturally.

### Key Soniox Configuration Parameters

| Parameter | Default | Purpose | Runtime Changeable |
|-----------|---------|---------|-------------------|
| `max_endpoint_delay_ms` | 500ms (our config) | Max wait before sending `<end>` token | No (WebSocket config) |
| `vad_force_turn_endpoint` | false | Use external VAD vs native detection | No (connection time) |
| `enable_non_final_tokens` | true (BB) | Stream interim tokens | No (connection time) |
| `max_non_final_tokens_duration_ms` | 0 | Force finalization timeout | No (connection time) |
| `context` | Business vocabulary | Improve recognition accuracy | No (connection time) |
| `language_hints` | "en" | Language detection | No (connection time) |

### Impact on Collection Mode Design

Since Soniox config is immutable at runtime:
- We **cannot** increase `max_endpoint_delay_ms` for collection nodes to delay finalization
- We **must** handle accumulation above the STT layer (in PipeCat's turn management)
- The `user_speech_timeout` approach is the correct lever — it works above STT and is runtime-configurable

---

## 4. Key Discovery: Fallback Mode Accumulation

### The Discovery

Without VAD (production default), `SpeechTimeoutUserTurnStopStrategy` operates in **fallback mode** where the timer **resets on every new transcript**. This means increasing `user_speech_timeout` naturally accumulates multi-segment input.

### How Fallback Mode Works

```python
# In SpeechTimeoutUserTurnStopStrategy._handle_transcription():

# Fallback path (no VAD): _vad_stopped_time is None
if not self._vad_user_speaking and self._vad_stopped_time is None:
    # Cancel existing timeout and start fresh
    if self._timeout_task:
        self._timeout_task.cancel()
    timeout = self._calculate_timeout()
    self._timeout_task = self.create_task(self._timeout_handler(timeout))
```

**Each new transcript (interim or finalized) cancels the existing timer and starts a new one.** The turn only ends when no new transcripts arrive for `user_speech_timeout` seconds.

### Proof of Concept

```
user_speech_timeout = 3.0, no VAD:

t=0.0s   User says "9 8 7"
t=0.5s   Soniox finalizes → TranscriptionFrame("9 8 7") → timer starts (3s)
t=1.5s   User says "6 5 4"
t=1.6s   Soniox interim → InterimTranscriptionFrame → timer RESETS (3s)
t=2.0s   Soniox finalizes → TranscriptionFrame("6 5 4") → timer RESETS (3s)
t=3.5s   User says "3 2 1 0"
t=3.6s   Soniox interim → timer RESETS (3s)
t=4.0s   Soniox finalizes → TranscriptionFrame("3 2 1 0") → timer RESETS (3s)
t=7.0s   [3s of silence] → timer fires → turn ends

LLM receives aggregated: "9 8 7 6 5 4 3 2 1 0" (single user message)
```

### Why This Is Elegant

- **Zero custom buffering code** — PipeCat's aggregator already accumulates text across segments within a turn
- **Works with existing pipeline** — just changing a single parameter value
- **Runtime configurable** — strategy can be rebuilt on node transitions (already supported)
- **Follows existing patterns** — same reset-then-apply as VAD/interruption

---

## 5. Implementation Plan (Phased)

### Phase 1: Configurable Turn Accumulation (Foundation)

**Goal:** Segments spoken with pauses merge into a single LLM turn for collection nodes.

#### Changes Required

##### 1. `types.py` — New config model

Add `InputCollectionConfig` and field to `FlowNodeModel`:

```python
class InputCollectionConfig(BaseModel):
    """Configuration for multi-segment input collection nodes."""
    enabled: bool = False
    user_speech_timeout: float = 0.0  # seconds to wait after last segment before ending turn

class FlowNodeModel(BaseModel):
    # ... existing fields ...
    input_collection: Optional[InputCollectionConfig] = None  # Node-level collection config
```

##### 2. `template/input_collection.py` — New module (reset-then-apply)

```python
async def reset_input_collection_to_default(context: TemplateContext):
    """Reset turn stop strategy to default user_speech_timeout=0.0"""
    # Rebuild strategies with default timeout

async def apply_node_input_collection_config(context: TemplateContext, node_name: str):
    """Apply node-specific input collection config (higher user_speech_timeout)"""
    # Read node config, rebuild strategies with configured timeout
```

##### 3. `interruption.py` — Parameterize `user_speech_timeout`

Change from hardcoded `0.0` to accept configurable value:

```python
# Currently (line 159):
SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)

# Change to:
SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=user_speech_timeout)
```

##### 4. `pipeline.py` — Same parameterization for initial pipeline creation

##### 5. `transition.py` — Add collection config to transition flow

```python
async def transition_handler(context, args, transition_to, hooks, function_name):
    # ... existing code ...
    reset_vad_to_default(context)
    apply_node_vad_config(context, transition_to)
    await reset_interruption_to_default(context)
    await apply_node_interruption_config(context, transition_to)
    await reset_input_collection_to_default(context)           # NEW
    await apply_node_input_collection_config(context, transition_to)  # NEW
    # ... existing code ...
```

##### 6. `builder.py` — Attach config to node dict

```python
if node.input_collection:
    cast(Dict[str, Any], node_config)["input_collection"] = node.input_collection
```

##### 7. Template JSON — Node configuration

```json
{
  "node_name": "update_number",
  "task_messages": [
    {
      "role": "system",
      "content": "You are collecting a phone number from the user. The user will provide digits in segments with natural pauses. All segments will be accumulated and delivered to you as a single message. Process the complete number when you receive it. If the number is incomplete or invalid, ask the user to provide the missing digits."
    }
  ],
  "input_collection": {
    "enabled": true,
    "user_speech_timeout": 3.0
  },
  "functions": [
    {
      "function_name": "confirm_number",
      "description": "User confirmed the updated number",
      "properties": {
        "phone_number": { "type": "string" }
      },
      "transition_to": "confirmation_node"
    }
  ]
}
```

#### What Phase 1 Solves
- No more premature "I got 2 digits" responses
- Multiple segments merge naturally via PipeCat's accumulation
- LLM receives complete data and handles it intelligently via prompts
- Conversational questions (after 3s+ silence) flow to LLM normally
- Follows existing architecture patterns exactly

#### Estimated Scope
- ~100-150 lines of new code
- 5-6 files modified
- Zero PipeCat modifications needed

---

### Phase 2: Back-Channeling During Accumulation

**Goal:** During the `user_speech_timeout` wait window, give soft audio acknowledgments so the user knows the agent is listening.

#### The Challenge
During accumulation, the turn hasn't ended, so the LLM hasn't been called. Back-channels must come from the framework.

#### Approach: `BackChannelProcessor`

A lightweight `FrameProcessor` inserted in the pipeline that:
1. Detects when collection mode is active and a finalized transcript arrives
2. Starts a short timer (`back_channel_delay`, e.g., 1.5s — less than `user_speech_timeout`)
3. If timer fires (user paused but not done), pushes `TTSSpeakFrame("okay", append_to_context=False)`
4. If user resumes speaking (new interim transcript), cancels the timer
5. Limits frequency via `min_interval_secs` to avoid rapid-fire acknowledgments

#### Pipeline Position

```
stt → TranscriptionGate → [BackChannelProcessor] → [InputCollectionProcessor if needed] → user_aggregator
```

#### Config Extension

```json
{
  "input_collection": {
    "enabled": true,
    "user_speech_timeout": 3.0,
    "back_channel": {
      "enabled": true,
      "delay_secs": 1.5,
      "min_interval_secs": 3.0,
      "messages": ["okay", "got it", "go on"]
    }
  }
}
```

#### Key Design Decisions
- Same TTS voice as agent (no pre-recorded audio in initial version)
- `append_to_context=False` — back-channels don't pollute LLM context
- Back-channel audio may overlap with user's next segment — this is natural and expected (like human back-channeling)
- Timer is always less than `user_speech_timeout` to fire during accumulation, not after

#### Estimated Scope
- ~80-100 lines for BackChannelProcessor
- Config model additions (~10 lines)
- Pipeline wiring (~5 lines)

---

### Phase 3: LLM-Driven Collection Intelligence

**Goal:** Let the LLM decide whether collection is complete or needs more rounds, and handle edge cases intelligently.

#### Approach: Collection-Aware LLM Function

Add a function to the node that the LLM can call to signal collection state:

```json
{
  "function_name": "collection_status",
  "description": "Signal the status of data collection. Call this after evaluating the user's input.",
  "properties": {
    "status": {
      "type": "string",
      "enum": ["complete", "need_more"],
      "description": "Whether enough data has been collected"
    },
    "collected_data": {
      "type": "string",
      "description": "The full data collected so far"
    },
    "message_to_user": {
      "type": "string",
      "description": "Message to speak to the user"
    }
  },
  "required": ["status", "collected_data"]
}
```

#### Framework Handler

```python
async def handle_collection_status(context, args, **kwargs):
    status = args.get("status")
    if status == "need_more":
        # Re-enable collection mode for next round
        # The LLM's message_to_user will be spoken via normal TTS
        # After bot finishes speaking, user_speech_timeout kicks in again
        pass
    elif status == "complete":
        # Proceed to next step (transition, confirmation, etc.)
        pass
    return {"acknowledged": True}
```

#### How It Works

1. User provides segments → accumulated via Phase 1 → LLM receives full input
2. LLM evaluates: "9 8 7 6 5" (only 5 digits for a 10-digit number)
3. LLM calls `collection_status(status="need_more", collected_data="98765", message_to_user="I have 9 8 7 6 5. Please continue with the remaining digits.")`
4. Framework speaks message, re-enables collection mode
5. User provides more: "4 3 2 1 0"
6. LLM evaluates: "4 3 2 1 0" (combined with context, now has full 10 digits)
7. LLM calls `collection_status(status="complete", collected_data="9876543210")`
8. Framework proceeds to next node

#### Edge Cases Handled Naturally
- "Are you listening?" → LLM responds conversationally, then calls `need_more`
- "Wait, the third digit is wrong" → LLM understands correction, updates collected data
- "That's all" with incomplete data → LLM asks about missing data
- "Actually, cancel that" → LLM handles via normal conversation flow

#### Estimated Scope
- ~60-80 lines for collection_status handler
- Integration with existing function call system
- Template examples and documentation

---

## 6. Edge Cases & Solutions

### Edge Case 1: User says half a phone number, then asks "are you listening?"

**Phase 1 solution:** If the question comes after 3s+ of silence, the turn ends naturally and LLM receives "9 8 7 6 5 are you listening". The LLM (via good prompting) recognizes the conversational question and responds: "Yes, I have 9 8 7 6 5. Please continue with the remaining digits."

**Phase 3 enhancement:** LLM calls `collection_status(status="need_more")` to explicitly re-enable collection.

### Edge Case 2: User says numbers in batches with natural pauses

**Phase 1 solution:** `user_speech_timeout=3.0` accumulates all batches within 3s of each other into one turn. LLM receives complete data.

**Phase 2 enhancement:** Back-channel "okay" between batches reassures user.

### Edge Case 3: User goes silent for a long time

**Phase 1 solution:** After `user_speech_timeout` expires, turn ends and LLM processes whatever was collected. User idle processor also kicks in if configured.

### Edge Case 4: Updating an address (multi-segment, variable length)

**Phase 1 solution:** Same config, different prompts:
```json
{
  "input_collection": { "enabled": true, "user_speech_timeout": 4.0 },
  "task_messages": [{ "role": "system", "content": "Collect the user's updated address..." }]
}
```

### Edge Case 5: User provides correction mid-collection

**Phase 1:** If said within 3s, it's part of the same turn: "9 8 7 wait no 9 8 6". LLM handles.

**Phase 3:** LLM can call `collection_status(status="need_more")` after clarifying.

### Edge Case 6: Background noise triggers false segments

**Existing:** `TranscriptionGateProcessor` keyword filter can suppress known noise words. `min_words` threshold in interruption config prevents single-word triggers.

---

## 7. File Reference

### Core Implementation Files

| File | Purpose |
|------|---------|
| `app/ai/voice/agents/breeze_buddy/template/types.py` | Config models (InputCollectionConfig, FlowNodeModel) |
| `app/ai/voice/agents/breeze_buddy/template/input_collection.py` | Reset-then-apply functions (NEW) |
| `app/ai/voice/agents/breeze_buddy/template/interruption.py` | Strategy rebuilding (modify: parameterize timeout) |
| `app/ai/voice/agents/breeze_buddy/template/transition.py` | Node transition orchestration (add collection calls) |
| `app/ai/voice/agents/breeze_buddy/template/builder.py` | Node config building (attach collection config) |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` | Pipeline creation (parameterize initial timeout) |
| `app/ai/voice/agents/breeze_buddy/agent/__init__.py` | Bot instance (store default collection config) |
| `app/ai/voice/agents/breeze_buddy/processors/back_channel.py` | BackChannelProcessor (NEW, Phase 2) |

### PipeCat Framework Files (Read-Only Reference)

| File | Purpose |
|------|---------|
| `.venv/.../pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py` | Turn stop strategy (fallback mode behavior) |
| `.venv/.../pipecat/turns/user_start/transcription_user_turn_start_strategy.py` | Turn start (no-VAD primary) |
| `.venv/.../pipecat/turns/user_start/min_words_user_turn_start_strategy.py` | Turn start (min words threshold) |
| `.venv/.../pipecat/processors/aggregators/llm_response_universal.py` | LLMUserAggregator (text accumulation) |
| `.venv/.../pipecat/processors/frame_processor.py` | FrameProcessor base class |
| `.venv/.../pipecat/frames/frames.py` | Frame type definitions |
| `.venv/.../pipecat/services/soniox/stt.py` | Soniox STT token handling |

### STT Configuration Files

| File | Purpose |
|------|---------|
| `app/ai/voice/stt/soniox/service.py` | Custom Soniox with max_endpoint_delay_ms |
| `app/ai/voice/stt/soniox/config.py` | Soniox config builder |
| `app/core/config/static.py` | Static env-based config (all STT providers) |
| `app/core/config/dynamic.py` | Redis-based dynamic config |

### Existing Pattern Reference Files

| File | Purpose |
|------|---------|
| `app/ai/voice/agents/breeze_buddy/template/vad.py` | VAD reset-then-apply pattern (template for input_collection.py) |
| `app/ai/voice/agents/breeze_buddy/template/interruption.py` | Interruption reset-then-apply pattern |
| `app/ai/voice/agents/breeze_buddy/processors/transcription_gate.py` | Existing processor example |
| `app/ai/voice/agents/breeze_buddy/processors/user_idle.py` | Timer/timeout patterns |
| `docs/INTERRUPTION_CONTROL.md` | Design doc for interruption system (template for this doc) |

### Example Templates

| File | Purpose |
|------|---------|
| `app/ai/voice/agents/breeze_buddy/examples/templates/interruption-config-example.json` | Interruption node config example |
| `app/ai/voice/agents/breeze_buddy/examples/templates/order-confirmation-with-vad.json` | Node-level VAD config example |
