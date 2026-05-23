# Buddy Widget — UI Generation Performance Optimisations

**Status:** Draft for review
**Scope:** `app/ai/voice/agents/breeze_buddy/chat/{ui_stream,ui_healer,agent}.py`, `template/{ui_prompt,ui_catalog}.py`, `mcp/__init__.py` (JIT injection)
**Goal:** Reduce time-to-first-component-render and total UI render time without sacrificing the generic spec-stream contract (LLM authoritative over the tree, deterministic healer/validator, template-driven primitive allowlist).

---

## 1. Current architecture recap

The widget generates UI via a SpecStream JSONL format. Each LLM turn:

1. `template/builder.py:351` splices the rendered `## Available primitives` section (`ui_prompt.py:render_primitives_section`) into the system prompt. Built every turn, no cache.
2. Tool results are mutated post-hoc in `mcp/__init__.py:_maybe_inject_ui_instructions` — `_ui_instructions` + `_ui_examples` keys spliced into the JSON envelope (JIT pattern).
3. LLM streams text. Inside `<ui_stream>…</ui_stream>` markers it emits one JSON op per line: `{"op":"add","id":"p1","type":"Tile","parent":"root","props":{...}}`.
4. `chat/ui_stream.py:UiStreamExtractor` is a stateful FSM with a 16-char carry buffer. Each complete line → `heal_op_line` (deterministic rule pass) → `parse_op_line` → `validate_props` (full Pydantic) → `ui_op` SSE event to the widget.
5. Widget applies ops to a session-stateful tree and renders.

**Where the seconds actually go (ranked):**

| Source | Cost | Mitigable? |
|---|---|---|
| LLM token throughput on verbose Tile JSON | **~2-4s for a typical carousel of 8 tiles** | Yes — biggest lever |
| `_ui_examples` re-injected per tool call, never cached | 1-10KB tokens per turn | Yes |
| Primitives section rebuilt per turn | Tokens + ~5-20ms CPU | Yes — trivial |
| Per-line full Pydantic validation | 1-5ms × N lines | Yes — short-circuit |
| No prompt-cache markers | Re-tokenised every turn | Yes — provider feature |

---

## 2. What other systems do (open-source survey)

### 2.1 [vercel-labs/json-render](https://github.com/vercel-labs/json-render) — Vercel's generative UI framework

**Key technique we don't use yet:** explicit **separation of structure and data**.

```
{"op":"add","path":"/root","value":"dashboard"}
{"op":"add","path":"/state/prices","value":[{"name":"BTC","price":98450}, …]}
{"op":"add","path":"/elements/dashboard","value":{"type":"Grid","children":["btc","eth"]}}
{"op":"add","path":"/elements/btc","value":{"type":"Metric","props":{"label":"BTC","value":{"$state":"/prices/0/price"}}}}
```

The element references data via `$state` pointers. Result: the **catalog of elements stays tiny**; only the data array grows with the result set.

**Even bigger lever — the `repeat` directive:**

```json
{"id":"todo-list","type":"Column","repeat":{"statePath":"/todos","key":"id"},"children":["todo-item"]}
{"id":"todo-item","type":"Card","props":{"title":{"$item":"title"}}}
```

One template + a data array = N rendered Cards. For an 8-tile carousel this collapses from **8 full Tile ops (~3KB of LLM output)** to **1 Tile template + 1 data array (~600 bytes)**. ~5× reduction in LLM output tokens for the dominant case (lists of similar items).

Other relevant patterns:
- **Inline vs Standalone modes** — LLM can mix prose with ops (Inline) or emit ops only (Standalone). We're effectively Inline already via the `<ui_stream>` markers.
- **Custom rules per turn** — `catalog.prompt({ customRules: [...] })` lets the caller tack on per-flow guidance. Our `ToolUiHint.instructions` is the analog.
- **JSON Pointer paths (RFC 6902/6901)** — standardised, well-supported by partial-JSON parsers.

### 2.2 [google/A2UI](https://github.com/google/A2UI) v0.9 + v0.10 — Agent-to-UI protocol

Same fundamental insight as json-render: **`updateComponents` and `updateDataModel` are separate message types**. The protocol explicitly says:

> "[The] `updateDataModel` message replaces the value at the specified `path` with the new content. … This allows the server to change the UI's content without resending the entire component structure."

Additional A2UI patterns we should adopt:

- **`createSurface` + `surfaceId`** — multiple named UI regions. Lets the agent open a side panel without disturbing the main chat surface.
- **Adjacency list with deferred resolution** — components can reference children that don't exist yet; the renderer buffers and resolves lazily. Lets the LLM emit in any order and the client renders progressively.
- **`ChildList.object` template + binding** — A2UI's equivalent of json-render's `repeat`. Confirms this is now a *standard*, not an idiosyncrasy.
- **`actionResponse` + `actionId`** — synchronous client→server RPC for things like typeahead. Closes the loop on interactive forms.

We already say in `ui_catalog.py:19` that we mirror "A2UI v0.9 / Vercel json-render SpecStream" — but we currently only implement the flat-op variant, not the data-binding split.

### 2.3 [tambo-ai/tambo](https://github.com/tambo-ai/tambo) — React SDK for streaming generative UI

Tambo's wire protocol (their v1 proposal) is **per-prop incremental streaming via JSON Patch**:

```ts
TamboComponentStartEvent       // component.id, component.name
TamboComponentPropsDeltaEvent  // delta: JsonPatchOperation[], streaming: { title: "streaming", body: "started" }
TamboComponentEndEvent         // final props
```

Each top-level prop has a status (`started` / `streaming` / `done`). The renderer paints the **container immediately** with skeleton placeholders for each prop, then fills them in as deltas arrive. The `useTamboStreamStatus` hook exposes per-prop status so each component can show its own pulse animation.

For our widget this means a Tile could show its skeleton + title within ~30 LLM tokens, then fill `media`, `body`, `actions` as they stream — versus today where the entire ~300-500 tokens of Tile JSON must complete before the line is emitted.

### 2.4 Constrained decoding (XGrammar / llguidance / OpenAI strict mode)

- **XGrammar** (default backend in vLLM/SGLang/TRT-LLM as of March 2026): pushdown automata, **<40μs/token overhead** for arbitrary JSON Schema. Near-zero perf cost.
- **llguidance** (Microsoft): Rust Earley parser, ~50μs/token.
- **OpenAI strict mode** (`response_format: json_schema`, `strict: true`): 10-60s schema compilation on first request, then cached. Eliminates malformed-JSON healer rules entirely.
- **Anthropic** doesn't expose grammar-level constraints, but **tool-call inputs are JSON-Schema-constrained** server-side already — so the natural escape hatch is "encode UI ops as a tool call" (see §3 option C).

The win: if we constrain the LLM to **only valid SpecStream JSONL**, the healer's `dropped_malformed_json` / `unknown_op` paths become dead code, and `parse_op_line` can skip every defensive branch.

### 2.5 Prompt caching (Claude/Gemini/OpenAI)

- **Claude:** explicit `cache_control: {"type": "ephemeral"}` breakpoints, 1024-token minimum per block. Anthropic reports up to **85% latency drop** and ~90% cost drop on cache hits. ~20-23% TTFT improvement measured for stable system prompts.
- **Gemini 2.5:** implicit caching automatic on stable prefixes, 75% input cost discount on cache hits, no code change needed beyond keeping the prompt prefix stable.
- **OpenAI:** automatic prompt caching on `gpt-4o` family for prefixes ≥1024 tokens.

**Order matters** for Claude: `tools → system → messages`. The catalog + tool definitions are the natural cache prefix; user content goes last.

### 2.6 Partial JSON parsers (jsonchunk, streamjson, partial-json-parser, JSON River)

Tolerant parsers that take a streaming buffer and return a `DeepPartial<T>` of whatever has arrived so far. Critical companion to Tambo-style per-prop streaming: the renderer reads `partial.title ?? <Skeleton>` after every delta.

Performance matters here — naive O(n²) re-parsing on every delta destroys the win. The good libraries are O(n) incremental.

---

## 3. Optimisation recommendations

Grouped by impact tier. Each item lists effort, latency impact, and the risk of breaking the generic spec-stream contract.

### Tier A — Token economy (largest impact on LLM output time)

#### A1. Adopt the `state` + `elements` split (json-render / A2UI pattern)

Today the LLM emits 8 Tile ops with all data inline. Move to:

```
<ui_stream>
{"op":"add","id":"root","type":"Carousel"}
{"op":"set_data","path":"/products","value":[<8 product dicts from tool result>]}
{"op":"add","id":"tile-tpl","type":"Tile","parent":"root","repeat":{"path":"/products","key":"id"},
 "props":{"title":{"$item":"title"},"media":{"src":{"$item":"image"},"alt":{"$item":"title"}},
          "body":[{"kind":"key_value","key":"Price","value":{"$item":"price"}}]}}
</ui_stream>
```

3 ops instead of 9. The data block is essentially the tool result echoed — the LLM doesn't have to retype it.

**Massive optimisation:** since the tool result is *already* in the conversation, the server can **emit the `set_data` op itself**, server-side, without the LLM. The LLM only needs to emit the template + a directive pointing at which tool result to bind. Net LLM output drops from ~3KB to ~400 bytes per carousel.

- Effort: 1-2 weeks (catalog change + healer + widget renderer)
- Latency: 5-10× output token reduction on list-rendering turns (the dominant case)
- Risk: medium — requires widget-side `$item` / `repeat` resolver; needs a fallback for non-list UIs (preserve the flat-op form)

#### A2. Server-side speculative emission

After a tool returns, immediately emit a default UI from `ToolUiHint.examples[0]` (or a deterministic mapping) **while the LLM is still generating**. Treat the LLM's eventual `<ui_stream>` as a refinement — same `id`s → `replace` ops, new ids → `add` ops, missing ids → `remove`.

- Effort: 2-3 days
- Latency: TTFR drops from ~1-2s to ~100ms on tool-result-driven turns
- Risk: low if op-merge semantics are correctly defined on the widget; needs explicit "speculative" tagging so analytics can measure refinement deltas

#### A3. Compact wire form (shorthand → expand server-side)

Teach the LLM a shorter form and expand in `process_op_line`:

```jsonl
{"+":"p1:Tile@root","title":"Dawn","body":[{"kv":["Price","₹699"]}]}
```

vs current

```jsonl
{"op":"add","id":"p1","type":"Tile","parent":"root","props":{"title":"Dawn","body":[{"kind":"key_value","key":"Price","value":"₹699"}]}}
```

~60% fewer tokens per op. The canonical ops downstream are unchanged — only the LLM-facing wire shape shrinks. Combine with A1 for compounding gains.

- Effort: 1-2 days
- Latency: ~30-40% output token reduction (with or without A1)
- Risk: low — pure encoding change, healer/validator can normalise

### Tier B — Per-prop streaming (perceived TTFR)

#### B1. Tambo-style per-prop status

After A1 (or even without it), allow the LLM to emit `add` ops with only required props, then `replace` ops to fill in the rest. Healer already handles `replace`. Surface a `prop_streaming` state on the widget so each Tile renders a skeleton until its slot is filled.

Concretely: change the LLM contract from "emit the full Tile JSON, then move on" to "emit `add` with title only, then emit `replace` patches as you confirm each prop". The user sees the carousel skeleton in ~50 LLM tokens, with content filling in over the next ~200.

- Effort: 1 week (mostly widget-side; server needs status SSE event)
- Latency: TTFR ~200-400ms instead of ~1-2s
- Risk: low — opt-in per primitive

#### B2. Partial-JSON parsing on the wire

Currently we wait for a complete JSONL line before emitting `ui_op`. With a tolerant partial JSON parser on the server, we can emit partial ops as deltas — the widget shows the title before the rest of the props arrive.

Library candidates (server-side, Python): `partial-json-parser` (PyPI). Or implement a small state-machine parser ourselves — our op shape is constrained enough to be deterministic.

- Effort: 3-5 days
- Latency: ~100-200ms perceived TTFR improvement on top of B1
- Risk: low — strictly additive, falls back to line-complete emission if parser disagrees

### Tier C — Constrained generation (eliminates whole error classes)

#### C1. Tool-call escape hatch for SpecStream

Define a tool called `render_ui` whose JSON-Schema-constrained input *is* the ops list. Anthropic guarantees the tool input validates against the schema. Replaces `<ui_stream>` markers entirely — the streaming SDK emits `content_block_delta(partial_json=…)` events that we accumulate into a tool call. Healer's malformed-JSON and unknown-type drops disappear because the schema enforces them.

Trade-off: tool-call streaming is per-arg, not per-op-line. We'd accumulate the full ops list before processing. Compatible with A1's data-binding pattern; conflicts with B2's partial-JSON streaming unless we use the SDK's `partial_json` events directly.

- Effort: 1 week
- Latency: small TTFT gain (no marker parsing), large healer simplification
- Risk: medium — locks us into Anthropic-shaped tool-call semantics; Gemini / OpenAI parity needs verification

#### C2. JSON Schema mode on OpenAI / Vertex paths

For OpenAI strict mode and Vertex Gemini structured output, pass the ops JSON Schema in `response_format`. Same idea as C1 without the tool-call indirection. Schema-compile latency is one-time per-process; cached afterwards.

- Effort: 2-3 days per provider
- Latency: TTFT neutral; eliminates a class of healer drops
- Risk: low

### Tier D — Free wins (prompt cache + caching internal computation)

#### D1. `@lru_cache(maxsize=64)` on `render_primitives_section`

```python
@lru_cache(maxsize=64)
def _render_primitives_section_cached(allowlist_key: frozenset[str]) -> str:
    return render_primitives_section(set(allowlist_key))
```

Key by `frozenset(allowlist)`. Templates with identical UI allowlists share the rendered text.

- Effort: 30 minutes
- Latency: ~5-20ms per turn (small) — but removes per-turn Pydantic introspection
- Risk: zero

#### D2. Anthropic `cache_control` markers on the catalog block

Wrap the rendered primitives section in a cache breakpoint:

```python
{
  "role": "system",
  "content": [
    {"type": "text", "text": "<task instructions>"},
    {"type": "text", "text": "<rendered primitives>", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "<per-turn dynamic>"},
  ]
}
```

Same idea for tool definitions — the MCP tool list is large and stable per template.

- Effort: half-day (each LLM driver path)
- Latency: ~20% TTFT drop on cached turns + ~90% input cost drop on the cached portion
- Risk: low — verify cache hit rate via Anthropic's `usage.cache_read_input_tokens` field

#### D3. Move `_ui_examples` from per-tool-result into a cached system-prompt block

Currently in `mcp/__init__.py:196`: `_ui_examples` is JSON-encoded and injected into every tool result, paid in input tokens every call. Move it to a static section at top of system prompt (cached by D2), and let the tool result carry only `_ui_hint_ref: "search_catalog"` — a 30-token pointer instead of a 1-10KB block.

- Effort: 1 day
- Latency: ~1-3KB input tokens off every tool-using turn
- Risk: low

#### D4. Short-circuit `validate_props` on the hot path

After healer (which is deterministic and already catches the common mistakes), the full Pydantic `model_validate` + `model_dump(mode="json")` per line is overkill. Pre-compute `frozenset` of allowed prop names per primitive at module load, do a cheap superset check, defer full Pydantic to a background telemetry task. If the cheap check passes, emit immediately.

- Effort: 1 day
- Latency: ~1-3ms × N ops per turn (small individually, adds up at 50+ ops)
- Risk: low — Pydantic still runs in the background path for `ui_op_dropped` telemetry; no on-wire correctness change

#### D5. `emits_ui` flag on nodes/intents

Many turns produce prose only or pure tool-call cycles with no UI. Skip the primitives section entirely for those (`_splice_ui_primitives` replaces placeholder with empty string when `emits_ui=False`). Saves ~1.5KB of input tokens on the prose-only path.

- Effort: half-day
- Latency: ~50-150ms TTFT on prose turns
- Risk: low

---

## 4. Suggested rollout sequence

Phase by ROI vs effort:

| Phase | Items | Effort | Latency win | Goal |
|---|---|---|---|---|
| **0 — Free wins** | D1, D2, D4, D5 | 2 days | 200-400ms TTFT + measurable input-token drop | Establish baseline + prove caching headroom |
| **1 — JIT prompt move** | D3 | 1 day | 1-3KB input tokens/turn off | Compounds with D2 |
| **2 — Wire compaction** | A3 | 2 days | 30-40% output-token reduction | Single biggest "no architectural change" win |
| **3 — Per-prop streaming** | B1, B2 | 1.5 weeks | TTFR drops to ~200ms | Perceived speed transformation |
| **4 — Data/structure split** | A1 + widget renderer changes | 2 weeks | 5-10× LLM output reduction on lists | Catches us up to json-render / A2UI parity |
| **5 — Speculative emission** | A2 | 3 days | Instant first paint after tool result | Stacked on phase 4 |
| **6 — Constrained decoding** | C1 + C2 (per provider) | 1 week each | Healer simplification + small TTFT | Stable end state |

Phase 0 is essentially free and proves the methodology. Phase 2 + 3 + 4 are where the user-visible step changes live.

---

## 5. Measurement plan

Before phase 0 ships, instrument:

- `ttft_ms` — from `/message` POST to first `assistant_token` / first `ui_op` SSE event (split metric)
- `ttfui_ms` — from `/message` POST to first `ui_op` SSE event
- `ttlui_ms` — from `/message` POST to last `ui_op` SSE event in the turn
- `llm_output_tokens` — break down by `<ui_stream>` vs prose
- `cache_read_input_tokens` — Anthropic cache hit rate after D2 lands
- `ui_op_dropped` count + reason — healer success rate; should drop after C1/C2

Compare p50/p95/p99 across phases. Tag every SSE stream with the active optimisation phase so we can A/B in production.

---

## 6. Generic functionality preserved

Every recommendation is additive:

- **A1's data-binding split** keeps the flat-op form as a fallback. LLM can still emit a hand-crafted tree when needed.
- **A2's speculative emission** is fully overridable — the LLM's final ops win.
- **A3's compact wire form** is a pure encoding alias; canonical ops downstream are unchanged.
- **B1/B2** are skeleton/progressive enhancement; final state matches today's.
- **C1/C2** narrow what's *allowed* but the existing healer rules already enforce this — we're just moving enforcement to the right layer.
- **D1-D5** are pure caching/short-circuiting; no contract change.

The catalog remains the single source of truth. Templates still declare allowlists and JIT instructions. Merchants still get per-template UI customisation.

---

## Sources

- [vercel-labs/json-render — Generative UI framework (GitHub)](https://github.com/vercel-labs/json-render)
- [json-render streaming docs (SpecStream JSONL format)](https://json-render.dev/docs/streaming) (cloned `apps/web/app/(main)/docs/streaming/page.mdx`)
- [json-render generation modes (Inline vs Standalone)](https://json-render.dev/docs/generation-modes)
- [json-render data binding (`$state`, `repeat`, `$item`)](https://json-render.dev/docs/data-binding)
- [Vercel Releases JSON-Render: a Generative UI Framework for AI-Driven Interface Composition — InfoQ (Mar 2026)](https://www.infoq.com/news/2026/03/vercel-json-render/)
- [google/A2UI v0.9 + v0.10 specifications](https://github.com/google/A2UI) (cloned, specifically `specification/v0_10/docs/a2ui_protocol.md`)
- [A2UI Protocol v0.9 — Agent-to-UI streaming](https://a2ui.org/specification/v0.9-a2ui/)
- [A2UI v0.9: What's New in Google's Generative UI Spec — CopilotKit blog](https://www.copilotkit.ai/blog/a2ui-whats-new-in-google-generative-ui-spec)
- [tambo-ai/tambo — Generative UI SDK for React](https://github.com/tambo-ai/tambo) (cloned, `plans/api-v1-proposal.md` + `plugins/tambo/skills/generative-ui/references/component-rendering.md`)
- [Tambo per-prop streaming docs](https://docs.tambo.co/)
- [Anthropic Prompt Caching Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [PromptHub — Prompt Caching with OpenAI, Anthropic, and Google Models](https://www.prompthub.us/blog/prompt-caching-with-openai-anthropic-and-google-models)
- [OpenAI Structured Outputs — strict JSON Schema mode](https://platform.openai.com/docs/guides/structured-outputs)
- [XGrammar: Flexible and Efficient Structured Generation (arXiv)](https://arxiv.org/pdf/2411.15100)
- [Structured Decoding in vLLM (XGrammar + llguidance benchmarks)](https://blog.vllm.ai/2025/01/14/struct-decode-intro.html)
- [Streaming AI responses and the incomplete JSON problem — Aha.io engineering blog](https://www.aha.io/engineering/articles/streaming-ai-responses-incomplete-json)
- [Thesys C1 — Generative UI API overview](https://www.thesys.dev/)
- [Vercel AI SDK useObject / streamObject docs](https://ai-sdk.dev/docs/reference/ai-sdk-ui/use-object)
- [CopilotKit Generative UI overview](https://www.copilotkit.ai/generative-ui)
