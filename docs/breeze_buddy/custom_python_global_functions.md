# Custom Python Global Functions — Design & Implementation Plan

Status: **Proposed (v1)** · Owner: TBD · Last updated: 2026-04-28

## 1. Goal

Today, Breeze Buddy exposes two kinds of LLM-callable global functions in templates:

- `http` — call an external HTTP/SSE endpoint and return the response to the LLM.
- `builtin` — invoke a registered internal handler (warm transfer, get current time, …).

We want a third kind: **`custom`** — a snippet of Python code, written by a developer and stored *inside the template JSON*, that the agent compiles at startup and exposes to the LLM as a callable function (same shape as MCP tools: name + description + JSON-schema args). At call time the LLM picks args, the agent executes the compiled code, and the return value is sent back to the LLM.

Use cases:
- Merchant-specific lookup tables / business rules that are too verbose to put in the LLM prompt (e.g. discount tier from order count, SLA window from product SKU).
- Light data transforms (formatting, filtering, deriving values from lead variables).
- Future: simple network calls written in Python instead of declaring a full `http_request` config.

## 2. Why this fits the existing architecture

The current global-function machinery is already adapter-based (`app/ai/voice/agents/breeze_buddy/template/global_function.py`). The enum even reserves the slot:

```python
class GlobalFunctionType(str, Enum):
    HTTP = "http"
    BUILTIN = "builtin"
    CUSTOM = "custom"  # Future: custom Python function handlers
```

Adding a third adapter alongside `HttpGlobalFunctionAdapter` and `BuiltinGlobalFunctionAdapter` is the natural seam. Filler audio, background music, post-actions, the `with_context` injection, and the `FlowsFunctionSchema` plumbing all work for free. We touch four files and add two.

## 3. Scope

### In scope (v1)
- New `GlobalCustomFunction` Pydantic model + `custom` adapter.
- Compile-on-template-build, run-on-LLM-call.
- Sync and async user code (auto-detected via `inspect.iscoroutinefunction`).
- Per-call wall-time timeout (default 5s, configurable via `timeout_seconds`).
- Standard wrapped response shape (`{"status", "data"}` or `{"status", "error"}`) — same envelope as `http_function_handler`.
- User code receives `args: dict` (LLM call args) and `context: dict` (read-only lead payload + a couple of lead identifiers). Nothing else.
- Compile failures: log a warning and skip *that one* function. Other functions and the call proceed normally.
- Runtime failures: caught, logged, returned to LLM as `{"status": "error", "error": "<ExcClass>: <message>"}`.

### Out of scope (v1)
- Sandboxing / RestrictedPython / AST allowlist / import blocklist — code is **dev-authored and trusted** for now. (See §9 for the basic guardrails we *do* keep.)
- Multi-file code, importing other custom functions from the template.
- Persistent state across calls (each invocation gets a fresh execution).
- Custom functions calling other global functions.
- Merchant-facing surfacing of compile/runtime errors (Slack, DB column on template). Logs only.
- Custom entrypoint name (always `handler`). UI can validate this; we add a comment for later.

## 4. Template schema

A `custom` function is declared in the same `flow.global_functions` array as `http`/`builtin`:

```json
{
  "type": "custom",
  "name": "calculate_discount_tier",
  "description": "Returns the discount tier (gold/silver/bronze) for a customer based on their lifetime order count",
  "properties": {
    "order_count": {"type": "integer", "description": "Total completed orders"}
  },
  "required": ["order_count"],
  "timeout_seconds": 5,
  "python_code": "def handler(args, context):\n    n = args['order_count']\n    if n > 50:\n        return {'tier': 'gold', 'discount_pct': 20}\n    if n > 10:\n        return {'tier': 'silver', 'discount_pct': 10}\n    return {'tier': 'bronze', 'discount_pct': 5}"
}
```

Inherited from `BaseGlobalFunction` (no extra wiring needed):
- `filler_audio` — TTS phrase + background music while the function runs.
- `pre_actions` / `post_actions` — fire-and-forget side effects.
- `name`, `description`, `properties`, `required` — LLM-facing schema.

New fields specific to `custom`:

| Field             | Type            | Required | Default | Notes |
| ----------------- | --------------- | -------- | ------- | ----- |
| `python_code`     | `str`           | yes      | —       | Source code. Must define `def handler(args, context)` or `async def handler(args, context)`. |
| `timeout_seconds` | `int`           | no       | `5`     | Wall-time limit. Bounded `[1, 30]`. Validation error outside that range. |

### Entrypoint contract

User code **must** define a top-level callable named `handler` taking exactly two positional args:

```python
def handler(args: dict, context: dict) -> Any:
    ...

# or
async def handler(args: dict, context: dict) -> Any:
    ...
```

- `args` — the dict the LLM passed for this function call (validated against `properties`/`required` upstream by Pipecat).
- `context` — a small read-only dict. **v1 fields:**
  - `lead`: the lead payload as a `dict` (rendered template variables — already available on `bot.lead`).
  - `call_sid`: current call SID (string, may be `None` for web).
  - `lead_id`: integer.
  - *(comment in code: "expand this with more context surface as new use cases emerge — e.g. transcripts, node history, merchant config")*

Return value: any JSON-serialisable Python value (`dict`, `list`, `str`, `int`, `float`, `bool`, `None`). The handler **wraps** it as:

```python
{"status": "success", "data": <return_value>}
```

This matches the envelope `http_function_handler` produces today, so downstream LLM-prompt formatting is identical.

## 5. Files to add

### 5.1 `app/ai/voice/agents/breeze_buddy/handlers/internal/custom_python_handler.py` (NEW)

The runtime handler. Follows the same signature shape as `http_function_handler` and `builtin_function_dispatcher`.

Responsibilities:
1. Pull the pre-compiled callable off `function_config` (attached during build — see §6).
2. Build the read-only `context` dict from `TemplateContext`.
3. `await` the callable with a timeout (`asyncio.wait_for`).
   - If `iscoroutinefunction(handler)` → `await handler(args, ctx)`.
   - Else → `await asyncio.to_thread(handler, args, ctx)` so a CPU-bound or sync-blocking call doesn't block the event loop.
4. Wrap the return as `{"status": "success", "data": <return>}`.
5. On `asyncio.TimeoutError` → `{"status": "error", "error": "Custom function timed out after Ns"}`.
6. On any other `Exception` → `{"status": "error", "error": f"{type(e).__name__}: {e}"}` (logged with `exc_info=True`).
7. Always returns `(result_dict, None)` to match the global-function tuple contract.

```python
# Sketch — full version in the implementation PR.
async def custom_python_handler(
    context: TemplateContext,
    args: Dict[str, Any],
    function_config: Optional[GlobalCustomFunction] = None,
) -> Tuple[Dict[str, Any], None]:
    if function_config is None or function_config.compiled_handler is None:
        return {"status": "error", "error": "Custom function not compiled"}, None

    user_ctx = {
        "lead": context.lead.model_dump() if context.lead else {},
        "call_sid": context.call_sid,
        "lead_id": getattr(context.lead, "id", None) if context.lead else None,
        # NOTE: keep this surface minimal in v1. Expand only as concrete need arises.
    }

    handler_fn = function_config.compiled_handler
    timeout = function_config.timeout_seconds

    try:
        if inspect.iscoroutinefunction(handler_fn):
            result = await asyncio.wait_for(handler_fn(args, user_ctx), timeout=timeout)
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(handler_fn, args, user_ctx), timeout=timeout
            )
        return {"status": "success", "data": result}, None
    except asyncio.TimeoutError:
        logger.warning(f"[{function_config.name}] timed out after {timeout}s")
        return {"status": "error", "error": f"Function timed out after {timeout}s"}, None
    except Exception as e:
        logger.error(f"[{function_config.name}] runtime error: {e}", exc_info=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}, None
```

> *Note*: lives under `handlers/internal/` rather than `handlers/transport/` because nothing here is networked at the *handler* level — the user's code might do its own I/O, but the handler itself is local execution.

### 5.2 `app/ai/voice/agents/breeze_buddy/template/custom_function_compiler.py` (NEW)

Tiny module owning the `compile()` + `exec()` step, isolated for testability.

```python
def compile_custom_function(name: str, source: str) -> Optional[Callable]:
    """
    Compile python_code source into a callable.

    Returns the `handler` callable on success, None on any failure
    (logs a warning). Never raises.
    """
    try:
        code_obj = compile(source, f"<custom_function:{name}>", "exec")
        namespace: Dict[str, Any] = {}
        exec(code_obj, namespace)  # noqa: S102 — trusted, dev-authored code
        handler = namespace.get("handler")
        if not callable(handler):
            logger.warning(
                f"[custom function '{name}'] python_code does not define a "
                f"top-level `handler` callable — skipping."
            )
            return None
        return handler
    except SyntaxError as e:
        logger.warning(f"[custom function '{name}'] syntax error: {e} — skipping.")
        return None
    except Exception as e:
        logger.warning(
            f"[custom function '{name}'] compilation failed: {e} — skipping.",
            exc_info=True,
        )
        return None
```

> *Comment in code*: `# Future: support custom entrypoint name (e.g. function_config.entrypoint), AST-allowlist sandboxing, import blocklist, and a Slack/DB error sink for merchant-facing surfacing.`

## 6. Files to modify

### 6.1 `app/ai/voice/agents/breeze_buddy/template/types.py`

Add `GlobalCustomFunction` next to `GlobalHttpFunction` and `GlobalBuiltinFunction`:

```python
class GlobalCustomFunction(BaseGlobalFunction):
    """
    Configuration for a custom Python global function available across all nodes.

    The python_code string is compiled at template-build time (per call) and the
    resulting callable is invoked when the LLM calls this function. Handler entry
    point must be a top-level `handler(args, context)` (sync or async).

    The compiled handler is attached to `compiled_handler` after build and is not
    serialised back out (excluded via Field(exclude=True)).
    """

    type: GlobalFunctionType = GlobalFunctionType.CUSTOM
    python_code: str = Field(..., description="Python source. Must define `handler(args, context)`.")
    timeout_seconds: int = Field(
        5,
        ge=1,
        le=30,
        description="Wall-time limit per invocation (seconds). 1-30. Default 5.",
    )

    # Populated by the adapter after successful compile. Excluded from
    # model_dump so we never accidentally round-trip a compiled object back to JSON.
    compiled_handler: Optional[Any] = Field(default=None, exclude=True, repr=False)

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)
```

> Why store `compiled_handler` on the model instead of in a separate dict? Symmetry with how `function_config` is already passed through `with_context` to the handler — the handler already receives the validated config object, so the compiled callable rides along on it.

### 6.2 `app/ai/voice/agents/breeze_buddy/template/global_function.py`

Add a third adapter and register it. The pattern mirrors the existing two adapters exactly — wrapper that runs filler audio, awaits the handler, stops music, fires post-actions.

```python
class CustomPythonGlobalFunctionAdapter:
    @property
    def function_type(self) -> GlobalFunctionType:
        return GlobalFunctionType.CUSTOM

    @property
    def handler_name(self) -> str:
        return "custom_python_handler"

    def can_handle(self, config: Dict[str, Any]) -> bool:
        return config.get("type") == GlobalFunctionType.CUSTOM.value

    def build_schema(
        self,
        config: Dict[str, Any],
        wrapped_handler: Callable,
        bot_instance: Any = None,
    ) -> Optional[FlowsFunctionSchema]:
        func = GlobalCustomFunction.model_validate(config)

        compiled = compile_custom_function(func.name, func.python_code)
        if compiled is None:
            # Compile failed -> silently drop this function (already logged).
            # Other global functions and the call itself proceed normally.
            return None
        func.compiled_handler = compiled

        # ... description suffix + create_wrapper(captured_func, captured_bot_instance)
        # identical to HttpGlobalFunctionAdapter / BuiltinGlobalFunctionAdapter.
```

Important: when `build_schema` returns `None`, `_build_one` already handles that case (look at the existing code path for "build failed" — it logs and returns `None`, the registry skips it). So returning `None` on compile failure is the correct silent-skip behaviour without any change to the registry.

Wait — current `_build_one` only returns `None` *inside the try/except* on `Exception`. `Optional` return on the protocol's `build_schema` isn't matched there. **Concrete change required**: update the `GlobalFunctionAdapter` protocol's `build_schema` return type to `Optional[FlowsFunctionSchema]`, and have `_build_one` skip `None` returns the same way it currently skips no-adapter-found.

Register at module bottom:

```python
GlobalFunctionRegistry.register("http", HttpGlobalFunctionAdapter())
GlobalFunctionRegistry.register("builtin", BuiltinGlobalFunctionAdapter())
GlobalFunctionRegistry.register("custom", CustomPythonGlobalFunctionAdapter())  # NEW
```

### 6.3 `app/ai/voice/agents/breeze_buddy/template/builder.py`

Add the new handler to `handler_map`:

```python
from app.ai.voice.agents.breeze_buddy.handlers.internal.custom_python_handler import (
    custom_python_handler,
)

# In FlowConfigBuilder.__init__:
self.handler_map = {
    ...
    "http_function_handler": http_function_handler,
    "builtin_function_dispatcher": builtin_function_dispatcher,
    "custom_python_handler": custom_python_handler,  # NEW
}
```

### 6.4 `app/ai/voice/agents/breeze_buddy/handlers/internal/__init__.py`

Export `custom_python_handler` so `builder.py` import works.

## 7. Lifecycle

```
Lead arrives  ──►  CallsManager spawns voice-agent subprocess (process pool)
                      │
                      ▼
              Template loaded from DB
                      │
                      ▼
       FlowConfigBuilder.build_global_functions()
                      │
                      ▼
       GlobalFunctionRegistry.build()
         - For each global_function entry:
             - Pick adapter via can_handle()
             - For "custom" adapter:
                 1. Validate into GlobalCustomFunction
                 2. compile_custom_function(name, python_code)
                    - SUCCESS → attach compiled_handler to func, build FlowsFunctionSchema
                    - FAILURE → log warning, return None → registry skips this function
             - Other functions in the template are unaffected
                      │
                      ▼
       FlowManager registers surviving functions with the LLM
                      │
                      ▼
       Conversation runs. LLM picks tool call.
                      │
                      ▼
       Pipecat invokes wrapper → with_context → custom_python_handler
                      │
                      ▼
       custom_python_handler:
         - Build read-only `context` dict
         - asyncio.wait_for(<sync-via-thread or async coroutine>, timeout=N)
         - Wrap result → {"status":"success","data":...}
                      │
                      ▼
       Result returned to LLM. Filler audio stops. Post-actions fire.
```

### Cache scope (Q9 from clarifications)

**Decision: per-call (per-subprocess) compile, no cross-call cache.**

Rationale:
- Each call already runs in its own subprocess (process pool pre-warmed at startup, but each call gets a fresh pipeline build). Compile is a one-shot cost per call.
- `compile() + exec()` of a small Python snippet is sub-millisecond — well below noise on the existing 5–6s warmup we're already amortising via the pool.
- Avoids any cache-invalidation question when the merchant edits the template (fresh compile = fresh code, always).
- We can revisit if profiling later shows compile time matters.

## 8. Response envelope (consistency)

For symmetry with `http_function_handler` (the LLM-facing contract the LLM already understands):

| Outcome           | Envelope                                                                  |
| ----------------- | ------------------------------------------------------------------------- |
| Success           | `{"status": "success", "data": <user return value>}`                      |
| User code raises  | `{"status": "error", "error": "<ExcClass>: <message>"}`                   |
| Timeout           | `{"status": "error", "error": "Function timed out after Ns"}`             |
| Not compiled      | `{"status": "error", "error": "Custom function not compiled"}`            |

No `status_code` field (only meaningful for HTTP). The LLM-prompt formatter is already tolerant of missing fields.

## 9. Security guardrails (basic, v1)

Per the trust model (dev-authored code, trusted authors), we are deliberately *not* sandboxing in v1. We do keep these basic guardrails:

1. **Wall-time timeout** (default 5s, max 30s) via `asyncio.wait_for`. Prevents a runaway loop from freezing the conversation. *Caveat*: for sync code running inside `asyncio.to_thread`, the `wait_for` returns control to the event loop on timeout, but the thread itself keeps running until completion — Python can't kill threads. Acceptable for v1; document as a known limitation.
2. **Off-event-loop execution** for sync code via `asyncio.to_thread` — a `time.sleep(60)` in user code won't block other agent activity (audio, transcription).
3. **Exception isolation** — runtime errors are caught and turned into a structured response; they never propagate up and tear down the pipeline.
4. **Filename-tagged compile** — `compile(source, "<custom_function:NAME>", "exec")` so tracebacks point at the right function.
5. **Compile failure ≠ template failure** — one bad function doesn't take down the call.

> Comment in code: `# Future hardening: AST allowlist (no __import__ of os/subprocess), restricted builtins, separate process per call, Slack/DB error sink for merchants.`

## 10. Testing

- **Unit**:
  - `compile_custom_function` — happy path, syntax error, missing `handler`, non-callable `handler`, raises during exec.
  - `custom_python_handler` — sync handler, async handler, return non-JSON-serialisable (document as user error), timeout, raises.
- **Integration** (a single end-to-end test against a fixture template):
  - Template with one valid + one syntactically broken custom function — agent boots, only the valid one is registered.
  - LLM-style call invokes the function with mock args, asserts the wrapped envelope reaches the LLM context.
- No DB migration is needed (templates are JSON, new fields are additive).

## 11. Observability

- Compile attempts: `INFO` line with function name; `WARNING` with reason on failure.
- Each invocation: `INFO` "starting custom function 'X'" / "completed in Yms".
- Timeouts and runtime errors: `WARNING`/`ERROR` respectively with `exc_info=True`.
- All logs use the existing `[function_name]` prefix convention (matching `http_function_handler` style) so per-call grep-by-function-name works out of the box.

No new OTEL spans in v1 — the existing function-call span from Pipecat already covers wall-time. Future: add a child span when we want to attribute time-spent inside the user code vs inside our wrapper.

## 12. Open questions / decisions deferred

- **Future entrypoint name**: keep `handler` as the only supported name in v1. UI can validate. Add a comment near `compile_custom_function` flagging "future: read `function_config.entrypoint` if present".
- **Future merchant-facing error sink**: today, errors only go to logs. Comment in `custom_python_handler` and `compile_custom_function` flagging "future: emit to Slack channel / write to a `template.last_compile_errors` JSONB column for merchant UI to surface".
- **Future context expansion**: the `context` dict starts intentionally tiny (`lead`, `call_sid`, `lead_id`). Comment near its construction in `custom_python_handler`: "expand only as concrete needs emerge — keep this an explicit read-only contract, not a leaky `TemplateContext`".

## 13. Summary of changes

| File                                                                                          | Change      |
| --------------------------------------------------------------------------------------------- | ----------- |
| `app/ai/voice/agents/breeze_buddy/template/types.py`                                          | + `GlobalCustomFunction` model                          |
| `app/ai/voice/agents/breeze_buddy/template/global_function.py`                                | + `CustomPythonGlobalFunctionAdapter`, register it; relax adapter protocol return type to `Optional[FlowsFunctionSchema]` |
| `app/ai/voice/agents/breeze_buddy/template/custom_function_compiler.py`                       | **NEW** — `compile_custom_function()` |
| `app/ai/voice/agents/breeze_buddy/handlers/internal/custom_python_handler.py`                 | **NEW** — `custom_python_handler()`   |
| `app/ai/voice/agents/breeze_buddy/handlers/internal/__init__.py`                              | export new handler                    |
| `app/ai/voice/agents/breeze_buddy/template/builder.py`                                        | wire `custom_python_handler` into `handler_map` |

No DB migrations. No changes to FlowManager, agent init, pipeline.py, or LLM-response shaping. The adapter pattern absorbs the new function type cleanly.
