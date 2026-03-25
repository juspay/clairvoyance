# Keyword Filter in Templates

## Overview

Templates can now configure a keyword filter that silently suppresses specific user transcriptions
**while the bot is actively speaking or the LLM is processing a response**.

This solves a common problem in telephony calls: a customer saying a background word like *"hello"*,
*"hmm"*, or *"haan"* while the bot is mid-sentence causes an unnecessary interruption — the bot
stops, the LLM re-processes, and the conversation feels broken. The keyword filter drops those
frames before they ever reach the interruption logic or the LLM context.

When the bot is **idle** (not speaking, not processing), transcriptions always pass through normally
— the filter only engages during bot activity.

---

## Configuration Structure

Add `keyword_filter` to the template's `configurations` object:

```json
{
  "configurations": {
    "keyword_filter": {
      "enabled": true,
      "keywords": ["hello", "hmm", "okay", "ok", "haan", "ha", "ji"],
      "match_type": "exact"
    }
  }
}
```

---

## Parameters

### `enabled`
- **Type:** `boolean`
- **Default:** `false`
- **Description:** Master switch. Set to `true` to activate the filter.

### `keywords`
- **Type:** `string[]`
- **Default:** `[]`
- **Description:** List of words or phrases to suppress. Matching is always case-insensitive and
  leading/trailing whitespace is ignored. An empty list disables filtering even when `enabled` is `true`.

### `match_type`
- **Type:** `"exact"` | `"includes"`
- **Default:** `"exact"`
- **Description:** Controls how the transcription is compared against each keyword.

  | Value | Behaviour | Example — keyword `"okay"` |
  |-------|-----------|---------------------------|
  | `exact` | The entire transcription must equal the keyword | `"okay"` → filtered · `"okay sure"` → **not** filtered |
  | `includes` | The transcription only needs to *contain* the keyword | `"okay"` → filtered · `"okay sure"` → filtered |

  Use `exact` (the default) to avoid accidentally dropping meaningful speech. Use `includes` when
  you want to catch phrases that embed the keyword, e.g. `"oh okay sure"`.

---

## How It Works

The `TranscriptionGateProcessor` sits in the pipeline **before** the user aggregator. In addition
to the keyword filter, it supports a **hard mute** mode (`mute()` / `unmute()` / `mute_for(duration)`)
that drops ALL transcription frames unconditionally — regardless of bot activity state or keyword
matching. Hard mute is typically engaged from a node pre-action when VAD is disabled. Both modes
can be active simultaneously; either condition causes a drop.

```
transport.input()
  → stt
  → TranscriptionGateProcessor    ← filters here
  → user_aggregator (VAD / turn strategies)
  → llm
  → tts
  → transport.output()
```

The processor independently tracks bot state by listening to Pipecat frames:

| Frame | Effect on filter |
|-------|-----------------|
| `LLMFullResponseStartFrame` | Bot becomes active — filter engages |
| `BotStartedSpeakingFrame` | Bot becomes active — filter engages |
| `LLMFullResponseEndFrame` | LLM finished processing — deactivates LLM-active flag |
| `BotStoppedSpeakingFrame` | Bot becomes idle — filter disengages |

A matching `TranscriptionFrame` or `InterimTranscriptionFrame` is silently dropped (`return` without
`push_frame`). Interim frames must also be filtered because `TranscriptionUserTurnStartStrategy`
with `use_interim=True` fires on interim frames, which would trigger an interruption even when the
final `TranscriptionFrame` would have been suppressed. No interruption is triggered, and the text
never reaches the LLM context.

---

## Example Use Cases

### Use Case 1: Suppress common Hindi/English acknowledgements (exact match)

```json
{
  "configurations": {
    "keyword_filter": {
      "enabled": true,
      "keywords": ["hello", "hmm", "okay", "ok", "haan", "ha", "ji", "uh", "yeah"],
      "match_type": "exact"
    }
  }
}
```

A customer murmuring *"haan"* or *"hmm"* while the bot reads out order details will not interrupt
the bot.

### Use Case 2: Suppress phrases containing a word (includes match)

```json
{
  "configurations": {
    "keyword_filter": {
      "enabled": true,
      "keywords": ["hello"],
      "match_type": "includes"
    }
  }
}
```

Any transcription containing the word *"hello"* — such as *"oh hello"* or *"hello hello"* — is
dropped while the bot is active.

### Use Case 3: Combined with VAD config

Keyword filter and VAD config are independent and can be used together:

```json
{
  "configurations": {
    "tts_voice_name": "rhea",
    "vad_config": {
      "confidence": 0.5,
      "stop_secs": 0.3
    },
    "keyword_filter": {
      "enabled": true,
      "keywords": ["hello", "hmm", "okay", "haan"],
      "match_type": "exact"
    }
  }
}
```

---

## Full Template Example

See [`examples/templates/order-confirmation-with-keyword-filter.json`](../app/ai/voice/agents/breeze_buddy/examples/templates/order-confirmation-with-keyword-filter.json)
for a complete working template that uses the keyword filter.

---

## Implementation Details

### Files

| File | Change |
|------|--------|
| `app/ai/voice/agents/breeze_buddy/template/types.py` | Added `KeywordMatchType` enum, `KeywordFilterConfig` model, and `keyword_filter` field on `ConfigurationModel` |
| `app/ai/voice/agents/breeze_buddy/processors/transcription_gate.py` | New `TranscriptionGateProcessor` (Pipecat `FrameProcessor`) |
| `app/ai/voice/agents/breeze_buddy/processors/__init__.py` | Exports `TranscriptionGateProcessor` |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` | Instantiates and inserts the processor into the pipeline |

### Backward Compatibility

- Field is optional on `ConfigurationModel` — existing templates are unaffected.
- `TranscriptionGateProcessor` is always inserted in the pipeline. When `keyword_filter` is absent
  or `enabled: false`, the processor acts as a transparent passthrough with negligible overhead —
  the pipeline behaves exactly as before.
