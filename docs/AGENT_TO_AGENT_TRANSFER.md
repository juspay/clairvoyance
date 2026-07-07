# Agent-to-Agent Transfer (Runtime Template Handoff)

**Status:** Design — approved direction, not yet implemented
**Scope:** Breeze Buddy agent-mode voice (telephony: Twilio/Plivo/Exotel; Daily)
**Pipecat:** designed against `pipecat-ai==1.1.0` (pinned), verified forward-compatible with pipecat master (1.5.0)

---

## 1. What this is

A global function — `connect_to_agent` — callable by the LLM from any template, that
**tears down the current Pipecat pipeline completely and rebuilds it from a different
template** (new prompts, tools, node graph, and possibly different STT/TTS/LLM
providers), **without dropping the underlying connection** — the phone call
(telephony websocket) or Daily room stays alive throughout.

```
Caller ──► [phone call / Daily room] ──► Agent A (template A: pipeline, STT, LLM, TTS)
                                              │  LLM calls connect_to_agent("sales")
                                              ▼
Caller ──► [SAME call, never dropped] ──► Agent B (template B: NEW pipeline, STT, LLM, TTS)
```

### Why a full pipeline rebuild (and not a prompt swap)?

A template fully defines an agent (`template/types.py` — `TemplateModel`):
the `flow` node graph (prompts, per-node functions, transitions, global functions)
**and** `configurations` (STT provider/language, TTS provider/voice, LLM provider,
VAD, turn detection, interruption, idle handling, MCP servers). Agent B may run
Deepgram+Cartesia while Agent A ran Soniox+ElevenLabs. Swapping nodes inside one
`FlowManager` cannot change services — global functions and services are frozen at
FlowManager construction (`pipecat_flows/manager.py`, `_set_node` merges
`self._global_functions` fixed at init). Only a rebuild honours the
"template defines the agent" contract.

### How this differs from existing transfer mechanisms

| Mechanism | What it does | Connection | Target |
|---|---|---|---|
| `connect_to_live_agent` (warm transfer) | Bridges caller to a **human** via conference/MPC, then tears the bot down | Call ends for the bot | Human phone number |
| `hold_and_consult` | Spins up a **second call** on another template, bridges results via Redis pub/sub | Two parallel calls | Another template (separate leg) |
| IVR template selection | Picks the template **before** the pipeline starts | N/A | Template (pre-call) |
| **`connect_to_agent` (this doc)** | **Replaces the running agent with another template on the same call** | **Never dropped** | Template (mid-call) |

---

## 2. Verified constraints (pipecat 1.1.0 — the pinned version)

All line refs are into `pipecat-ai==1.1.0` unless noted. These four facts shape the
entire design; each was verified by reading the installed package source.

1. **`EndFrame`/`CancelFrame` hang up the real phone call.** The Twilio/Plivo
   serializers default to `auto_hang_up=True` and call the provider's REST API to
   terminate the call the moment an End/Cancel frame passes through
   (`serializers/plivo.py:127-131`, same in `twilio.py`). We already fight this in
   `handlers/internal/warm_transfer.py:319` by setting
   `serializer._hangup_attempted = True`.

2. **Transport teardown closes the connection.** Transport `stop()`/`cancel()` →
   `client.disconnect()` → ref-counted → `websocket.close()` when the count hits 0
   (`transports/websocket/fastapi.py:157-166`); Daily likewise `leave()`s the room
   (`transports/daily/transport.py:933-939`).

3. **A transport instance cannot be restarted.** Input/output transport processors
   latch a one-way `_initialized` flag (`fastapi.py:239/249`; Daily
   `transport.py:1797/2087`). A second `StartFrame` on the same instance silently
   no-ops the bring-up (no receive task, no `set_transport_ready`). **But a fresh
   transport instance wrapping the same live websocket works** — a new
   `FastAPIWebsocketClient` wrapper resets all state, and
   `_create_telephony_transport(ws, params, transport_type, call_data)`
   (`runner/utils.py:417`) takes the ws and pre-parsed call data explicitly, so no
   re-parsing of the provider's `start` message is needed.

4. **Provider STT/TTS websockets open in `start()`, not `__init__`.** Soniox
   (`services/soniox/stt.py:353-360`), ElevenLabs (`elevenlabs/tts.py:624-632`),
   Cartesia (`cartesia/tts.py:484-492`), Deepgram (`deepgram/stt.py:490-497`) all
   connect on `StartFrame` and disconnect on `EndFrame`/`CancelFrame`. Constructing
   service objects opens **zero** provider connections.

Two helpful behaviors, also verified:

- `on_client_disconnected` fires only when the **client** actually disconnects — not
  when we tear the transport down ourselves (`fastapi.py:322-325`). So a
  self-initiated pipeline stop does not spuriously trigger
  `_handle_unexpected_disconnect` → `end_conversation`.
- `PipelineTask.queue_frame()` just puts to an `asyncio.Queue` (`pipeline/task.py:570-585`)
  — frames may be queued before `runner.run()` starts and are processed at startup.
  (Relevant for Daily generation ≥ 2 flow init, §6.8.)

---

## 3. Design: the generation loop

Treat one call as **one connection + N pipeline generations**. Everything
Clairvoyance already does per call is kept — and simply run in a loop.

```
Agent.run()
├── connection setup (ONCE): ws accept + parse_telephony_websocket, OR Daily room
│   (lead resolution, inbound handling, initial template load — all unchanged)
└── while True:                                          ← the only new control flow
        _run_generation():
            create_services(configurations)              ← existing fn, untouched
            build_pipeline(transport, stt, llm, tts, …)  ← existing fn, untouched
            create_pipeline_task(...)                    ← existing fn, untouched
            setup_flow_manager(...)                      ← existing fn, untouched
            fresh transport over the SAME connection
            runner.run(task)                             ← blocks, as today
        if self.pending_transfer:
            _apply_transfer()   # swap template, snapshot context, rebuild inputs
            continue            # next generation = same code path as a cold start
        break                   # real call end → finalize ONCE
```

`connect_to_agent` never rebuilds anything itself. It **validates the target, snapshots
handoff state, sets `pending_transfer`, and ends the current task without hanging up**.
The loop does the rest through the exact same build path that runs at call start —
generation 2 is not a special code path, which is the core reliability property.

### Transfer sequence (telephony)

```
 LLM calls connect_to_agent(target="sales", handoff_summary="…")
   │
   1. Load + render + validate target template          ── any failure → error result
   │     (FlowConfigLoader, validate_template_compat)      to LLM; CALL CONTINUES
   2. Guards: max transfer depth, channel support, mode
   3. [optional] pre_tts_message speaks & completes      ── existing _speak_and_wait
   4. bot.pending_transfer = PendingAgentTransfer(...)      (dispatcher does this)
   5. suppress_auto_hangup(bot.transport)                ── serializer must not
   6. await bot.task.stop_when_done()                       REST-hangup the call
   │     EndFrame drains → TTS finishes → services
   │     disconnect → transport "closes" (no-op, §6.2)
   ▼
 runner.run() returns → loop sees pending_transfer
   7. Snapshot context.messages → prior_generation_messages
   8. Swap self.template/configurations/template_vars; update lead template in DB
   9. Rebuild: flow_builder, vad_analyzer, FRESH transport over same ws
  10. _run_generation() → new pipeline → new StartFrame → Agent B's STT/TTS connect
  11. Agent B's initial node runs with respond_immediately=True → B speaks first
```

The caller hears the (optional) handoff line, then a sub-second pause, then Agent B.

---

## 4. Provider concurrency — why this never doubles connections

STT/TTS providers enforce account-level concurrent-connection limits. The generation
loop is **strictly sequential** with provider connections:

- Validation/pre-build of Agent B creates **config objects only** (constraint #4:
  sockets open in `start()`).
- Agent A's STT/TTS sockets close during EndFrame teardown.
- Agent B's sockets open only when the new pipeline's StartFrame flows — strictly
  after A's closed.

At any moment, one call holds exactly one set of provider connections. A transfer
adds **zero** to the concurrent-connection footprint. (Contrast with pipecat 1.5.0's
multi-worker examples, where inactive agents hold keepalived provider sockets for the
whole call — see §11.)

---

## 5. What changes where — file map

| # | File | Change | Size |
|---|---|---|---|
| 1 | `agent/__init__.py` | Generation-loop refactor of `run()`; new state fields | refactor |
| 2 | `utils/transport/nonclosing.py` | **new** — `NonClosingWebSocket` proxy | ~30 lines |
| 3 | `utils/agent_transfer.py` | **new** — `PendingAgentTransfer`, `suppress_auto_hangup` | ~40 lines |
| 4 | `handlers/internal/agent_transfer.py` | **new** — `connect_to_agent` handler | ~120 lines |
| 5 | `handlers/internal/builtin_dispatcher.py` | registry entry | 2 lines |
| 6 | `template/types.py` | `AgentTransferConfig` on `ConfigurationModel` | ~25 lines |
| 7 | `handlers/internal/end_conversation.py` | transfer guard + transcript merge | ~15 lines |
| 8 | `agent/flow.py` (`prepare_initial_node`) | optional handoff messages param | ~10 lines |
| 9 | `chat/disabled.py` | add `connect_to_agent` to `CHAT_DISABLED_NAMES` | 1 line |

Modes untouched: chat, stream (DAILY_STREAM), IVR, realtime all bypass the transfer
path entirely (the function only exists on agent-mode voice pipelines, which are the
only ones with a FlowManager).

---

## 6. Implementation guide (with code sketches)

### 6.1 Agent state + the loop (`agent/__init__.py`)

New fields in `Agent.__init__`:

```python
# Agent-to-agent transfer (generation loop)
self.pending_transfer: Optional[PendingAgentTransfer] = None
self.transfer_count: int = 0
self.generation: int = 1
# Transcript snapshots from completed generations, merged at final end.
self.prior_generation_messages: List[Dict[str, Any]] = []
# Handoff system messages to seed the NEXT generation's initial node.
self._handoff_messages: List[Dict[str, str]] = []
# Stored at telephony setup for transport rebuilds (gen >= 2).
self._telephony_transport_type: Optional[str] = None
self._telephony_call_data: Optional[dict] = None
self._runner_args: Optional[RunnerArguments] = None  # daily rebuilds
self._flow_initialized: bool = False  # per-generation guard, see 6.8
```

`_setup_telephony_transport` keeps its current body with two additions: store
`transport_type`/`call_data` on `self`, and create the transport over the proxy:

```python
self._telephony_transport_type = transport_type
self._telephony_call_data = call_data
self._ws_for_transport = NonClosingWebSocket(self.ws)   # see 6.2
self.transport = await _create_telephony_transport(
    self._ws_for_transport, params, transport_type, call_data
)
```

> `self.ws` stays the **raw** websocket everywhere else. Pre-pipeline error paths
> (`close_websocket_safely(self.ws, …)`) must still really close; only the pipecat
> transport gets the non-closing proxy.

`run()` refactor — extract the current body from `create_services` down through
`runner.run` into `_run_generation()`, verbatim, and wrap:

```python
# in run(), replacing the single build-and-run block:
while True:
    await self._run_generation()          # steps 4–11 of today's run(), unchanged
    if not self.pending_transfer or self.conversation_ended:
        break
    transfer = self.pending_transfer
    self.pending_transfer = None
    await self._apply_transfer(transfer)  # see below

# telephony: the Agent now owns the final close (the proxy swallowed pipecat's)
if not self.is_daily_mode and self.ws:
    await close_websocket_safely(self.ws, code=1000, reason="Conversation ended")
```

`_apply_transfer` — swap inputs and reset per-generation state:

```python
async def _apply_transfer(self, transfer: PendingAgentTransfer) -> None:
    self.transfer_count += 1
    self.generation += 1

    # 1. Snapshot outgoing generation's transcript (context still alive here).
    if self.context:
        self.prior_generation_messages.extend(
            m for m in self.context.messages
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        )
        self.prior_generation_messages.append({
            "role": "system",
            "content": f"[call transferred to agent template '{transfer.template.name}']",
        })

    # 2. Record + persist. Same precedent as IVR template selection.
    (self.lead.metaData or {}).setdefault("agent_transfers", []).append({
        "from_template_id": str(self.template.id),
        "to_template_id": str(transfer.template.id),
        "at": datetime.now(timezone.utc).isoformat(),
        "generation": self.generation,
    })
    await update_lead_template(
        lead_id=self.lead.id,
        template=transfer.template.name,
        template_id=str(transfer.template.id),
    )

    # 3. Swap the template trio — everything downstream reads these.
    self.template = transfer.template
    self.configurations = transfer.template.configurations
    self.template_vars = transfer.template_vars
    self._handoff_messages = transfer.handoff_messages

    # 4. Rebuild per-template inputs the same way setup does.
    self.flow_builder = FlowConfigBuilder()
    for name, fn in self.flow_builder.handler_map.items():
        self.flow_builder.handler_map[name] = with_context(self)(fn)
    self.vad_analyzer, self.default_vad_params = await create_vad_analyzer(
        is_daily_mode=self.is_daily_mode, template=self.template
    )

    # 5. FRESH transport over the SAME connection (constraint #3).
    transport_params = get_transport_params(self.template, self.configurations)
    if self.is_daily_mode:
        self.transport = await create_transport(self._runner_args, transport_params)
    else:
        params = transport_params[self._telephony_transport_type]()
        self.transport = await _create_telephony_transport(
            self._ws_for_transport, params,
            self._telephony_transport_type, self._telephony_call_data,
        )

    # 6. Reset per-generation state. NOTE: conversation_ended stays False;
    #    lead/agent_state/errors/conversation_id persist across generations.
    self.task = None
    self.context = None
    self.flow_manager = None
    self._context_aggregator = None
    self._rtvi_processor = None
    self.approval_manager = None
    self._user_idle_callback_handler = None
    self._flow_initialized = False
    self.greeting_source = None      # → respond_immediately=True for gen >= 2:
    self.greeting_text = None        #   the new agent speaks first
    if self._post_greeting_task and not self._post_greeting_task.done():
        self._post_greeting_task.cancel()
        self._post_greeting_task = None
    update_log_context(generation=str(self.generation))
```

What deliberately **persists** across generations: `self.lead` (+ metaData),
`self.agent_state` (reducer-built session state), `self.errors`,
`self.conversation_id` (one Langfuse identity per call; the per-generation root span
gets a `transfer_index`/`generation` attribute), `self.ws` / `self._ws_for_transport`,
`self.call_sid`/`stream_sid`, `completion_function`, `telephony_service`.

### 6.2 `NonClosingWebSocket` (`utils/transport/nonclosing.py`) — new

The single guarantee that the connection survives, independent of anything pipecat
does during teardown: pipecat's ref-counted `websocket.close()` becomes unreachable.

```python
"""Proxy that shields a live FastAPI WebSocket from pipecat transport teardown.

During an agent-to-agent transfer the old pipeline's transports call
websocket.close() as part of EndFrame teardown (ref-counted in
FastAPIWebsocketClient.disconnect). The Agent owns the real close instead:
this proxy forwards every attribute to the underlying websocket but turns
close() into a no-op. Final close goes through the RAW websocket
(close_websocket_safely(self.ws, ...)), never through the proxy.
"""

from fastapi import WebSocket

from app.core.logger import logger


class NonClosingWebSocket:
    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        logger.debug(
            f"NonClosingWebSocket: suppressed close(code={code}) — "
            "Agent owns the connection lifecycle"
        )

    def __getattr__(self, name):  # client_state, send_text, iter_text, ...
        return getattr(self._ws, name)
```

Wraps FastAPI/Starlette's API, not pipecat's → inherently pipecat-version-proof.
`__getattr__` forwarding covers `client_state`, `application_state`, `send_text`,
`send_bytes`, `receive`, `iter_text`, `query_params` — everything the pipecat client
and serializers touch.

### 6.3 `suppress_auto_hangup` + `PendingAgentTransfer` (`utils/agent_transfer.py`) — new

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger


@dataclass
class PendingAgentTransfer:
    template: TemplateModel                       # loaded + rendered + validated
    template_vars: Dict[str, str]
    handoff_messages: List[Dict[str, str]] = field(default_factory=list)


def suppress_auto_hangup(transport: Any) -> None:
    """Stop the telephony serializer from REST-hanging-up the call on EndFrame.

    Generalizes the warm_transfer Plivo trick (warm_transfer.py:319) to all
    providers. _hangup_attempted is a private pipecat attribute — this helper is
    the ONE deliberately version-coupled seam in the transfer feature (verified
    present and identical on pipecat 1.1.0 and master/1.5.0). Daily transports
    have no serializer; the hasattr guard makes this a no-op there.
    """
    try:
        serializer = transport.output()._params.serializer
        if serializer is not None and hasattr(serializer, "_hangup_attempted"):
            serializer._hangup_attempted = True
            logger.info("[agent_transfer] Suppressed serializer auto-hangup")
    except Exception as e:
        logger.warning(f"[agent_transfer] Could not suppress auto-hangup: {e}")
```

### 6.4 Template configuration (`template/types.py`)

Following the `transfer_number` / `hold_transfer` precedent: targets are declared in
template configuration, **not** free-typed by the LLM. The LLM picks a friendly alias;
the template pins which template id that alias means.

```python
class AgentTransferConfig(BaseModel):
    """Configuration for agent-to-agent transfer (connect_to_agent builtin)."""

    targets: Dict[str, str] = Field(
        ...,
        description="Alias -> target template id. The LLM selects an alias "
        "(e.g. 'sales'); the template controls which template it maps to.",
    )
    max_transfers: int = Field(
        5, ge=1, le=20,
        description="Max transfers per call — loop guard (A->B->A ping-pong).",
    )
    context_mode: Literal["summary", "full", "fresh"] = Field(
        "summary",
        description="What the next agent inherits: 'summary' = handoff note with "
        "the LLM-provided summary; 'full' = replay entire message history; "
        "'fresh' = nothing but a transfer marker.",
    )

# on ConfigurationModel:
agent_transfer: Optional[AgentTransferConfig] = None
```

### 6.5 The handler (`handlers/internal/agent_transfer.py`) — new

Signature matches every other builtin: `(context, args) -> Dict`. Registered in
`BUILTIN_HANDLERS`; the dispatcher already handles `pre_tts_message`
(speak-and-wait, STT muted) *before* invoking the handler — the announcement plays
fully before teardown begins, for free.

```python
async def connect_to_agent(context: TemplateContext, args: Dict[str, Any]) -> Dict:
    bot = context.bot

    # ── Guards: every failure returns an error result; the call continues. ──
    cfg = getattr(bot.configurations, "agent_transfer", None)
    if not cfg:
        return {"status": "error", "error": "agent_transfer not configured"}
    if bot.pending_transfer:
        return {"status": "error", "error": "transfer already in progress"}
    if bot.transfer_count >= cfg.max_transfers:
        return {"status": "error", "error": "max transfers reached for this call"}

    target_alias = args.get("target", "")
    target_template_id = cfg.targets.get(target_alias)
    if not target_template_id:
        return {"status": "error",
                "error": f"unknown target '{target_alias}'",
                "available_targets": list(cfg.targets.keys())}

    # ── Load + render + validate target BEFORE any teardown. ──
    # Same loader as call start: reseller scoping, {placeholder} resolution
    # from the SAME lead payload, playground overrides, compat validation.
    try:
        loader = FlowConfigLoader()
        template, template_vars = await loader.load_template(
            reseller_id=bot.lead.reseller_id,
            template=bot.lead.template,
            merchant_id=bot.lead.merchant_id,
            call_payload=bot.lead.payload,
            template_id=target_template_id,
        )
        validate_template_compat(template)
        if template.flow.get("mode") == FlowMode.IVR.value:
            return {"status": "error", "error": "cannot transfer to an IVR template"}
        if "voice" not in (template.supported_channels or ["voice"]):
            return {"status": "error", "error": "target does not support voice"}
    except Exception as e:
        logger.error(f"[agent_transfer] target load failed: {e}", exc_info=True)
        return {"status": "error", "error": f"target template unavailable: {e}"}

    # ── Build handoff messages per context_mode. ──
    handoff: List[Dict[str, str]] = []
    summary = (args.get("handoff_summary") or "").strip()
    if cfg.context_mode == "full" and bot.context:
        handoff = [m for m in bot.context.messages
                   if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                   and isinstance(m.get("content"), str)]
    if cfg.context_mode in ("summary", "full"):
        note = ("This call was just transferred to you from another agent."
                + (f" Handoff summary: {summary}" if summary else ""))
        handoff.append({"role": "system", "content": note})

    # ── Commit: from here the current pipeline is going down. ──
    bot.pending_transfer = PendingAgentTransfer(
        template=template, template_vars=template_vars, handoff_messages=handoff,
    )
    suppress_auto_hangup(bot.transport)
    logger.info(
        f"[agent_transfer] {bot.template.name} -> {template.name} "
        f"(call {context.call_sid}, generation {bot.generation})"
    )
    await bot.task.stop_when_done()   # EndFrame: drains, then stops the pipeline
    return {"status": "transferring", "target": template.name}
```

Notes:

- `stop_when_done()` queues `EndFrame` behind in-flight frames — any queued TTS
  completes first. Same in-handler teardown pattern `warm_transfer` already uses
  (it calls `end_conversation` which queues `EndFrame` from inside a handler).
- The **ordering is the reliability story**: nothing is torn down until the target
  template has fully loaded, rendered, and validated. A bad target = an error string
  the LLM can apologize with, on a still-healthy call.
- The `handoff_summary` arg lets the outgoing LLM write the context note — better
  than a mechanical transcript dump for `summary` mode.

### 6.6 Registry + chat exclusion

`handlers/internal/builtin_dispatcher.py`:

```python
from app.ai.voice.agents.breeze_buddy.handlers.internal.agent_transfer import (
    connect_to_agent,
)

BUILTIN_HANDLERS: Dict[str, Callable] = {
    "connect_to_agent": connect_to_agent,      # ← new
    "connect_to_live_agent": connect_to_live_agent,
    ...
}
```

`chat/disabled.py`: add `"connect_to_agent"` to `CHAT_DISABLED_NAMES` (voice-only in
v1 — chat agents are constructed-and-discarded per turn, so "transfer" there is just
changing the session's template id; a different, much smaller feature).

### 6.7 Finalize-once guards (`end_conversation.py`, disconnect handlers)

`end_conversation` runs exactly once, at real call end. Two small changes:

```python
# top of end_conversation(), next to the conversation_ended guard:
if getattr(context.bot, "pending_transfer", None):
    logger.info(f"Agent transfer in progress for {context.call_sid}; "
                "skipping finalization (defensive guard)")
    return {}
```

(Defensive only — self-initiated teardown doesn't fire `on_client_disconnected`
[constraint verification, §2], and the transfer path never calls `end_conversation`.
The realistic race it covers: the aggregator's idle timer firing in the same tick as
the transfer commit.)

Transcript merge — where transcription is collected from `context.context.messages`:

```python
prior = getattr(context.bot, "prior_generation_messages", [])
transcription = list(prior)          # generations 1..N-1
# ... existing loop appends the current (final) generation's messages ...
```

`lead.metaData` also carries `agent_transfers` (written by `_apply_transfer`), so the
final record shows the full journey; `update_lead_template` means the lead row ends
attributed to the final template — same convention IVR selection established.

### 6.8 Flow initialization for generation ≥ 2 (`_handle_client_connected`)

- **Telephony:** nothing to do. `FastAPIWebsocketInputTransport.start()` calls
  `trigger_client_connected()` **unconditionally** (`fastapi.py:261`) — every fresh
  transport fires `on_client_connected` when its pipeline starts, so the existing
  handler → `flow_manager.initialize(...)` path just runs again.
- **Daily:** `on_client_connected` is participant-join driven
  (`daily/transport.py:2834`). On a rebuild the participant is already in the room;
  daily-python generally re-delivers participant events on join, but don't bet the
  feature on it: after `_register_event_handlers()` in `_run_generation`, for
  `generation > 1`, call `await self._handle_client_connected()` directly.
  Frames queued by `flow_manager.initialize` sit in the task's queue until
  `runner.run()` starts (verified: `queue_frame` is a plain queue put,
  `task.py:570-585`).
- Make it race-proof for both paths with a per-generation flag:

```python
# top of _handle_client_connected():
if self._flow_initialized:
    return
self._flow_initialized = True
```

Handoff seeding — in `_handle_client_connected`, after `prepare_initial_node(...)`:

```python
if self._handoff_messages:
    initial_node_config["task_messages"] = (
        list(self._handoff_messages) + list(initial_node_config["task_messages"])
    )
    self._handoff_messages = []
```

(Or add an optional `handoff_messages` parameter to `prepare_initial_node` in
`agent/flow.py` — equivalent; pick whichever reads better in review.)

Since `greeting_source`/`greeting_text` are reset to `None` for generation ≥ 2,
`prepare_initial_node` yields `respond_immediately=True` → **the new agent speaks
first**, driven by its own initial node prompt plus the handoff note.

---

## 7. Template usage example

Template A (triage) declares the transfer function:

```json
{
  "flow": {
    "global_functions": [
      {
        "type": "builtin",
        "handler": "connect_to_agent",
        "name": "transfer_to_specialist",
        "description": "Transfer the caller to a specialist agent. Use 'sales' for pricing/purchase intents, 'support' for order issues. Always pass a one-paragraph handoff_summary of the conversation so far.",
        "pre_tts_message": "Sure — let me connect you to the right specialist. One moment.",
        "properties": {
          "target": {
            "type": "string",
            "enum": ["sales", "support"],
            "description": "Which specialist agent to transfer to"
          },
          "handoff_summary": {
            "type": "string",
            "description": "Summary of the caller's need and conversation so far"
          }
        },
        "required": ["target", "handoff_summary"]
      }
    ]
  },
  "configurations": {
    "agent_transfer": {
      "targets": {
        "sales":   "3f1c2d9a-…-template-uuid",
        "support": "8a4b7e21-…-template-uuid"
      },
      "max_transfers": 5,
      "context_mode": "summary"
    }
  }
}
```

The LLM can only ever reach template ids the template author allow-listed; the loader
re-applies reseller/merchant scoping on top.

---

## 8. Failure modes

| Scenario | Behavior |
|---|---|
| Target template missing / invalid / IVR / wrong channel | Error result to LLM **before** teardown; call continues on Agent A |
| Max transfers hit | Error result; call continues |
| Caller hangs up while Agent A drains (during EndFrame) | Serializer hangup suppressed but caller already gone; new transport's receive loop hits the closed ws immediately → `on_client_disconnected` → normal `end_conversation` (lead/metaData intact, transcripts merged) |
| Caller hangs up during the rebuild gap (~sub-second) | Same as above — the raw ws is closed by the provider; gen N+1 pipeline detects on start |
| Rebuild throws (service init fails for template B) | Bubbles into `run()`'s existing error handling; `finally` cleanup runs; DB finalization via disconnect/completion paths. Same blast radius as a service failure at call start |
| Transfer + idle-timeout race | `pending_transfer` guard in `end_conversation` (defensive); idle timers cancelled in `_apply_transfer` |
| A → B → A ping-pong | Allowed (legitimate: escalate & return), bounded by `max_transfers` |

**Testing (must-have):** integration test with two templates transferring to each
other over a mocked telephony websocket, asserting (a) the ws never receives
`close()`, (b) no provider hangup call is made, (c) Agent B's first LLM context
contains the handoff note, (d) final transcription contains both generations,
(e) `agent_transfers` metadata recorded. This test is also the **upgrade tripwire**:
any future pipecat bump that changes a teardown assumption fails here in CI, not on
a live call.

---

## 9. Scalability

- **No new infrastructure.** Everything happens inside the one process/websocket
  already handling the call — no Redis coordination, no cross-pod concerns (unlike
  `hold_and_consult`, which needs pub/sub across legs). Scales exactly as calls
  scale today.
- **No leaks.** Each generation's `PipelineTask`/`PipelineRunner`/services are
  dropped when the loop iterates and garbage-collected; nothing accumulates across
  generations except the (bounded) transcript snapshots and transfer history.
- **Provider connections:** strictly sequential (§4); transfers are invisible to
  concurrency limits.
- **Bounded:** `max_transfers` per call; targets allow-listed per template;
  reseller/merchant scoping enforced by the loader.

---

## 10. Pipecat version independence

The feature's pipecat coupling is a **strict subset of the coupling Clairvoyance
already has** — it touches no pipecat API the codebase doesn't already call today.
Any pipecat upgrade that would break transfer would have broken the agent anyway.

| Layer | Pipecat dependency |
|---|---|
| Generation loop | None — re-invokes our own `create_services`/`build_pipeline`/`create_pipeline_task`/`setup_flow_manager` |
| `connect_to_agent` handler | None — template loading/validation, snapshot, flag |
| `NonClosingWebSocket` | None — wraps FastAPI/Starlette, not pipecat |
| Finalize guards, transcript merge, handoff seeding | None |
| `task.stop_when_done()` / EndFrame semantics | Public API — identical 1.1.0 → 1.5.0 |
| Services connect in `start()` | Framework architectural invariant |
| `_create_telephony_transport(...)` | Private-prefixed but **already used in production** (`agent/__init__.py:661`); present with the same signature on master (`runner/utils.py:483` @1.5.0) |
| `serializer._hangup_attempted` | **The one private-API seam.** Already in production via `warm_transfer.py:319`; verified on 1.1.0 and master. Quarantined in `suppress_auto_hangup()` — one function to fix if ever renamed. Escape hatch: construct the serializer ourselves with public `auto_hang_up=False` (~15 lines mirroring the runner util) |

### 1.5.0 upgrade notes (separate track — do not fuse with this feature)

Most of Clairvoyance's ~60 pipecat imports survive 1.5.0 via deprecation shims.
Items that need focused attention when that upgrade happens (none affect this
feature's design):

- RTVI protocol → **2.0.0** in 1.4.0 (`bot-output` reshaped) — re-verify widget/Daily clients.
- `TTSSpeakFrame.append_to_context` default flipped to `True` — stream-mode `tts-speak` transcript accounting.
- `SonioxTTSService` emits word-aligned `TTSTextFrame`s; `LLMContextAggregatorPair.realtime_service_mode` behavior change.
- Renames to do proactively: `PipelineTask`→`PipelineWorker`, `PipelineRunner`→`WorkerRunner` (`pipecat.workers.runner`), drop external `pipecat-ai-flows` for core `pipecat.flows` (same `FlowManager` constructor; core warns if both installed).

---

## 11. Why not the alternatives

**Swap only the FlowManager on a fixed pipeline** — cheapest, but cannot change
STT/TTS/LLM/VAD, which templates are allowed to differ on. Silently breaks the
template contract.

**`ServiceSwitcher` / `ParallelPipeline`** (exists in 1.1.0) — requires
pre-instantiating every provider any target might use inside one pipeline, and (on
1.5.0, verified) **every branch starts and connects at StartFrame**. Built for
primary+failover pairs, not an arbitrary template universe. Worst case for
connection limits.

**`StopFrame`** — the one frame that ends a pipeline while "keeping connections
open" (`task.py:158`), but it does not re-arm the transports (`_initialized` stays
latched), so it solves only half the problem. The fresh-transport approach makes it
unnecessary.

**Pipecat 1.5.0 multi-worker handoff** — the framework-native long-term direction,
evaluated in depth against master. What it gives: bus-bridged child agents, atomic
`activate_worker(deactivate_self=True)` swaps, shared `LLMContext`, **dynamic
`add_workers()` after `run()`** (targets need not be known upfront). What it does
NOT give (verified):

- Deactivation sends **no** End/Cancel/Stop — an inactive child's TTS holds an open,
  actively-keepalived provider socket for the whole call (Cartesia auto-reconnect
  `cartesia/tts.py:690`; ElevenLabs keepalive `elevenlabs/tts.py:938`). The shipped
  "pre-instantiate all agents + deactivate" lifecycle multiplies provider
  connections per call — exactly our constraint. Usable only as
  spawn-on-transfer + **end**-on-leave.
- Per-template STT is not modeled: every example keeps STT on the main worker,
  shared across agents. Child-owned STT means bridging raw audio over the bus —
  no shipped example.
- No `remove_workers`; ended workers stay referenced by the runner until it tears
  down (bounded per call, a leak for a shared host runner). Worker names can't be
  reused.
- Clairvoyance's handler architecture (one pipeline, one FlowManager,
  `TemplateContext` reaching into `bot.task`) needs genuine re-architecture to map
  onto main-worker + child-workers.

**When to revisit workers:** after the 1.5.x upgrade lands, and if we want what
workers uniquely enable beyond transfer — agent-consults-agent while the caller
waits, parallel shadow agents/observers, distributed handoff across processes
(Redis/PGMQ bus). `connect_to_agent`'s contract (template global function →
validate → snapshot → switch) is unchanged in that world; only the loop's internals
would be replaced by `add_workers`/`activate_worker`/end-worker calls. Nothing in
this design is throwaway.

---

## 12. FAQ

**Q: Does the caller's phone connection really survive the pipeline teardown?**
Yes, by construction. The two things that end a call are (a) the serializer's REST
hangup on EndFrame — suppressed via the same `_hangup_attempted` flag warm transfer
uses in production — and (b) `websocket.close()` from the transport client — which
goes through `NonClosingWebSocket` and is a no-op. The Agent closes the raw
websocket exactly once, at true call end.

**Q: Won't STT/TTS provider connections double during a transfer?**
No. Provider sockets open in `start()` (StartFrame) and close on EndFrame — verified
for Soniox/ElevenLabs/Cartesia/Deepgram on 1.1.0. Agent A's sockets close before
Agent B's open. Pre-building B's services for validation creates config objects
only. One call = one set of provider connections, always.

**Q: What does the caller hear during a transfer?**
The optional `pre_tts_message` ("let me connect you…"), spoken fully with STT muted
(existing dispatcher behavior), then a sub-second pause (teardown + rebuild — target
template and services are prepared *before* teardown), then Agent B's opening line.

**Q: Is this coupled to a pipecat version / will the 1.5.0 upgrade break it?**
Effectively no — see §10. One private attribute (`_hangup_attempted`) is the only
non-public touch, already used in production, verified identical on master, and
isolated in a single helper. The integration test doubles as the upgrade tripwire.

**Q: Why not use pipecat's multi-worker handoff now?**
It doesn't exist on our pinned 1.1.0; on 1.5.0 its shipped lifecycle holds provider
connections for every pre-built agent (our exact constraint), per-template STT isn't
modeled, and adopting it means re-architecting the handler/FlowManager layer. The
generation loop delivers the feature now, with the same lazy-spawn/end-on-leave
lifecycle we'd want on workers later. See §11.

**Q: How does Agent B know what happened before the transfer?**
`context_mode` on `AgentTransferConfig`: `summary` (default — a system handoff note
carrying the outgoing LLM's `handoff_summary`), `full` (replay the entire
user/assistant history into B's context — the pattern chat mode already proves), or
`fresh` (transfer marker only).

**Q: What ends up in the lead record?**
One lead, one finalization. Merged transcription across all generations,
`agent_transfers` history in `metaData`, and the lead's `template`/`template_id`
updated to the final template (IVR-selection precedent). Outcome/callbacks fire once
at real call end.

**Q: Can B transfer back to A? Can transfers loop forever?**
Yes, and no — ping-pong is legitimate (escalate & return) and bounded by
`max_transfers` (default 5).

**Q: Does this work for Daily (web) calls?**
Yes, same loop. One caveat: the old transport's ref-counted `leave()` makes the bot
exit and rejoin the room (~1s of bot silence; the user never leaves). Telephony has
no such gap. Generation ≥ 2 flow init is triggered explicitly on Daily (§6.8).

**Q: What about chat / stream / IVR / realtime modes?**
Untouched. The builtin only exists on agent-mode voice pipelines (the only ones with
a FlowManager). `connect_to_agent` is added to `CHAT_DISABLED_NAMES`. Realtime-LLM
templates can be transfer *targets* (build_pipeline's realtime branch runs in the
loop like any other generation).

**Q: Why is the LLM given aliases instead of template ids?**
Security and authoring ergonomics: the template allow-lists `alias → template_id`
under `configurations.agent_transfer.targets`; the loader re-applies
reseller/merchant scoping. The LLM can never reach an arbitrary template.

**Q: Langfuse/tracing?**
`conversation_id` is stable across the call; each generation gets its own root span
(created per `runner.run` in `_run_with_tracing`) tagged with the generation index.
Evaluators see the final merged outcome at end_conversation as today.

**Q: Rollout?**
Ship behind a dynamic config flag (`core/config/dynamic.py`, DevCycle) gating the
`connect_to_agent` registry entry; templates opt in explicitly via
`agent_transfer` config regardless. Start with Twilio, then Plivo/Exotel
(suppression helper is provider-generic), then Daily.
