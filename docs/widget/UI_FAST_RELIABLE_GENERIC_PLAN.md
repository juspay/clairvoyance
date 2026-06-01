# Buddy Widget — Fast, Reliable, Generic UI (execution plan)

**Status:** In progress — Phase 0 landing in this branch
**Companion:** `docs/widget/UI_PERF_OPTIMIZATIONS.md` (PR #801) — the perf survey this plan executes against.
**Scope:** `chat/{agent,ui_stream,ui_healer,metrics}.py`, `api/routers/breeze_buddy/chat/handlers.py`, `template/*`, the 4 Shopify-assist templates.

## Goal & non-negotiables

Make the widget's generative UI **fast**, **reliable**, and **generic** — without
giving up the thing that makes it generative.

- **The LLM stays the author of the UI.** We do *not* move card rendering
  server-side. Emergent UI (e.g. an ad-hoc "WhatsApp support" redirect button the
  model decides to show) must keep working.
- **Engine stays domain-blind.** Every domain specific (Handoff label/url,
  required-op declarations, render hints) lives in the **template**. Widget
  primitives stay vertical-agnostic.
- **Catalog is the single source of truth.** Every change is additive / opt-in,
  with the flat-op form preserved as fallback (per `UI_PERF_OPTIMIZATIONS.md` §6).
- **Don't over-engineer.** Measure first; add machinery only where the numbers
  demand it.

## The incident this addresses

Production (The Drip Co., 2026-05-29, session `f659423e`): a cart rendered the
tiles + total but the **"Review and checkout" Handoff was missing** — the LLM
emitted `… → CardHeader(total) → SideEffect`, silently skipping the Handoff op
(present in 3 of 4 carts that day, ~25% drop). Not truncation (the op *after* it
was emitted), not a render bug — **the LLM intermittently drops a structural op.**
The cart-cookie `SideEffect` is LLM-emitted too, so it carries the same risk.

The fix for the dropped button and the fix for slow rendering turn out to be the
**same lever** — shorter, more structured, cached emission renders faster *and*
drops fewer ops. Speed and reliability are not traded against each other here.

## How we get reliability (LLM keeps authoring)

Three generic levers, cheapest first:

1. **Make it hard to drop** — restructure the cart `tool_ui_instructions` into a
   tight ordered checklist with a hard invariant ("the Review-and-checkout
   Handoff is MANDATORY; a cart without it is invalid"). Plus shorter emission
   (lever A1 below) drifts less. Cuts the rate; not a guarantee.
2. **Self-heal the gap** — template declares per-render *required ops* (by id); at
   turn finalization the healer **injects any the LLM dropped**, from the declared
   snippet. The LLM still authors the card; the net is invisible unless it fails.
   This is the *guarantee*, and it's the "self-healing when needed" goal. A
   guardrail, **not** a server render.
3. **Constrain at generation time** (`UI_PERF_OPTIMIZATIONS.md` Tier C) — emit ops
   through a JSON-Schema-constrained channel so invalid/incomplete renders can't
   be generated. Durable end-state; heavier.

## How we get speed (none of it touches authorship)

Time-to-UI is gated by tokens emitted + when generation starts. Three sub-levers:

- **Emit fewer tokens** — **A3** compact wire form (~30-40%/op), **A1**
  data/structure split (`repeat`/`$item`, 5-10× on lists; the model authors the
  template, the server binds the already-present tool-result data).
- **Start sooner** — Tier **D** free wins (prompt-cache the catalog, move
  `_ui_examples` to the cached prefix, `emits_ui` skip, short-circuit validation).
- **Paint progressively** — Tier **B** per-prop streaming (skeletons; perceived
  first-paint ~200-400ms).

> Trade-off we accepted by keeping the LLM as author: no ~100ms server-paint (that
> needed server-side emission). Floor is TTFT + first tokens → realistically
> ~200-400ms perceived + a much faster total. That's the price of generative UI.

## Phases

### Phase 0 — Foundation (this branch)
- [x] **Turn metrics** (`chat/metrics.py` + router observation): `ttft_ms`,
      `ttfui_ms`, `ttlui_ms`, `total_ms`, `ui_ops`, **`ui_dropped` + reasons**,
      `healer_applied`, `tool_calls`, `prose_chars`, `ui_chars`, `status` — one
      structured `[CHAT_METRICS]` log per turn, tagged by `phase=`. Passive; never
      mutates the wire. Structural only (no payload content).
- [ ] Dashboards / queries: drop-rate and TTFR p50/p95/p99 by template.

### Phase 1 — Now (cheap, template-only)
- [ ] **Cart instruction hardening** on the 4 templates — ordered checklist +
      mandatory-Handoff invariant; make the cart-cookie `SideEffect` equally
      non-skippable. *(Production PUT — reviewed before deploy.)*
- **Tier D free wins** — [x] D1 LRU primitives cache · [ ] D2 Anthropic
      `cache_control` markers · [ ] D3 `_ui_examples` → cached system prompt ·
      [ ] D4 short-circuit `validate_props` · [ ] D5 `emits_ui` skip. *(D2/D4/D3
      touch the live LLM-call or validation hot path — each is its own change,
      not batched.)*

### Phase 2 — Next (bigger speed, no authorship change)
- [x] **A3** compact wire form — LLM emits `{"+":"<id>:<Type>@<parent>", ...props}`
      / `{"~":"id",...}` / `{"-":"id"}` + `{"kv":[k,v]}` body rows; expanded
      server-side in `ui_stream.expand_compact_op` *before* the healer, so the
      canonical ops + persisted `ui_blocks` are unchanged and both forms are
      accepted. **Measured: ~15% smaller on a content-heavy carousel, up to ~46%
      on small structural ops** (lower than #801's 30-40% because real tiles are
      content-dominated — that's A1's territory).
- **A1** data/structure split (`repeat`/`$item`):
  - [x] **v1 — inline data.** LLM emits ONE element with `repeat:{items:[...],key}`
        + `{"$item":"<path>"}` bindings; the server fans it out to N canonical flat
        ops in `ui_stream.expand_repeat_line` (no widget change; all rows always
        render — they can't be individually dropped). The LLM still shapes the data
        array, so per-item emergent choices (e.g. binding the red variant's image)
        survive. **Measured ~33% smaller / 1.5x on an 8-tile carousel** (structural
        repetition removed; data is typed once).
  - [ ] **v2 — tool-result binding (deferred).** Bind the array from the turn's
        tool result so the LLM types *no* data → the ~5–10x cut on large
        "show-all" lists. Agent-level (the expander must read the turn's tool
        results). **Build bind-all only — NOT selector paths or id-select-lists.**
        Gated on metrics. See the full breakdown in "A1 v2 — the breakdown" below.

### Phase 3 — Only if metrics still show gaps
- [ ] **Self-heal completion net** — declared required-ops, inject-on-miss in the
      healer. The reliability *guarantee*; build only if Phase 1 doesn't drive
      drops to ~0.
- [ ] **B** per-prop streaming (perceived first-paint).
- [ ] **Tier C** constrained decoding (durable reliability end-state).

## Sequencing

`Phase 0 → Phase 1 → measure → Phase 2 (A1 for the carousel win) → reach into
Phase 3 only where the numbers say so.` Ship and measure before each escalation.

## A1 v2 — tool-result binding: the breakdown (deferred)

**What it is.** In v1 the LLM types the data array (`repeat:{items:[...]}`); the
server only dedups the *structure* (~33%). In v2 the LLM types **no data** — it
references the tool result, and the server binds the array from the tool output
that's already in context: `repeat:{from:"<tool>.<path>"}`. LLM output collapses
to the template + a reference → the ~5–10x cut, and the data is the *literal
tool result* so prices/URLs/images can't be mistyped.

**The catch.** Binding the raw array shows *all* of it. The moment the model
wants anything other than "all rows, default fields," the selection has to be
expressed *somehow* — and where that selection lives decides reliability.

**Three list shapes → three mechanisms.** They are not the same problem:

| Shape | Reliable mechanism | Why |
|---|---|---|
| **Show ALL of a result** (20 of 20, default fields) | **v2 bind-all** `repeat:{from:"<tool>.<path>"}` | one tool reference; data from source; the big win |
| **Show a chosen SUBSET** (5 of 20) | **v1 inline** — LLM emits the 5 rows it picked | selection is the LLM's strength; bind-all would show 20; an id-select-list would add a reference surface for a modest win |
| **Per-item value selection** (red variant's image, not default) | **v1 inline / flat** — LLM picks each value | the LLM reasons over the data; a predicate can't ("crimson" ≠ "Red", out-of-stock fallbacks, …) |

**Build vs don't build.**
- **Build (if justified): bind-all only.** `from` is a dotted path into the
  latest tool result; no `items`, no `select`, no predicates. Covers the dominant
  "show me the products" case.
- **Don't build: selector paths** (`variants[options.color='Red'].image`). They
  move the LLM's reliable *semantic* selection into an abstract *declarative
  query* the engine executes — schema-coupled, silent `None` failures, can't
  handle the messy real cases. The LLM is better at selection than at writing
  correct predicates.
- **Probably don't build: id-select-lists.** For subsets, v1 (emit the chosen
  rows) avoids the reference surface entirely; the id-list saving is modest.
  Revisit only if large subset-renders prove common.

**The principle.** *Engine for mechanical binding, LLM for semantic selection.*
v2's win is moving **data** to the engine (reliable: no typos, from source). Its
one new failure surface is the **reference** — the LLM must name the right tool
result + path; a wrong reference fails silently (empty list). Never push the
*which/what* (selection) into a binding the engine resolves — that's where
reliability drops.

**Implementation notes.** Agent-level: `expand_repeat_line` is stateless today
(per line). v2 needs a `tool_data` dict (`tool_name → latest result`) threaded
from the agent's `_run_turn_inner` (which already accumulates `tool_result_pairs`)
into `process_op_line`. Timing: a `from`-binding can only reference a tool result
from a **completed prior cycle** (results land after each cycle's stream) — the
normal "search in cycle N, render in cycle N+1" flow satisfies this; a reference
to a not-yet-returned tool resolves to empty. Stays domain-blind: `from` is just a
path; the engine never learns "products."

**Decision gate.** Build *nothing* here until `[CHAT_METRICS]` shows large
"show-all" carousels (high `ui_chars` from many rows on uniform lists) are common
*and* their token cost is a real latency/cost driver. If subset / per-item lists
dominate instead, v1 already covers them reliably — do nothing.

## Done already (context)
- **Cart-cookie key fix** — the `cart` cookie now carries `<token>%3Fkey%3D<key>`
  (was keyless, which made `/checkout` mint a new empty cart). Deployed + verified
  on all 4 Shopify-assist templates.
