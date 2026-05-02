# Blueprint — Template Creation Agent

Multi-agent text agent at `app/ai/text/agents/blueprint/` that turns chat input into a validated Breeze Buddy template (`ReplaceTemplateRequest`). Frontend lives at the `templates/blueprint/` route in Loom; transport is SSE. Handles both **create** mode (empty seed) and **edit** mode (seeded with an existing template).

The companion playbook — `docs/blueprint/TEMPLATE_PERFECT_PLAYBOOK.md` — describes what a *correct* output looks like (ordered build steps, field reference, silent-breakage checklist consumed by `specialists/template_linter.py`).

---

## 1. Architecture

**One LLM call per user turn.** A single tick handler (`agent/turn.py:run_turn`) builds context, calls the turn LLM with structured output, applies the result, and returns. There is no separate planner / dispatcher / extractor — the LLM is the planner.

Per-turn input the LLM sees:

* A compact **schema view** built from `breeze_buddy/template/types.py` + enrichment from `blueprint_field_enrichment.yaml` (groups, fields, recommendations, alternatives, couplings, conditional skips).
* The current **draft** (partial template as built so far).
* **Completed** + **skipped** group lists (skipped = user explicitly opted out, distinct from auto-skips driven by structural rules).
* The most recent transcript window (last ~30 turns).
* Outstanding `validation_issues` from the validator + template linter.
* The session-fixed `BlueprintContext` (mode, reseller_id, existing_template_id, available_outbound_numbers).

Per-turn output (`TurnDecision`, `agent/turn_schema.py`):

```python
class TurnDecision(BaseModel):
    message_to_user: str
    draft_patch: dict[str, Any]            # values to deep-merge into draft
    completed_groups: list[str]            # newly-done askable groups
    skipped_groups: list[str]              # newly-opted-out askable groups
    pending_approval_for: Optional[str]    # group name for UI approval bar
    request_specialist: Optional[str]      # validator | template_linter
    finalize: bool                         # round-trip draft through Pydantic
    terminal: bool                         # session done
```

`completed_groups` / `skipped_groups` / `pending_approval_for` are dynamically enum-constrained to valid group names so the LLM cannot hallucinate.

### LangGraph wiring

Two nodes per session, conditional routing:

```
START -> tick -> { await_approval | END }
                       |
                       v
                      END
```

* **`tick`** (`agent/graph.py:_tick`) → calls `run_turn`. Writes draft, completed/skipped groups, validation issues, template_json, and the approval flag.
* **`await_approval`** (`agent/turn.py:await_approval`) → calls `langgraph.types.interrupt` when `pending_approval_for` is set. State writes from `tick` are already committed by then, so chat UIs that poll `aget_state` see the flag while SDK consumers can `Command(resume=...)` to continue.
* **`route_after_turn`** (`agent/turn.py`) decides between `await_approval` and `END`.

Session state is persisted by `langgraph.checkpoint.postgres.AsyncPostgresSaver` (`agent/checkpointer.py`) on a dedicated psycopg3 pool, falling back to `MemorySaver` when Postgres env vars are missing (dev). The `BlueprintContext` lives outside `BlueprintState` so session-fixed fields don't bloat every checkpoint snapshot.

### Specialists

Pure functions invoked by the turn handler — no LLM, no I/O:

* **`specialists/validator.py`** — coupling-rule validator. Runs every tick before the LLM call so the LLM sees current issues.
* **`specialists/template_linter.py`** — 24-point silent-breakage linter (Part 4 of the playbook). Auto-fixes safe transforms, surfaces actionable errors otherwise. Runs pre-finalize.

### Schema graph

`schema/introspect.py` walks `TemplateModel` and emits `FieldNode`s grouped into ~17 logical groups. `schema/enrich.py` merges hand-curated rationale / recommendations / alternatives / example phrasings from `blueprint_field_enrichment.yaml`, keyed by dotted field path. `schema/couplings.py` encodes cross-field rules (e.g. `smart_turn` requires Deepgram). `agent/schema_view.py` projects the graph into the compact view fed into the turn prompt.

### LLM provider

Vertex Anthropic via `langchain_google_vertexai.model_garden.ChatAnthropicVertex`. Service account from `BLUE_PRINT_GOOGLE_CREDENTIALS_JSON`; project/location/model from `BLUEPRINT_VERTEX_PROJECT` / `BLUEPRINT_VERTEX_LOCATION` / `BLUEPRINT_VERTEX_CLAUDE_MODEL`. The user picks which Claude model to use via the `selected_model` `ContextVar`. LLM instances are cached by `(name, thinking_type, budget)` so the OAuth token + httpx pool stay warm.

When Vertex creds are missing, `run_turn` returns a deterministic terminal "I can't help without LLM credentials" so dev/CI doesn't crash.

---

## 2. Module layout

```
app/ai/text/agents/blueprint/
├── __init__.py                  # exports: create_blueprint_agent, get_available_models, selected_model
├── agent/
│   ├── graph.py                 # two-node StateGraph(BlueprintState, context_schema=BlueprintContext)
│   ├── turn.py                  # run_turn, await_approval, route_after_turn, finalize
│   ├── turn_schema.py           # TurnDecision (dynamic enum-constrained schema)
│   ├── schema_view.py           # compact view of schema graph for the LLM prompt
│   ├── state.py                 # BlueprintState, BlueprintContext
│   ├── models.py                # Vertex Claude wrapper + cached factory
│   └── checkpointer.py          # AsyncPostgresSaver + MemorySaver fallback
├── schema/
│   ├── introspect.py            # types.py -> FieldNode list
│   ├── enrich.py                # merge YAML enrichment into FieldNodes
│   ├── graph.py                 # TemplateSchemaGraph (index by path / group)
│   ├── groups.py                # group ordering, conditional-skip rules
│   ├── couplings.py             # cross-field predicates
│   └── models.py                # FieldNode, GroupSpec, Recommendation, Coupling
├── specialists/
│   ├── validator.py             # pure coupling-rule validator
│   └── template_linter.py       # 24-point silent-breakage linter (auto-fix + warnings)
└── draft/
    └── assembly.py              # round-trip draft through ReplaceTemplateRequest
```

---

## 3. Public contract

The agent exposes exactly this surface. Handlers (`app/api/routers/blueprint/chat/handlers.py`) and Loom must not read state fields outside this contract.

**Module `app.ai.text.agents.blueprint`:**

```python
def create_blueprint_agent(*, checkpointer) -> CompiledStateGraph: ...
def get_available_models() -> list[dict]:    # [{"name": ..., "display_name": ...}]
selected_model: contextvars.ContextVar[str]  # per-request model override
```

**Graph input — first turn:**

```python
{
  "messages": [HumanMessage(...)],
}
# context (set once on session creation, passed via agent.ainvoke(..., context=...)):
BlueprintContext(
    mode="create" | "edit",
    reseller_id=str,
    existing_template_id=str | None,
    available_outbound_numbers=list[dict],   # [{"id": ..., "number": ..., "provider": ...}]
)
```

**Graph state — every turn:**

```python
class BlueprintState(BaseModel):
    messages: list[AnyMessage]                       # full history; last AI = user-facing reply
    draft: dict[str, Any]                            # partial template as built so far
    completed_groups: list[str]                      # askable groups gathered
    skipped_groups: list[str]                        # askable groups user opted out of
    pending_approval_for: Optional[str]              # group name for UI approval bar
    validation_issues: list[str]
    template_json: Optional[dict[str, Any]]          # only set on successful finalize
    finalize_retries: int                            # capped at 1 auto-retry
```

**Consumer-side:**

* Preview is `state["draft"]` (partial) or `state["template_json"]` (final).
* Approval bar shows iff `pending_approval_for is not None`.
* Revision intent is handled inside the turn LLM — no external classifier call.
* `_serde.allowed_msgpack_modules` does NOT need any blueprint enums.

---

## 4. Edit mode (unified flow)

Edit-mode reuses the same graph. Session init:

1. Load existing `TemplateModel` from DB into `state.draft` (all fields filled).
2. The user's opening intent goes into `messages` like any first turn.
3. The turn LLM reads draft + transcript and decides what to change. Groups the user didn't touch resolve to `completed` immediately. Touched groups go through `pending_approval_for`.
4. Final review surfaces a diff vs. the original template (rendered by the handler from `existing_template_id`'s stored snapshot), not the full template.

There is no separate edit pipeline.

---

## 5. Non-goals

* No frontend changes outside the existing SSE + REST routes. Loom continues to consume the same wire contract.
* No new template fields. Blueprint only changes how the agent navigates the existing surface.
* No change to the `blueprint_sessions` table schema.
* No DB-learned recommendations yet (see TODOs).

---

## 6. Pending work

Not yet landed; tracked here so contributors don't re-discover them:

* **DB-learned recommendations.** Replace the hand-curated `blueprint_field_enrichment.yaml` `recommendation` blocks with values inferred from production `template` rows (per reseller / use-case cohort, mode/median per field, prevalence-based justification). YAML stays for `rationale` + `alternatives` + `example_phrasings`.
* **Edit-mode E2E test.** The §4 contract has never been exercised end-to-end with `mode="edit"`.
* **Merchant-context injection.** Specialists currently see only `mode` + `reseller_id`. Wiring merchant facts (industry, sample call recordings) into the turn prompt would tighten flow design.
* **Real pytest setup.** Today there is no test suite under `app/ai/text/agents/blueprint/`. Replace with proper pytest covering: schema introspection determinism, validator coupling fixes, template_linter silent-breakage coverage, and a scripted multi-turn smoke against the turn handler.
* **Loom dogfood.** End-to-end smoke with real Vertex creds — eyeball question quality across PrefillAndConfirm → revise → AskGroup → specialist → FinalReview paths.
* **Playbook gaps still open** (see playbook §4 trap list — most have a linter check, but a few are surface-level only):
  * `flow.end_conversation_callbacks: ["service_callback"]` is auto-emitted at finalize when missing — verify the linter actually runs before finalize on every path.
  * Per-function `update_outcome_in_database` hooks (with `outcome` SCREAMING_SNAKE_CASE) need stronger LLM-side prompting; the linter currently warns but cannot synthesize the hook from scratch.
  * `role_messages` vs `task_messages` — keep policing in prompt examples; persistent persona belongs in `role_messages`, never duplicated per-node.
