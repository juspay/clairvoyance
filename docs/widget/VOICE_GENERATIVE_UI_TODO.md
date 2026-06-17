# TODO — Re-enable generative voice UI (agent-mode Daily) — DEFERRED

**Status:** deferred out of the widget voice-as-chat PR (`feat/widget-voice-as-chat-backend`).
The `VoiceUiStreamProcessor` module is **kept** but **not plugged into the pipeline**.

## What this feature is

Generative UI over the **voice** RTVI channel for **non-widget, Daily *agent-mode*** calls
(`ExecutionMode.DAILY` / `DAILY_TEST`, i.e. a full FlowManager + LLM pipeline). The LLM emits
`<ui_stream>` ops inline; a `VoiceUiStreamProcessor` placed between the LLM and TTS strips the
markers from the spoken prose and re-emits each op as an RTVI `ui-op` event so the client renders
carousels/cards. Opt-in per template via `configurations.ui_catalog` + the
`{{ui_primitives_section}}` prompt placeholder.

## Why it was deferred

- **Widget voice no longer uses it.** Widget voice is now stream mode (`DAILY_STREAM`) driven by
  the chat `ChatAgent`, which emits and persists `ui_op` itself via the `WidgetVoiceBridge`
  (`chat/voice_bridge.py`). The processor was gated off for stream mode anyway
  (`_resolve_voice_ui_allowlist` returned `None` when `is_stream`).
- The only remaining consumer would be a **non-widget** Daily agent-mode call with a `ui_catalog`
  template — a path not currently exercised. Keeping the half-wired feature in this PR added
  surface area (an extra processor in the agent pipeline, RTVI observer `ignored_sources`
  juggling) for no active caller. Deferring keeps the voice-as-chat PR focused.

## What was REMOVED (the wiring) — and where

- `agent/flow.py` — `build_flow_config` lost its `ui_allowlist` param; it now calls
  `flow_builder.build_flow_config(template)` (the `{{ui_primitives_section}}` placeholder stays
  inert).
- `agent/pipeline.py` — `build_pipeline` lost `ui_emit` / `ui_allowlist` params and the
  `VoiceUiStreamProcessor` splice (the agent tail is now plain `[llm, tts, output, assistant]`);
  `create_pipeline_task` lost `ignored_rtvi_sources`; the `VoiceUiStreamProcessor` / `RtviEmit`
  imports are gone.
- `agent/__init__.py` — removed `self._voice_ui_allowlist`, the `_resolve_voice_ui_allowlist`
  method, the `resolve_allowlist` import, and the `ui_emit` / `ui_allowlist` /
  `ignored_rtvi_sources` arguments at the `build_flow_config` + `build_pipeline` +
  `create_pipeline_task` call sites.

## What was KEPT (do NOT delete — still used or intentionally dormant)

- `processors/voice_ui_stream.py` — the `VoiceUiStreamProcessor` class (+ `RtviEmit`) and the
  `coerce_ui_action_text` helper. The module is exported from `processors/__init__.py`.
- `coerce_ui_action_text` is **still in use** for **click-to-talk** (the `ui-action` inbound
  injection), which the widget voice-as-chat bridge relies on — unrelated to the deferred
  generative-UI *output*.
- `FlowConfigBuilder.build_flow_config(..., ui_allowlist=...)` (in `template/builder.py`) keeps its
  keyword-only `ui_allowlist` param — **chat** still uses it; only the voice caller stopped passing
  it.
- The `configurations.ui_catalog` template field and `template/ui_catalog.resolve_allowlist`.

## How to RE-ENABLE later (re-wiring checklist)

1. `agent/__init__.py`: restore `self._voice_ui_allowlist: Optional[Set[str]] = None`, the
   `_resolve_voice_ui_allowlist(is_stream, is_realtime)` method (gates on
   `not is_stream and not is_realtime and is_daily_mode and configurations.ui_catalog`), and
   re-import `resolve_allowlist`. Recompute `is_realtime = stt is None and tts is None and llm is not None`
   and set `self._voice_ui_allowlist` before building the pipeline.
2. Pass `ui_allowlist=self._voice_ui_allowlist` to `build_flow_config(...)`.
3. Pass `ui_emit=self._emit_rtvi_event, ui_allowlist=self._voice_ui_allowlist` to `build_pipeline(...)`,
   and `ignored_rtvi_sources=[llm] if (self._voice_ui_allowlist is not None and llm) else None` to
   `create_pipeline_task(...)`.
4. `agent/pipeline.py`: re-add the `ui_emit` / `ui_allowlist` params + the
   `VoiceUiStreamProcessor` splice in the agent (`else`) branch, the `ignored_rtvi_sources` param on
   `create_pipeline_task`, and the two imports (`VoiceUiStreamProcessor`, `RtviEmit`). Restore `Set`
   to the typing import.
5. `agent/flow.py`: re-add the `ui_allowlist` param and pass it to
   `flow_builder.build_flow_config(template, ui_allowlist=ui_allowlist)`. Restore `Set` to the
   typing import.
6. Tests: `tests/test_voice_ui_stream.py` covers the processor in isolation and stays green; add a
   wiring/integration test for the agent-mode splice.

Reference for the original design: the generative-UI-over-RTVI work (commit `344863e`) and
`docs/widget/VOICE_AS_CHAT.md` (§A1).
