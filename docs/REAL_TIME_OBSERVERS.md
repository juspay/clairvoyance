# Real-Time Observers

Side-LLMs that watch the conversation in parallel and detect problems (voicemail, hallucination, abuse).

## Architecture

```text
                        EXISTING PIPELINE (unchanged)
                        =============================

transport → STT → gate → user_aggregator → LLM (GPT-4o) → TTS → transport
                                |
                          LLMContext
                         (stores full conversation
                          including tool_calls)
                                |
                    +-----------+-----------+
                    |                       |
                    |  OBSERVER SYSTEM      |
                    |  ================     |
                    |                       |
                    |  configurable events  |
                    |        event          |
                    |           |           |
                    |           v           |
                    |   +------------------+|
                    |   | ObserverManager   |
                    |   |                   |
                    |   | turn_count++      |
                    |   | read LLMContext   |
                    |   | build transcript  |
                    |   | run N observers   |
                    |   +-------+----------+|
                    |           |               |            |
                    |     Observer 1       Observer 2        |
                    |    (voicemail)     (hallucination)     |
                    |     gpt-4o-mini    gpt-4o-mini         |
                    |           |               |            |
                    |    tool called?     tool called?       |
                    |         |                              |
                    |         v                              |
                    |  execute_action()                      |
                    |  → set outcome, call handler           |
                    |  → pipeline terminates                 |
                    |  → call hangs up                       |
                    +----------------------------------------+
```

## Code Flow

```text
CALL STARTS
│
├── agent/__init__.py: run()
│   │
│   ├── build_pipeline()                          # existing, unchanged
│   │   └── returns: pipeline, context (LLMContext), speech_gate, ...
│   │
│   ├── setup_flow_manager()                      # existing, unchanged
│   │   └── returns: flow_manager with handler_map
│   │
│   ├── ── OBSERVER SETUP (new) ──────────────
│   │   │
│   │   ├── self.configurations.observers         # read from template JSON
│   │   │   └── [{name, enabled, system_prompt, trigger_on, start_after_turn, llm, action}, ...]
│   │   │
│   │   ├── factory.build_observers()
│   │   │   │
│   │   │   ├── for each observer config:
│   │   │   │   ├── merge_llm_config()            # inherit template LLM + override
│   │   │   │   ├── get_llm_service(pooled=True)  # existing factory, reused
│   │   │   │   └── RealtimeObserver(config, llm_service, agent, handler_map)
│   │   │   │
│   │   │   └── returns: [observer1, observer2, ...]
│   │   │
│   │   └── ObserverManager(observers, LLMContext)
│   │
│   ├── _register_event_handlers()
│   │   │
│   │   ├── @configurable events                 # existing event
│   │   │   └── observer_manager.on_event(event_name)    # NEW: 1 line added
│   │   │
│   │
│   └── runner.run(task)                          # existing, unchanged
│
│
TURN HAPPENS (customer speaks, LLM responds)
│
├── configurable events fires
│   └── ObserverManager.on_event(event_name)
│       │
│       ├── turn_count += 1
│       └── asyncio.create_task(_run_checks())    # background, non-blocking
│
│
_run_checks() RUNS IN BACKGROUND
│
├── _build_transcript()
│   ├── reads LLMContext.messages                 # [user] customer speech
│   │                                             # [assistant] bot responses
│   └── appends function_calls                    # [bot_action] confirm_order()
│
├── filter eligible observers
│   └── turn_count >= obs.start_after_turn?
│
├── asyncio.gather(*[obs.check(transcript)])      # ALL in parallel
│   │
│   │   ┌── Observer 1: voicemail_detector ──────────────────┐
│   │   │   LLMContext()                                      │
│   │   │   context.set_tools([end_conversation])             │
│   │   │   context.set_tool_choice("required")               │
│   │   │   build_chat_completion_params + _client.create()   │
│   │   │   → gpt-4o-mini calls configured action tool        │
│   │   │   → tool called → detection → True                   │
│   │   └─────────────────────────────────────────────────────┘
│   │
│   │   ┌── Observer 2: hallucination_detector ──────────────┐
│   │   │   (same flow, different system_prompt)              │
│   │   │   → gpt-4o-mini calls:                              │
│   │   │     report_detection(detected=false)                │
│   │   │   → return False                                    │
│   │   └─────────────────────────────────────────────────────┘
│   │
│   └── results = [True, False]
│
├── zip(eligible, results) → first True wins
│   └── observer1 detected!
│
└── observer1.execute_action()
    │
    ├── lead.outcome = "VOICEMAIL"
    ├── lead.metaData["observer_triggered"] = "voicemail_detector"
    │
    └── handler_map["end_conversation"](args)      # REUSES existing handler
        │
        ├── collects transcript
        ├── updates DB
        ├── runs service_callback
        └── task.queue_frame(EndFrame)
            └── pipeline terminates → call hangs up


CALL ENDS
│
├── observer_manager.stop()
└── clear_log_context()
```

## Sequence Diagram: Voicemail Detection Call

```text
  Phone        Plivo       Pipeline        LLM          Observer
  (Customer)   (Provider)  (STT→Gate→Agg)  (GPT-4o)     Manager
    |            |            |              |              |
    |  call rings|            |              |              |
    |<-----------|            |              |              |
    |            |            |              |              |
    | voicemail  |  audio     |              |              |
    | answers    | stream     |              |              |
    |----------->|----------->|              |              |
    |            |            |              |              |
    |            |       STT transcribes     |              |
    |            |       gate passes (after  |              |
    |            |       4s mute expires)    |              |
    |            |            |              |              |
    |            |       aggregator collects |              |
    |            |       detects turn end    |              |
    |            |            |              |              |
    |            |            |--user msg--->|              |
    |            |            | added to     |              |
    |            |            | LLMContext   |              |
    |            |            |              |              |
    |            |       configurable events|              |
    |            |       event fires --------+------------->|
    |            |            |              |              |
    |            |            |              |     turn_count++ = 1
    |            |            |              |     asyncio.create_task(
    |            |            |              |       _run_checks()
    |            |            |              |     )
    |            |            |              |              |
    |            |            |              |     _build_transcript()
    |            |            |              |     reads LLMContext:
    |            |            |              |       [bot] "Namaste..."
    |            |            |              |       [user] "Tone. Please
    |            |            |              |        record your message"
    |            |            |              |              |
    |            |            |              |     voicemail_detector:
    |            |            |              |       start_after_turn=0
    |            |            |              |       turn 1 >= 0 ✅ eligible
    |            |            |              |              |
    |            |            |              |     hallucination_detector:
    |            |            |              |       start_after_turn=2
    |            |            |              |       turn 1 >= 2 ❌ skip
    |            |            |              |              |
    |            |            |              |     asyncio.gather(
    |            |            |              |       voicemail.check()
    |            |            |              |     )
    |            |            |              |              |
    |            |            |              |     voicemail.check():
    |            |            |              |       LLMContext()
    |            |            |              |       set_tools([report_detection])
    |            |            |              |       build_params + _client.create()
    |            |            |              |              |
    |            |            |              |     gpt-4o-mini calls:
    |            |            |              |       report_detection(detected=true)
    |            |            |              |              |
    |            |            |              |     execute_action():
    |            |            |              |       lead.outcome = "VOICEMAIL"
    |            |            |              |       lead.metaData[
    |            |            |              |         "observer_triggered"
    |            |            |              |       ] = "voicemail_detector"
    |            |            |              |              |
    |            |            |              |     handler_map[
    |            |            |              |       "end_conversation"
    |            |            |              |     ](args)
    |            |            |              |              |
    |            |            |  end_conversation handler:  |
    |            |            |    collect transcript       |
    |            |            |    update DB (VOICEMAIL)    |
    |            |            |    run service_callback     |
    |            |            |    queue EndFrame           |
    |            |            |              |              |
    |            |  pipeline  |              |              |
    |            |  terminates|              |              |
    |            |            |              |              |
    |  call      |            |              |              |
    |  hangs up  |            |              |              |
    |<-----------|            |              |              |

TOTAL: ~9 seconds. Outcome: VOICEMAIL. Observer: voicemail_detector.
```

## Files

```text
observers/
├── __init__.py          # exports
├── observer.py          # RealtimeObserver — one side-LLM
│                        #   check(): function calling via report_detection tool
│                        #   provider dispatch (OpenAI/Azure, Anthropic, Google)
│                        #   execute_action(): calls handler_map
├── manager.py           # ObserverManager — coordinates N observers
│                        #   on_event(event_name): triggered by pipeline event
│                        #   _run_checks(): parallel via asyncio.gather
│                        #   _build_transcript(): reads LLMContext
└── factory.py           # build_observers(): creates instances
                         #   merge_llm_config(): inherit with override
                         #   get_llm_service(): reuses existing factory

Modified:
  template/types.py      # +ObserverConfig (reuses LLMConfiguration, FlowAction)
  agent/__init__.py       # +observer setup, +event hooks, +cleanup
```

## Template Config

```json
"observers": [
  {
    "name": "voicemail_detector",
    "enabled": true,
    "system_prompt": "Analyze the call transcript for signs of voicemail...",
    "trigger_on": ["on_user_turn_message_added", "on_assistant_turn_stopped"],
    "start_after_turn": 0,
    "action": {
      "type": "function",
      "handler": "end_conversation",
      "args": { "outcome": "VOICEMAIL" }
    }
  }
]
```

- **enabled**: Optional switch for keeping an observer configured but not active. Defaults to `true`.
- **system_prompt**: Detection instructions for the observer LLM. The LLM receives one generated tool derived from `action` and calls it when it detects something.
- **trigger_on**: Pipecat events that trigger this observer. Supported: `on_user_turn_message_added`, `on_user_turn_stopped`, `on_assistant_turn_stopped`, `on_function_calls_started`. Default: `["on_user_turn_message_added"]`.
- **start_after_turn**: Skip first N events (0 = check from first event).
- **llm**: Optional LLM override. Omit to inherit from the template LLM configuration.
- **action**: Reuses FlowAction. For `type: "function"`, the observer tool name is `action.handler`; for non-function actions such as `alert`, the tool name is the action type string. The generated detection tool currently has no custom arguments.

Add any observer by adding to this array. No code changes needed.
