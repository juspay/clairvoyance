# Breeze Buddy Template Testing Framework

A system for automatically testing any Breeze Buddy template end-to-end — from
generating realistic test scenarios with an LLM, to executing them and validating
that the bot calls the right functions, transitions through the right nodes, and
reaches a valid terminal state.

---

## Overview

Testing a voice bot template manually requires making a real phone call for every
scenario you want to check. This framework replaces that with an automated pipeline:

```
Template flow graph
       ↓
  Path enumeration  (every unique route through the graph)
       ↓
  Scenario generation  (Claude writes realistic user conversations per path)
       ↓
  User review + edits  (scenarios are inspectable and editable in the UI)
       ↓
  Scenario execution  (structural validation OR full LLM replay)
       ↓
  Pass / fail report  (per-turn transcript, node trail, failure reason)
```

---

## API

Four endpoints, all scoped under `/agent/voice/breeze-buddy/templates/{id}/test/`:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `.../generate` | Queue LLM scenario generation. Returns `job_id` immediately. |
| `GET`  | `.../generate/{job_id}` | Poll the generation job. Returns scenarios when complete. |
| `POST` | `.../run` | Run scenarios. Structural runs return synchronously; LLM runs return a `run_id`. |
| `GET`  | `.../run/{run_id}` | Poll a running LLM test. Returns live results as they complete. |

Generation and LLM execution run as FastAPI background tasks so the HTTP layer
never times out, regardless of how many paths or scenarios the template has.

---

## Generation Tiers

Before generating, the user selects a depth tier:

| Tier | Scenarios per path | Temperature | Notes |
|------|--------------------|-------------|-------|
| Basic | 1 | 0.3 | Fast smoke-test. One faithful scenario per path. |
| Advanced | 2–3 | 0.6 | Cooperative, hesitant, and terse user variants. |
| Pro | 4–5 | 0.8 | Adds impatient, chatty, and non-native-speaker personas. |

For Basic tier, up to 5 paths are batched into a single LLM call.
For Advanced and Pro, each path gets its own dedicated call for maximum quality.
All calls run concurrently with `asyncio.gather`, so total wall-clock time equals
the slowest single call regardless of how many paths exist.

---

## How Scenarios Are Generated

### Step 1 — Build the flow graph

Every node in the template's flow has a set of functions, each with a
`transition_to` destination. This forms a directed graph.

### Step 2 — Enumerate every path

A depth-first search walks the graph from the initial node to every terminal node
(nodes with an `end_conversation` action, or nodes with no functions). Each unique
root-to-leaf sequence of hops is collected as a path.

- Cycles are prevented by not revisiting nodes already on the current path.
- Self-loops (a function that transitions back to the same node) are captured as
  one variant where the user retries once before the conversation exits.

### Step 3 — Send paths to Claude

The LLM receives the full template structure (node names, system prompt summaries,
function names and descriptions, required arguments, transition targets, global
functions) and the paths to cover.

For each path, Claude is asked to write one or more test scenarios. Each scenario
is a JSON object with:

- `id` — unique snake_case identifier
- `name` — one sentence describing who the user is and what they do
- `scenario_type` — a short snake_case label the LLM invents to describe what is
  distinctive about this scenario for this specific template (e.g.
  `low_rating_retry`, `address_clarification_then_confirm`). Not a fixed enum —
  the LLM picks a label that reflects the actual template domain.
- `description` — one sentence on what makes this variant distinct
- `payload_example` — realistic values for the template's payload fields
- `turns` — the full conversation from opening to end node (see below)
- `expected_outcome` — the outcome hook value from the last function, if any
- `expected_final_node` — must match the path's terminal node exactly

### Step 4 — Turn structure

Each turn in a scenario represents one user message and its expected bot behaviour:

| Field | Meaning |
|-------|---------|
| `user_message` | What the user says. Empty string for silence/abandonment. |
| `expect_function_call` | The exact function the bot should call on this turn, or null. |
| `expect_no_function_call` | True for turns where the bot should respond without calling a function (clarifications, data collection turns). |
| `expect_node` | The node the bot lands on **after** processing this turn — i.e. the `to_node` of the hop the function triggers. Null if this turn does not cause a node transition. |
| `simulate_function_failures` | A list of functions to inject a mock error response for, to test API failure handling. |

A key prompt constraint: user messages must not look ahead. Every message is only
a natural reaction to what the bot just said in the immediately preceding turn.
The user has no knowledge of topics the bot has not yet introduced.

### Step 5 — Parse and validate

The raw LLM response is stripped of markdown fences and parsed as a JSON array.
Each object is converted to a `GeneratedScenario`. Invalid or malformed objects
are logged and skipped — they do not cause the generation job to fail. If no
valid scenarios are produced at all, the job fails with an informative error.

---

## How Structural Validation Works

Structural mode runs without making any LLM calls. It validates the flow logic
of the scenario itself — does it follow a path that actually exists in the template?

For each turn in sequence:

1. **Node existence** — confirm the current node exists in the template flow.
2. **Implicit advance** — if `expect_node` differs from the current node but no
   function call is declared, validate that the target node exists. This handles
   turns where the bot transitions between nodes without a user-triggered function.
3. **Function availability** — if `expect_function_call` is set, confirm the
   function exists either on the current node or in the template's global functions.
   If both `expect_function_call` and `expect_no_function_call` are set, the turn
   fails immediately (contradictory assertion).
4. **Failure injection** — if `simulate_function_failures` is set for a function
   on this turn, the function is marked as a simulated failure and the transition
   is skipped (the bot does not move to the next node).
5. **Node transition** — for non-failing function calls, the runner follows
   `transition_to` and advances the current node accordingly.

After all turns:

6. **End-node check** — the final current node must be one of the template's
   terminal nodes. If the scenario ends mid-graph, it fails with a message
   explaining which terminal nodes were reachable.

Structural runs are synchronous and return results immediately — typically under
10 ms per scenario.

---

## How LLM Execution Works

LLM mode replays the conversation through Azure OpenAI exactly as it would happen
in a real call, using the same model and temperature configuration as production.

### Setup

The runner loads the initial node's `task_messages` and substitutes all
`{variable}` placeholders with values from the scenario's `payload_example`
(or template defaults for missing keys). These become the opening system prompt.

### Per-turn loop

For each turn:

1. **Build tools** — the current node's functions are converted to OpenAI tool
   definitions and combined with the template's global functions.

2. **Send user message** — appended to the conversation history as a `user` role
   message.

3. **Call the LLM** — `chat.completions.create` with `tool_choice: "auto"`.
   The response is either a text reply or a tool call.

4. **Assert function call** — if `expect_no_function_call` is true but the LLM
   called a function, the turn fails. If `expect_function_call` is set but the
   LLM called a different function (or none), the turn fails.

5. **Inject tool result** — if a function was called:
   - If it appears in `simulate_function_failures`, the configured error payload
     is returned as the tool result instead of `{"status": "ok"}`.
   - Otherwise `{"status": "ok"}` is returned.
   - The assistant message (with `tool_calls`) and the tool result message are
     both appended to conversation history.

6. **Extract outcome** — if the function has a static-source outcome hook, its
   value is recorded as `actual_outcome`.

7. **Node transition** — the runner follows `transition_to` for successful
   (non-failing) function calls, loads the new node's `task_messages`, and resets
   the conversation history to the new node's context with placeholders substituted.

8. **Assert expect_node** — checked *after* the transition, not before. The
   scenario's `expect_node` is the node the bot should be on after this turn is
   fully processed. If the current node does not match, the turn is marked as
   failed.

### End validation

After all turns, the same end-node check as structural mode applies: the final
node must be a recognised terminal node.

LLM runs are asynchronous. Each scenario runs sequentially (one at a time) within
a run job. As each scenario completes, its result is appended to the live results
list visible to the poller.

---

## Validation Summary

Every scenario — regardless of run mode — is checked against the same set of
assertions:

| Check | How it works |
|-------|-------------|
| **Function call** | `expect_function_call` vs. actual function called by the bot. |
| **No function call** | `expect_no_function_call: true` requires the bot to reply without invoking any function. |
| **Node after turn** | `expect_node` is checked after transition — must match the node the bot lands on after the function fires. |
| **End node** | The scenario must finish on one of the template's terminal nodes. |
| **Failure injection** | A simulated failure is delivered as the tool result; the bot must handle the error path gracefully. |

A scenario passes only if every turn passes and the final node is terminal.
The first failing turn's reason is surfaced as the top-level `failure_reason`.
All subsequent turns still execute and are reported, so you can see the full
downstream effect of a failure rather than just the first symptom.

---

## Result Shape

Each executed scenario produces a `ScenarioRunResult` with:

- `passed` — overall pass/fail
- `failure_reason` — the first failure encountered, in plain English
- `turns` — full turn-by-turn transcript with bot response, function called,
  execution status, node name, and per-turn pass/fail
- `nodes_visited` — deduplicated ordered list of nodes the bot transitioned through
- `final_node` — the node the bot ended on
- `actual_outcome` — outcome hook value extracted from the last function call
- `total_latency_ms` — total wall-clock time (LLM mode only)
- `simulated_failures` — list of turn index, function name, and injected error
  for each failure that was triggered

A run summary is produced when all scenarios complete: total, passed, failed,
pass rate, and average latency.

---

## LLM Infrastructure

Scenario generation uses **Claude claude-sonnet-4-6** via **Google Vertex AI**
(`us-east5` region, `breeze-automatic-prod` project).

A dedicated `VERTEX_CREDENTIALS_JSON` service account is used for Vertex — it is
completely separate from `GOOGLE_CREDENTIALS_JSON`, which remains the UAT
TTS/STT account and must not be used for Vertex calls.

The project ID is extracted from the credentials JSON automatically. It can be
overridden with `VERTEX_PROJECT_ID`. The region and model are configurable via
`VERTEX_REGION` and `VERTEX_CLAUDE_MODEL`.

LLM test execution (LLM run mode) uses **Azure OpenAI** via the same
`AZURE_BREEZE_BUDDY_OPENAI_MODEL` configuration as the production voice pipeline —
the same model, same endpoint, same API version.
