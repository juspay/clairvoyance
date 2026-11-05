# Widget Voice-as-Chat

> **Architecture v2 — stream mode + one brain (current, 2026-06-18).**
> Everything from "## 1. Goal" down is the **superseded v1 plan** (dual-brain:
> a voice FlowManager+LLM agent in `ExecutionMode.DAILY` synced to the chat
> `ChatAgent` via a resume seed + an end-of-call drain + `prepare_resume_node` +
> a `ui_turn_sink`). That design caused every voice/chat sync bug (cart_id lost
> on reconnect, carousels lost, "no cart in this session"). It has been
> **removed**. Read this v2 section as the source of truth; the rest is kept for
> historical context only.

## Architecture v2 — widget voice = stream mode through the chat brain

Widget voice now runs **`ExecutionMode.DAILY_STREAM`**: a Daily pipeline of
`transport.input → STT → TranscriptionGate → TranscriptCollector →
user_aggregator → TTS → transport.output` with **no LLM and no FlowManager**.
Every finished user turn is driven through the **existing chat `ChatAgent`** — the
same brain `POST /message` uses — so voice is pure audio I/O around one brain.
cart_id, `content_blocks` history, `agent_state` / client-context, carousels,
policy links are all inherited from chat because it *is* chat. No resume seed, no
drain, no block re-encoding, no sync layer.

**Backend (clairvoyance):**
- `chat/turn_core.py` — `run_chat_turn(session_id, user_content, …)`: the chat
  brain factored out of `send_chat_message_handler` (loads session/template/
  history+repair/agent_state/template_vars, supersedes pending approvals, drives
  `ChatAgent.run_turn`). Both `POST /message` and the voice bridge call it. The
  HTTP shell (RedisLock, SSE, cancel-bus, TurnMetrics, the 413 piggyback
  pre-validation) stays in `chat/handlers.py`.
- `chat/voice_bridge.py` — `WidgetVoiceBridge`: tapped from the pipeline's
  `on_user_turn_stopped` (full concatenated utterance) in stream mode for a
  widget session. Runs `run_chat_turn` in a tracked task and adapts the SSE
  events: `assistant_token` → sentence-aggregate → `TTSSpeakFrame` (speak) +
  `assistant-transcript` RTVI (render); `ui_op` → `ui-op` RTVI; the first tool
  call before any prose → a TTS filler; `turn_end` → `turn-end` RTVI.
  `on_user_turn_started` → `cancel_inflight()` (pipecat already flushed TTS
  natively); a generation counter drops a cancelled turn's tail frames; the next
  turn's `repair_dangling_tool_uses` heals any tool_use the barge-in left
  dangling — exactly like chat's `/cancel`.
- `agent/__init__.py` — wires the bridge when `is_stream_mode AND
  lead.metaData.widget_session_id`; speaks the persisted chat greeting once on
  the first attachment (no user turns yet); routes `ui-action` clicks through the
  bridge in stream mode.
- `widget/handlers.py` — `voice_connect` creates/reuses the lead with
  `DAILY_STREAM` and carries only `{is_widget, widget_config_id,
  widget_session_id}` + the rendered `payload` (the brain owns history/state/
  node). `voice_end` flips `current_channel` back to CHAT (crash safety net);
  `end_conversation` flips it authoritatively on pipeline teardown — both via the
  conditional `flip_chat_session_to_chat` UPDATE, so the second is a no-op.

**Frontend (loom `client-sdk`):** `session.ts routeServerMessage` maps the
bridge's `assistant-transcript` (build/extend the assistant bubble), `turn-end`
(finalize it), and `error` server messages — reusing the same `currentAssistantEntry`
→ `transcript` → store pipeline the old `onBotLlm*` callbacks fed in agent mode.
The user transcript still comes from STT via pipecat's `onUserTranscript`; `ui-op`
is unchanged.

**Removed by v2:** `prepare_resume_node` + `_render_resume_state_note` (flow.py),
the `_widget_resume_seed` plumbing (agent), `drain_voice_into_chat_session` +
query, `attach_voice_ui_blocks` + the `ui_turn_sink`/`_voice_ui_turns`
persistence (the chat brain now persists voice carousels as
`chat_message.ui_blocks` itself). The live `VoiceUiStreamProcessor` ui-op emit is
kept for any **non-widget** daily agent-mode template with `ui_catalog`.

**Deferred:** warm per-session `ChatAgent` (latency); re-adding specific
voice-only functions stripped by `CHAT_DISABLED_NAMES` if web voice needs them.

---

# Widget Voice-as-Chat — Implementation Plan (v1, SUPERSEDED)

> **Status:** Superseded by Architecture v2 above. Kept for historical context.
> Spans **clairvoyance** (backend), **loom `client-sdk`**, and
> **loom `breeze-buddy-assist-widget`**.

## 1. Goal (locked with the user)

When voice is toggled in the widget, **keep the chat UI** instead of swapping to a
full-screen voice pane:

- User speech and assistant speech render as **message bubbles** in the same transcript.
- **Carousels / product UI render with the SAME chat SpecStream primitives**
  (`BbUiPane → UiRenderer → Carousel/Tile/Button`) — no new UI components — delivered
  over the voice channel and able to be **generated mid-call**.
- The transcript (incl. carousels) stays **fully visible and scrollable**.
- **No text input box** during voice. The footer becomes a voice-control bar:
  **Mic mute/unmute · Speaker off (mute bot audio) · End**, plus a Listening/Speaking status.
- Clicking a product **feeds in as input to the LIVE voice agent** and shows as a user bubble.

Decisions: **Full scope** (loom + clairvoyance). Reuse chat UI primitives. Defer nothing
that the requirement needs.

## 2. Why this needs backend work (the architecture)

During a live Daily voice call the session is `current_channel=VOICE`, so the chat
`/message` HTTP path 409s — the click/input path **cannot** reuse it. The widget voice
attachment runs **`ExecutionMode.DAILY`** (agent mode, in-process Daily bot), RTVI enabled
when `ENABLE_BREEZE_BUDDY_DAILY_EVENTS=true` (default true). Three flows over the RTVI channel:

| Flow | Direction | Status today | Work |
|---|---|---|---|
| **Transcripts** (user speech + bot text) | server→client | ✅ already emitted (`RTVIObserverParams(user_transcription_enabled=True, bot_tts_enabled=True)`, pipeline.py:541-555); SDK already emits `'transcript'` | Frontend ingest only |
| **Generative UI** (carousels/cards) | server→client | ❌ ui-ops only stream over chat SSE; nothing over voice RTVI | **Backend A1** + SDK `ui-op` event + widget render |
| **Click → input** (carousel/product selection) | client→server | ❌ `on_client_message` handles only `tts-speak` + approval decisions | **Backend A2** + SDK `sendUserAction` + widget wiring |

Backend transport already exists: `Agent._emit_rtvi_event(event_type, payload)`
(agent/__init__.py:216-231, pushes `RTVIServerMessageFrame(data={type,timestamp,payload})`)
and the `on_client_message` handler (agent/__init__.py:827-866). The automatic agent's
charts-over-RTVI (`features/charts/rtvi/rtvi.py`) and `LLMSpyProcessor`
(`automatic/processors/llm_spy.py:321-325`) are the **transport pattern** to mirror
(NOT its HITL — out of scope per standing mandate).

---

## Part A — clairvoyance backend

### A1. Generative UI over the voice RTVI channel

The LLM produces UI exactly as in chat: it emits `<ui_stream>…</ui_stream>` JSONL blocks in
its text output. We reuse chat's entire extraction/heal/validate stack and only swap the
**SSE sink for an RTVI sink**.

**A1.1 — Prompt: make the voice LLM emit `<ui_stream>`.** Voice templates don't include the
generative-UI instructions today. Inject the SAME instructions chat uses so the voice LLM
emits ui_stream blocks (and a short *speakable* prose line alongside, since the prose becomes
TTS). Reuse chat's prompt-injection hook + `resolve_allowlist(...)` per-template gating
(chat does this at `chat/agent.py:200-209`). *Confirm the exact injection point during impl —
chat adds it in the flow/builder path.*

**A1.2 — `VoiceUiStreamProcessor` (new `FrameProcessor`).** Insert between the LLM service and
TTS in `build_pipeline()` (pipeline.py:~194-519), gated on `is_daily_mode AND ui_enabled`.
For each LLM text frame:
- Feed text to a session-scoped `UiStreamExtractor` (ui_stream.py:78 — handles markers split
  across frames via its carry buffer).
- `TextOut` (prose, markers stripped) → forward downstream to TTS **so the bot never speaks the
  JSON** (this is the load-bearing reason the processor sits *before* TTS).
- `JsonlOpLine` → run through `heal_op_line` + `parse_op_line` + catalog `validate_props`
  (reuse `process_op_line`, ui_stream.py:584-677, swapping the SSE factory for the RTVI emit)
  → `await bot._emit_rtvi_event('ui-op', {'op': <validated_op_dict>})`.
- Model it on `LLMSpyProcessor` (automatic/processors/llm_spy.py) — same "tap frames, emit RTVI"
  shape, but tapping LLM **text** (not function-result) frames.

**A1.3 — known-ids scope.** Chat resets `_known_ui_ids` per turn; a voice call is one long
session. Scope known-ids **per assistant response** (reset on each bot-turn start) to avoid an
unbounded id registry over a long call (recon risk).

**A1.4 — UiOp wire shape (unchanged from chat):**
`{op:'add'|'replace'|'remove', id, type (add only), parent (add non-root), props}`.
Emit envelope: `_emit_rtvi_event('ui-op', {op})` → client sees `{type:'ui-op', timestamp, payload:{op}}`.

### A2. Client input injection (carousel/product click → user turn)

Extend `on_client_message` (agent/__init__.py:827-866) with a `ui-action` branch (mirrors the
existing `tts-speak` / approval-decision branches):

```python
elif message.type == "ui-action":          # client→server, from SDK sendUserAction
    data = message.data or {}
    text = (data.get("msg") or "").strip()
    if not isinstance(text, str) or not text:
        logger.warning("[ui-action] empty/malformed"); return
    text = text[:UI_ACTION_MAX_CHARS]       # new static.py cap, mirror TTS_SPEAK_MAX_CHARS
    if self.task:
        await self.task.queue_frame(
            LLMMessagesAppendFrame([{"role": "user", "content": text}], run_llm=True)
        )
```

- `LLMMessagesAppendFrame(..., run_llm=True)` is the exact mid-call user-turn injection pattern
  already used by `user_idle.py:85-91` and (PTT) the automatic agent. Frame enters pipeline top;
  Pipecat's interruption strategy handles barge-in natively — **no custom barge-in logic**.
- The backend injects `msg` into context only; it emits **no** transcript echo. The widget
  renders the `display` bubble optimistically (see C3) — matches how chat's carousel click
  optimistically appends the user bubble.
- **No DB audit / no persistence change** — voice turns already drain to `chat_message` on
  `end_conversation`; the injected user turn rides the LLM context like any spoken turn.

### A3. Transcripts — confirm only

Already flow over RTVI (A2 table). No backend change. Confirm `ENABLE_BREEZE_BUDDY_DAILY_EVENTS=true`
in the target env (without it `_rtvi_processor` is None → ui-ops + approvals silently no-op).

### A4. Wire-contract additions (single source of truth)

- **server→client** RTVI `ui-op` — payload `{op: <UiOp>}` (chat-identical UiOp).
- **client→server** RTVI `ui-action` — data `{msg, display?}`.
- Transcripts: existing RTVI transcription events (unchanged).

### A5. Backend verification

`uv run pyrefly check`; new tests: `VoiceUiStreamProcessor` (prose passthrough + marker strip +
op emit, reusing chat's ui_stream test fixtures), `on_client_message` ui-action injection
(queues `LLMMessagesAppendFrame`), telephony/no-RTVI no-op. Docs: `docs/DAILY_RTVI_EVENTS.md`
(+`ui-op`, +`ui-action`).

---

## Part B — client-sdk (`loom/packages/client-sdk`)

### B1. Voice `ui-op` event (mirror chat)
- `session/types.ts`: add `'ui-op': (e: {op: UiOp}) => void` to `VoiceSessionEventMap`
  (~L127-168); import `UiOp` from `../ui/types.js`. Optional `onUiOp` sugar.
- `session/session.ts` `routeServerMessage` (~L124-186): add `case 'ui-op'` reading
  `obj.payload.op`, validate `{op,id}` (mirror `_turn-engine.ts:222-227`), `emitter.emit('ui-op',{op})`.

### B2. `sendUserAction` (carousel click → live voice)
- New `VoiceSession.sendUserAction(action: UiAction & {type:'to_assistant'})` → fire-and-forget
  `sendMessage('ui-action', {msg: action.msg, display: action.display})` (mirror `respondToApproval`,
  session.ts:375-393; try/catch — `sendMessage` throws when disconnected).

### B3. Speaker mute (bot audio)
- `session.ts`: `isAudioMuted` state; `setAudioMuted(muted)` sets `audioElement.muted` AND
  `assistantAudioTrack.enabled = !muted` (defensive). **Re-apply on `onTrackStarted`** (track can
  be replaced mid-call — recon risk). Expose `muteAudio()/unmuteAudio()/setAudioMuted`; emit
  `'audio-mute-change'`; include `isAudioMuted` in `getState()`.

### B4. Store ingestion (the unified-history core) — `store/_buddy-chat-store.ts`
The widget consumes the SDK **only** through `BuddyChatStore`, so the merge lives here:
- **Transcripts → bubbles:** subscribe (via the store's voice wiring) to `'transcript'`. Map
  `UserTranscript`→ a `ChatTextMessage{role:'user'}`; `AssistantTranscript` (streaming, accumulates,
  `isComplete`) → an assistant `ChatTextMessage` that updates then finalizes. New action
  `ingestVoiceTranscript(entry)`.
- **Voice ui-ops → cards:** subscribe to `'ui-op'`; buffer into a `ChatUiMessage` attached to the
  current assistant turn — reuse the existing `appendUiOp`/`activeUiMessage` machinery
  (_buddy-chat-store.ts:472-496), keyed off the voice turn instead of an SSE turn.
- **Voice delegates** for the widget surface: add `sendUserAction`, `setAudioMuted` (+ getState
  audio flag) to the `StoreSession` Pick and the store's public API (the widget can't reach the raw
  session). `transferToVoice`/`endVoice` already exist.

### B5. SDK tests
`approval.test.ts`-style harness: `ui-op` server-message → `'ui-op'` event + store `ChatUiMessage`;
`sendUserAction` → POST/RTVI body+type; transcript→bubble mapping (partial→final); `setAudioMuted`
toggles element+track and survives a track-restart. Bump **0.4.0 → 0.5.0** + CHANGELOG (additive).

---

## Part C — widget (`loom/packages/breeze-buddy-assist-widget`)

### C1. Layout: chat stays visible; footer becomes voice controls
- `BbWindow.svelte`: stop rendering the voice pane as a full-cover `overlay` (`position:absolute;
  inset:0`). Keep `BbTextPane` in `body` (flex:1, already reflows). The voice controls live in the
  **footer** (replacing the composer), so the transcript simply shrinks by the footer height —
  pure flex, no CSS hiding. Approval cards move inline into the transcript (BbTurn already renders
  `kind:'approval'`) — drop the BbVoicePane bottom-sheet.
- `BuddyAssist.svelte` footer (~L973-983): `{#if voiceLive}` render **`BbVoiceControls`** (new) else
  `BbComposer` as today. **No text input during voice.**
- `approvalsDisabled` should NO LONGER include `voiceLive` (cards are now interactive during voice).

### C2. `BbVoiceControls.svelte` (new, presentational)
Footer bar mirroring the composer pill chrome: **Mic mute/unmute** (`voiceSession.setMicEnabled`,
reflect `mic-change`) · **Speaker off** (`store.setAudioMuted`, reflect `audio-mute-change`) ·
**End** (red, `stopVoice()`) · a **Listening/Speaking/Connecting** status badge (driven by
`assistant-speech-*` + `voiceStatus`). Reuse `BbVoicePane`'s existing button SVGs/styles; styles in
`internal.ts` under one hoisted `.bb-voice-controls` class (check:styles). Then **delete/retire
`BbVoicePane.svelte`** (its mascot can become a small inline status glyph if wanted).

### C3. Carousel click during voice → inject + optimistic bubble
`handleAction` (BuddyAssist.svelte:510-546): branch on `voiceLive`:
```
if (voiceLive && voiceSession) {
  store.appendUserBubble(action.display ?? action.msg);   // optimistic (no server echo)
  store.sendUserAction({type:'to_assistant', msg: action.msg, display: action.display});
} else { send(cleaned, bubble); }                           // chat path unchanged
```
`open_url` actions behave as today. (Optimistic bubble because the backend injects `msg` into
context but emits no transcript echo — confirmed in A2.)

### C4. Transcript + ui-op rendering — reuse, don't rebuild
Because B4 feeds voice transcripts + ui-ops into the **same store message list**, `BbTextPane →
BbTurn → BbUiPane` render them with zero new rendering code. Only wiring: in `startVoice`
(~L603-661) the store's voice subscriptions (B4) are what populate the list; verify auto-scroll
(BbTextPane.svelte:138-164) follows new voice bubbles.

### C5. Build & deploy
Voice deps (`@pipecat-ai/*`, `@daily-co/daily-js`) already in the widget (lazy chunks). `pnpm
--filter client-sdk check|test|build` then widget `check|check:styles|build`; assert the dist file
list; exercise from the BUILT bundle in the harness (`modes="text,voice"`, port 5180,
`api-base`→local clairvoyance branch). Docs: SDK README/CHANGELOG, widget README (voice mode UX).

---

## 3. Rollout (deploy order is load-bearing)

1. **Backend deploy** (clairvoyance) — wire-safe against old SDK: new RTVI `ui-op` events are
   ignored by an old `routeServerMessage` (switch has no default); `ui-action` is never sent by an
   old SDK. Degradation = no carousels/clicks in voice, transcripts still flow.
2. **loom deploy** (SDK 0.5.0 + widget, one release) — new SDK on old backend is dormant (no
   `ui-op` frames arrive; `sendUserAction` injects nothing). Publish ALL `dist/assist*.js`.
3. **Template opt-in LAST** — only a template that includes the generative-UI prompt instructions
   (A1.1) will render UI in voice; roll out per-template after both deploys are live.
4. `ENABLE_BREEZE_BUDDY_DAILY_EVENTS=true` required (already default).

## 4. Risks / decisions to confirm during implementation

- **A1.1 prompt injection point** — exact chat hook that adds the `<ui_stream>` instructions +
  allowlist; reuse it for voice. (Only real unknown; everything else has a verified pattern.)
- **TTS must never speak the JSON** — guaranteed by placing `VoiceUiStreamProcessor` *before* TTS
  and forwarding only marker-stripped prose. Prompt should keep the prose short + speakable.
- **Barge-in semantics** — a click while the bot is speaking injects a user turn; works only with
  interruption ENABLED (default). With `disabled_discard` the click would be dropped — document /
  ensure widget voice uses interruption-enabled.
- **Telephony** — Daily-only (no `_rtvi_processor` on Twilio/Plivo/Exotel); out of scope.
- **known-ids growth** over a long call — reset per assistant response (A1.3).

## 4. Resume parity — voice UI ops persist as `ui_blocks` (added 2026-06-18)

Found in live testing: after ending a voice call and reopening the widget, the
**carousels were gone** while text remained — the conversation looked half-
forgotten. Root cause: chat persists each assistant turn's UI ops in the
`chat_message.ui_blocks` column (the widget's `resumed` handler repaints them),
but the voice `end_conversation` drain only wrote `content` (text), never
`ui_blocks`. So a widget reload (`GET /widget/session/{id}/state`) returned voice
turns with empty `uiBlocks` → no carousels.

Fix is **backend-only** (the loom `resumed` handler already repaints `uiBlocks`
channel-agnostically — once persisted, voice carousels restore exactly like chat):

- **`VoiceUiStreamProcessor`** accumulates the UI ops of each assistant turn into
  an ordered `ui_turn_sink` (one bucket per *spoken* response — empty when a turn
  drew no UI; a prose-less all-UI turn is dropped, the §4a gap). Interruption-safe:
  a barge-in commits the pending bucket on the next response's start frame — or on
  the call-ending frame if no further turn follows — so the sink stays 1:1 with the
  assistant messages the context aggregator commits.
- **`build_pipeline` / `Agent`** thread the bot's `self._voice_ui_turns` list in as
  the sink.
- **`end_conversation`** calls `attach_voice_ui_blocks(new_messages, ui_turns)` to
  zip the buckets onto the assistant entries it drains; **`drain_voice_into_chat_session`**
  passes `ui_blocks` to `insert_chat_message` (same column + shape chat uses).

Text + LLM memory already worked (the drain persisted text and `/voice/connect`
re-seeds `prior_history` into the next call's LLM context — verified in logs). The
only missing piece was the UI, now at parity with chat.

## 4a. Known limitation / deferred follow-up

- **No `summarize_ui_ops` referential memory for voice (deferred — not small).** Chat appends a
  compact `[ui rendered: …]` summary as a `visibility=internal` block so the LLM remembers what it
  showed ("the green one") without the widget ever displaying it. Voice's context is built by the
  frame aggregator and `end_conversation` reads the LLM message history verbatim — there is **no
  internal-visibility filter**, so injecting the summary would pollute the persisted transcript with
  synthetic `[ui rendered: …]` lines. Doing it right needs a transcript-side filter, not just an
  injection. Low impact for voice: the bot's *spoken* prose (which is in context) already names the
  items, so only pure-UI-no-speech turns lose referents — rare in voice. Implement alongside a
  transcript-filtering hook if/when referential misses are observed.

## 5. Suggested sequencing (stackable PRs)

1. **clairvoyance PR** — A1 (`VoiceUiStreamProcessor` + prompt) + A2 (`ui-action` inject) + tests + docs.
2. **loom SDK PR** — B1–B5 (ui-op event, sendUserAction, speaker mute, store ingestion) + 0.5.0.
3. **loom widget PR** — C1–C5 (layout, `BbVoiceControls`, click routing, retire `BbVoicePane`).

Backend and SDK are independently dormant, so order is flexible; widget depends on the SDK PR.
