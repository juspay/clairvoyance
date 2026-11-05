# Tool-Based Speech Architecture

How Breeze Buddy templates speak when the LLM never writes prose: every spoken
line is a **tool call with a `say` payload**, the line is queued to TTS the
moment the function *name* is decodable (before arguments finish streaming),
and calls end through a dedicated closing node whose actions run after the
goodbye drains.

This doc covers the template format, the runtime contracts that make it work,
and the caveats — especially **how to end a call correctly** (there is exactly
one right way, and the obvious way silently never fires).

Reference implementation lives in `app/ai/voice/agents/breeze_buddy/` —
`template/` (`transition.py`, `builder.py`, `types.py`) plus `agent/`
(`early_speech.py`, the service-level router) — and is covered by
`tests/test_early_speech.py` + `tests/test_early_speech_templates.py`.

---

## 1. Why tool-based speech

In the default LLM pipeline the model writes the utterance token by token and
TTS starts after the first sentence of prose exists. Two problems on telephony:

- **Latency** — the caller waits for tokens before hearing anything.
- **Non-determinism** — the model paraphrases, rambles, or writes disclaimers.

Tool-based speech fixes both: the line is authored in the template (fixed,
reviewable text with `{variable}` substitution), and the router queues it to
TTS while the model is still streaming the function *name*. The model's job is
reduced to picking the right line for the conversation state.

The cost is that the model can no longer end a turn with prose — which is why
this architecture requires `tool_choice: "required"` and an explicit
call-ending mechanism (sections 4 and 5).

## 2. Template modes

A template's `flow` object is one of two shapes. `builder.py`
(`build_flow_config`) branches on it.

### Direct mode — one synthesized node

```json
"flow": {
  "mode": "direct",
  "functions": [ ... ],
  "system_prompt": "..."
}
```

All functions live in a single implicit node. Simplest to author, but **cannot
end the call by itself** (section 6) — the call ends on idle timeout or when
the caller hangs up. Fine for screening trees; not for outcome-driven calls.

### Node mode — explicit nodes

```json
"flow": {
  "nodes": [ ... ],
  "initial_node": "initial",
  "end_conversation_callbacks": ["service_callback"]
}
```

A nodeless flow without `"mode": "direct"` fails validation ("initial_node
not found"). Use node mode whenever the call must hang itself up — the
standard shape is two nodes: the conversation node plus a closing node
(section 5).

## 3. Say functions

Any function entry may carry a `say`. Three shapes (normalized in
`early_speech.py::_collect_say_functions`):

| Shape | Behavior |
|---|---|
| `"say": "fixed text"` | Whole line early-fired at name-decode. |
| `"say": {"text": "..."}` | Same as the string form. |
| `"say": {"prefix": "Your order number is", "arg": "order_id"}` | Prefix fired early; the decoded `order_id` value is spoken by the handler right after it. For lines that must quote the caller's choice back. |

- `{variable}` placeholders substitute from template vars (lead payload +
  injected fields). Unknown variables stop the early fire (the line waits
  for the handler); the handler path drops any placeholder still
  unresolved instead of speaking braces.
- Never put the `arg`'s own placeholder inside `prefix` — the slot is
  dropped and the value appended after the prefix (warned at load). A
  same-named template var would otherwise leak a stale lead value.
- **Outcome tools carry `say` too.** A function with `hooks`
  (e.g. `update_outcome_in_database`) still speaks its closing line — the say
  pool is everything except global-typed functions.
- A `say` function must not also declare a `language` property — the text
  itself carries the language (names in these templates are fully collapsed,
  no `_hi`/`_en` suffixes).

## 4. Early-fire and the contracts that keep it stable

### The router

`EarlySpeechRouter` (`agent/early_speech.py`) matches the streamed function-name
fragment against the say pool on a **unique-prefix** basis and queues the line
straight to TTS. You'll see this at call start:

```
[early-speech] armed for 32 say function(s) (0 with dynamic LLM text, 0 other tool name(s) guarded)
```

### Naming rules (hard requirements)

- Names must be **unique** across the whole flow (direct functions plus every
  node's functions).
- **No name may be a strict prefix of another** (`card_amount` and
  `card_amount_extra` would break unique-prefix decode).
- Keep names short — fire depth is the length of the shortest unique prefix.
  The six qwen_harness templates were renamed to a flat short scheme
  (`check_step`, `card_amount`, `step_kyc`, …) to keep max fire depth ≤ 8.

Both rules are enforced at load: a name carrying *different* `say` config in
two places fails attach (the say pool is flat — one node would silently speak
the other's line), and a strict-prefix pair logs a warning (the shorter name
can never uniquely decode, so it forgoes early fire and speaks at handler
time). The test suite asserts both. The fire also defers while any **non-say**
tool (adapter-routed global — flat or top-level — the config-synthesized
knowledge-base tool, MCP tool) shares the current prefix, so a mixed tool list
can never mis-fire a say line. A name declared with `say` in one place and
without it in another is rejected at load: the bare twin would speak the say
line and inherit the empty-result contract.

### The empty-result contract (`transition.py`)

Say functions return `({}, None)` from the transition handler — an **empty,
falsy result**. This is load-bearing:

- The response aggregator (`llm_response_universal.py`) only re-runs the LLM
  when `frame.result` is truthy.
- Templates run with `tool_choice: "required"` (set it in
  `configurations.llm_configurations`), so any re-run *forces another tool
  call*.
- A truthy say result would therefore loop the model through tool calls with
  no user turn in between — the bot repeating itself forever (the
  "keeps calling tools without stopping" bug).
- Empty result = speak once, then wait for the user. flows preserves `{}`;
  only `None` results get replaced with an ack object.

The handler keys off the router's `says` set (`early_speech.py` exposes it as
a property) — that's the complete say pool per section 3.

### Turn rhythm

Because the result frame is a non-system frame, pipecat's TTS service holds it
(processing pause + serialization queue) until the say line has finished
draining. Only then does the aggregator release the turn. This is what makes
every timing guarantee below work: **nothing queued later can overtake speech
already queued**.

## 5. Ending the call — the 2-node closing pattern

This is the part with real caveats. The pattern:

1. The six **outcome tools** on the conversation node carry
   `"transition_to": "end_conversation_node"` plus their outcome hook. The
   hook (DB outcome write) fires when the tool is called; the goodbye is the
   tool's `say`.
2. A **closing node** runs `mute_stt` and `end_conversation` as **pre-actions**.

Exact closing node:

```json
{
  "node_name": "end_conversation_node",
  "respond_immediately": false,
  "functions": [],
  "pre_actions": [
    {"type": "function", "handler": "mute_stt"},
    {"type": "function", "handler": "end_conversation"}
  ],
  "role_messages": [
    {
      "role": "system",
      "content": "You are the closing node. The goodbye has already been spoken by the call-ending tool. Say nothing and call nothing — the call ends now."
    }
  ],
  "task_messages": []
}
```

### Why `respond_immediately: false`

flows defaults to running the LLM on node entry. The closing node has zero
functions, and with `tool_choice: "required"` on the service, a forced run
with an empty tool list is a guaranteed Azure 400 on every call. The field is
opt-in per node (`FlowNodeModel.respond_immediately`, documented in
`field_reference.json`); unset behaves exactly as before.

### Why `end_conversation` must be a PRE-action, not a post-action

This is the trap. With `respond_immediately: false`, flows defers post-actions
until the next `BotStoppedSpeakingFrame` reaches the pipeline's downstream
end. In this architecture that trigger has **already fired** by the time the
node is entered:

```
goodbye drains ──► BotStoppedSpeakingFrame ──► aggregator releases the
held tool result ──► node transition ──► _set_node schedules the
deferred post-action        ▲ (the frame it waits for passed one step earlier)
```

After node entry nothing ever speaks again (STT muted, no functions, no LLM
run), so no future `BotStoppedSpeakingFrame` arrives — the deferred
`end_conversation` **never executes**, and the call sits in dead air until
the caller gives up and hangs up. This exact failure shipped once (2026-09-07,
call `9e905715`): 23.6 s of silence after the goodbye, caller disconnect.

Pre-actions run at node entry — which is *always after* the goodbye, because
the transition itself is gated on the speech having drained (section 4). And
function actions execute via `FunctionActionFrame` at the pipeline's
downstream end, ordered behind any speech still queued, so even a transition
that lands mid-speech still hangs up after the audio finishes. Order between
the two pre-actions is enforced by flows' ongoing-actions wait: `mute_stt`
completes before `end_conversation`'s frame is queued.

### Why the goodbye is never cut

Three independent orderings, verified against pipecat 1.1.0 source:

1. The tool result frame (and the transition) is held behind the goodbye by
   the TTS service — the transition cannot run early.
2. The `FunctionActionFrame`s are non-system frames — they serialize behind
   any remaining speech.
3. The `EndFrame` is only emitted by the `end_conversation` handler *after*
   its action frame reached the sink — i.e. after everything before it.

### Edge cases

- **Barge-in during the goodbye:** the interruption produces its own
  bot-stopped event, which releases the held result frame the same way —
  transition runs, call ends. The goodbye is cut by the caller's own
  interruption, and the outcome was already recorded at tool time. Same
  behavior as legacy end nodes.
- **Caller talking over the goodbye's end:** transition fires anyway; the
  call ends. Also identical to legacy.
- **Double finalization:** `end_conversation` guards on
  `context.conversation_ended` — if the pre-action races a disconnect, DB
  writes and callbacks run exactly once.
- **Multiple outcome tools in one turn:** flows keeps the first pending
  transition; the rest resolve into the already-ending node. Harmless.

## 6. Direct mode's limitation

Under `tool_choice: "required"` a direct-mode template cannot end its own
call: the model has no prose path and no second turn to invoke a hangup tool,
so the call ends on idle timeout (or the caller). If a template must hang up
deterministically, convert it to the 2-node pattern (section 5). The upgrade
repo replaces this pattern with a `say.end_call: true` flag on the function
itself (the transition handler speaks the line and finalizes); porting that
flag is the planned evolution.

## 7. Converting a direct template to 2 nodes — checklist

1. Wrap the flow: `nodes` + `initial_node: "initial"` +
   `end_conversation_callbacks` (copy from the existing template).
2. Move `system_prompt` verbatim into the initial node's `role_messages`.
3. Delete any `end_conversation` builtin from `functions`; give each outcome
   tool `"transition_to": "end_conversation_node"` (keep its hooks and `say`).
4. Replace the prompt's "call end_conversation to hang up" line with: the
   call-ending tools hang up automatically once their line finishes — never
   call a function just to end the call.
5. Add the closing node exactly as in section 5 (pre-actions, no post-actions,
   `respond_immediately: false`).
6. Re-run the naming checks (unique, no strict prefixes — section 4).
7. Update `tests/test_early_speech_templates.py` invariants if the template
   is covered there.

Do **not** touch `llm_configurations` (`tool_choice: "required"` stays) or any
`say` text while converting.

## 8. Testing

- `tests/test_early_speech.py` — router behavior, the empty-result contract,
  `tool_choice` Literal validation, attach no-op without TTS.
- `tests/test_early_speech_templates.py` — template invariants per mode and a
  full build through `FlowConfigBuilder`, including resolution of the closing
  node's action handlers.
- Live-call pass criteria after any change here: at the outcome tool's
  transition you should see `mute_stt called` immediately followed by
  `End conversation handler called ... finalizing call` → `EndFrame queued`,
  and the call drops about a second after the goodbye finishes.
