# Breeze Buddy Assist — Scale Roadmap

Single source of truth for planned work. Update as scope changes. No implementation has started for any item below the "Already shipped" section unless explicitly noted.

**Last updated**: 2026-05-21 (rev 9 — widget polish wave: cancel/stop, thinking indicator, dual-channel display, new-chat sheet, showcase landing, BbTurn segment-id fix, composer redesign; voice readiness audit recorded for next sprint)
**Validation policy**: end-to-end testing happens **once**, after all planned sprint work has landed. No piecemeal browser tests in between — except for smoke checks immediately after each sprint to catch obvious wire-format regressions. Sprint 1 surfaced two tweaks (max_tokens, alias rename); Sprint 1.5 added Tile + groups; Sprint 1.6 (commerce-scrub) + 1.7 (session resume) + Sprint 3 (UCP cutover, S3.1+S3.2) all shipped and live-validated. See §"Real-traffic learnings".

---

## 🧭 Quickstart for resuming (read first after context compaction)

If you're picking this up after compaction, here's the orientation:

1. **The big picture in one paragraph**: cart-id fix + Sprint 1/1.5/1.6/1.7 + **Sprint 3 (UCP cutover, S3.1 + S3.2 idempotency)** are all shipped AND live-validated. UCP migration is 26 days ahead of Shopify's 2026-06-15 deadline. Catalog + cart traffic runs on `/api/ucp/mcp` via a direct-HTTP poster that bypasses Pipecat MCPClient (UCP rejects `initialize`); profile hosted at `https://breezebuddy.ai/.well-known/ucp/agent.json`. The runtime carries **seven** load-bearing pivots: (a) no UI subagent — single-agent + in-stream healer; (b) pure SpecStream JSONL — no hybrid DSL; (c) composite slot-filled `Tile` primitive; (d) per-template primitive-group selection; (e) zero commerce primitives in runtime; (f) session-resume via localStorage + persisted ui_blocks; (g) **declared `tool_schemas` + `default_args` + direct-HTTP poster as the UCP integration path; `state_reducers` + `tool_arg_injection` (now with generic generators: uuid_v4/uuid_v7/timestamp_*) are the commerce-agnostic engines on top.** Payment works today via `continue_url` Handoff → Shopify hosted checkout. On top of the seven pivots, the **widget panel is now polished for release** (Sprint 1.8): stop-button + server-side cancel via Redis pubsub fan-out, TTFB-only thinking indicator with random Claude-Code-style verbs, dual-channel `display?: string` on `to_assistant` actions (LLM payload keeps GIDs, user bubble gets human label), new-chat confirmation bottom-sheet, and a 20-vertical use-case showcase at `/showcase.html` (Sprint 1.9). **We are in release-readiness mode** — refine, smoke, ship; next phase is queued below.

2. **Where the deep context lives** (cited liberally below):
   - `poc/GOLDEN_INSIGHTS.md` — cart-id loss + generic-vs-flavoured verdict
   - `poc/GOLDEN_UI_INSIGHTS.md` — open-source UI patterns (15-repo synthesis)
   - `poc/WEB_RESEARCH_UI_GEN.md` — production-system research, source of the three pivots
   - `poc/REPOS.md` — index of 15 cloned repos under `poc/`
   - `docs/features/BREEZE_BUDDY_ASSIST.md` — primary design doc

3. **How to act on it**: pick a sprint item (S1.1, S1.2, etc.) below, read its "Files to touch" + "Acceptance criteria", read the relevant `poc/*.md` referenced in §Architectural decisions, and start. No e2e tests between items — they accumulate for the final pass.

4. **What NOT to revisit**: see §"Considered and rejected" — items already weighed and explicitly dropped (UI subagent, hybrid DSL/JSONL, RSC-streamed UI, etc.). Don't re-litigate without new evidence.

5. **Release readiness — what's required to refine + ship the current set** (2026-05-20):

   **Required before release** (small, mechanical, all ≤30 min each):
   1. **Smoke-test S3.2 idempotency_key on Milton** — confirm UCP accepts top-level `idempotency_key`; if not, nest under `meta.idempotency_key` (1-line template change). Look at request body in clairvoyance logs after one `create_cart` + one `update_cart`.
   2. **Move private signing keys** from `/tmp/ucp-keys/private_jwk.json` → `~/.config/breeze-buddy/private_jwk.json`. `/tmp` evaporates on reboot. Update any clairvoyance env / scripts that reference the old path.
   3. **Consolidated e2e validation pass** (§"Validation strategy" — 13-point checklist). One pass across both merchants; no piecemeal repeats.

   **Optional / deferred** (not blocking; can revisit anytime):
   - **A7 — `primitive_disabled` telemetry deliberate test** — requires temporary template tweak (`disabled_primitives: ["Table"]`), re-provision, deliberate Table emission, then revert. Proves the group-allowlist gate end-to-end. ~3 min when needed.
   - **Cross-provider swap (Anthropic → OpenAI)** — flip `llm_configurations.sdk` in the template, re-provision, repeat A1. Validates block-codec neutrality. Low priority.
   - **Voice mode is NOT in this release.** Backend `/voice/connect` + `/voice/end` are live and `VoiceSession` types are exported from the SDK, but SDK `transferTo('voice')` is a stub and the widget has no in-call UI. Full breakdown + effort estimate recorded in §"Voice readiness audit (2026-05-21)" below. Plan is to ship voice as a clean next-sprint item once one open product question (call-screen UX) is decided.

   **Anticipated quirks that did NOT surface** (recorded so we don't re-fear them):
   - **Numeric-tag noise**: Milton products carry tag codes like `1008`/`5019`/`6010`. The LLM correctly ignored them and synthesized semantic chips (`Stainless Steel`, `Vacuum Insulated`, `BPA-Free Plastic`) from descriptions. **No healer rule needed.**
   - **Empty `alt_text`**: many Milton products have no `media[0].alt_text`. The LLM correctly used `product.title` as the alt per JIT. **No `props_validation_failed` observed.**

   **Process notes**:
   - The user owns the Clairvoyance process directly (runs it in their own terminal, never spawn in background). Same for any long-running session-owned process.
   - The widget Vite dev server is spawned by the assistant on this machine (port 5180); needs a restart whenever the SDK dist changes because Vite caches resolved imports. `pnpm install` from the nautilus root refreshes the pnpm cached symlink to the SDK after a build.

---

## 📦 This release vs next phase

### In this release (refine + ship)

All of these are shipped + live-validated; this is what we are smoke-testing once more and releasing:

| Sprint | What | Status |
|---|---|---|
| Cart-id fix | block persistence + session state + arg-injection | ✅ shipped + live-validated |
| Sprint 1 | SpecStream JSONL wire + in-stream healer + JIT UI instructions | ✅ shipped + live-validated |
| Sprint 1.5 | Composite `Tile` primitive + per-template primitive groups | ✅ shipped + live-validated |
| Sprint 1.6 | Commerce-scrub (runtime now 100% commerce-agnostic; Money primitive deleted; `scale_by_exponent` replaces `scale_money_amount`; `requires_buyer_*` → `requires_user_*`) | ✅ shipped + live-validated |
| Sprint 1.7 | Session resume on refresh (localStorage + `GET /widget/session/{id}` + persisted `ui_blocks` column) | ✅ shipped + live-validated |
| Sprint 3 (S3.1) | Full UCP cutover — direct-HTTP poster, declared `tool_schemas`, `default_args` profile injection, FLAT cart shape, `links[]` policy fallback | ✅ shipped 2026-05-20 + live-validated on Milton |
| Sprint 3 (S3.2) | Idempotency-Key — generic `generators:` source on `tool_arg_injection` (uuid_v4/uuid_v7/timestamp_iso8601/timestamp_unix_ms); template wires `idempotency_key` on `create_cart` + `update_cart` | ✅ shipped 2026-05-20 (151/151 tests; +8 generator tests) |
| Sprint 1.8 | Widget polish wave — cancel/stop button with multi-pod-safe server cancel (Redis pubsub `breeze-buddy:chat:cancel` + per-pod task registry in `cancel_bus.py`), TTFB-only thinking indicator (mascot + shimmer + sequential dots, 12 random verbs), dual-channel `display?` on `UiAction.to_assistant` (catalog + SDK + widget + canonical-template prompt), new-chat confirmation bottom-sheet, BbTurn segment-id stability fix (typewriter no longer re-types on tool-without-UI), composer redesign (send button inside input pill, "buddy by Breeze" watermark above), mascot launcher iterations (BbBuddy pearly orb + BbSolidOrb oil-slick swirl variant) | ✅ shipped 2026-05-21 |
| Sprint 1.9 | Use-case showcase — `showcase.html` + `Showcase.svelte` rendering 20 diverse vertical demos using REAL widget chrome (BbHeader + BbWatermark + BbComposer) and the actual `UiRenderer`, demonstrating cross-industry reusability with 9+ distinct visual archetypes | ✅ shipped 2026-05-21 |

**Refinement gates before release** (see §"Release readiness" in Quickstart): smoke-test idempotency_key wiring on Milton; move signing keys out of `/tmp`; consolidated e2e pass.

### Next phase (after this release)

Concrete, in priority order. Pick from this list when refinement + release lands:

| Phase | Sprint | What | Why now | Effort |
|---|---|---|---|---|
| **NP-1** | S4.1 | **Eval harness** — frozen prompts + golden screenshots + LLM-as-judge in CI | Reliability foundation. Every template tweak today is rolling the dice. **Recommend starting here.** | ~1 week |
| **NP-2** | S2.1 | Voice/chat UI parity — same SpecStream over RTVI + SSE | **Architecture is 70% in place** — backend `/voice/connect` + `/voice/end` live, `VoiceSession` types exported, Daily.co + Pipecat plumbing reused from the standalone voice product. Gap is ~7–8 dev-days of SDK + widget UI wiring. See §"Voice readiness audit (2026-05-21)" for the full breakdown. | ~7–8 dev-days |
| **NP-3** | S2.2 | MCP Apps emission wrapper | Free distribution to Claude / ChatGPT / Goose / VS Code | ~3 days |
| **NP-4** | S2.3 | `refine_ui(card_id, intent)` tool | "Make this card smaller" → targeted patch, not full re-render | ~3 days |
| **NP-5** | S2.4 | **A2A AgentCard** at `/.well-known/agent-card.json` ← **distinct from the UCP profile at `/.well-known/ucp/agent.json` (already shipped)**. A2A AgentCard is Google's agent-to-agent federation spec; the UCP profile is Shopify's commerce protocol identity. Two specs, two endpoints. | A2A federation forward-compat. Future-proofing only. | ~2 days |

### 📦 Repo layout — widget moved to loom (2026-05-20)

The Breeze Buddy Assist widget package previously lived at `nautilus/packages/breeze-buddy-assist-widget/`. It now lives at `loom/packages/breeze-buddy-assist-widget/`, colocated with the `@juspay/breeze-buddy-client-sdk` it depends on (was an absolute `file:` link, now `workspace:*`).

Production serve path: `https://breezebuddy.ai/widget/assist.js` (loom nginx + Cloud Run). Storefronts load it via a `<script>` tag — no nautilus build artifact required. Shopify-app integration shrinks to:

```html
<script src="https://breezebuddy.ai/widget/assist.js" async></script>
<breeze-buddy-assist tenant="…" shop="…"></breeze-buddy-assist>
```

What stays in nautilus: the Shopify app (`shopify-apps/breeze-buddy/`), OAuth/webhooks, the canonical template JSON (`static/buddy-assist-agent/`), provisioning scripts, DB migrations — i.e. everything the Shopify-app installation flow touches. What stays in clairvoyance: unchanged (the Python agent backend).

Historical references in this doc that mention `nautilus/packages/breeze-buddy-assist-widget/...` describe past state and are accurate for the period they refer to. For *today's* layout, see §"File map".

### Demoted to deferred-on-demand (no longer in next phase)

- **S3.3 Dual-tier error envelope** — today's catch-all "technical hiccup" message is acceptable. Revisit only after eval data (NP-1) shows which error scenarios actually hurt UX. Trigger: ≥3 distinct error categories surface in production telemetry with bad UX impact.
- **S3.4 ConfirmCard Trusted Surface** — current `continue_url` Handoff gets shoppers to Shopify hosted checkout, where Apple Pay / Google Pay / cards / Shop Pay all work. ConfirmCard is the "agentic-pay-in-chat" upgrade — strategic bet, not a deadline item. Trigger: positioning commits to in-chat purchase completion as a differentiator.

Both items remain documented under §"Sprint 3" below for posterity, with their original effort + mechanism notes. The "Deferred — on-demand" table is the load-bearing one.

---

## Architectural decisions (record of pivots — don't re-litigate)

Three architectural pivots happened this week. They supersede the original "build a UI subagent" direction. Recording them here so future-us doesn't re-discover them.

| Decision | Rationale | Source |
|---|---|---|
| **No UI subagent.** Stay single-agent for UI generation. | Vercel v0 (most capable UI generator in production), Shopify Sidekick, Claude Artifacts, ChatGPT Canvas, Tldraw Make Real — every leading production system does this without a UI subagent. Cognition's "subagents fragment context, UI needs coherent context" argument applies. | `poc/WEB_RESEARCH_UI_GEN.md` §"Counter-evidence" |
| **In-stream healer instead of subagent.** A deterministic + (later) tiny-model layer fixes mechanical errors mid-stream in <250ms. Solves correctness without the cost/latency/complexity of a second LLM call. | Vercel v0 publicly documented this as "LLM Suspense" + AutoFix. Difference between ~90% and ~99% generation success. | `poc/WEB_RESEARCH_UI_GEN.md` §"Vercel v0" |
| **Pure SpecStream / A2UI wire format.** Drop our OpenUI Lang DSL. LLM emits JSON-patch operations over JSONL, renderer applies patches incrementally against a session-stateful component tree. | Industry consolidating on this format (A2UI, json-render, MCP Apps). Bulletproof JSONL parsing. Multi-framework renderers available. Stateful UI for free. Standards-aligned for cross-host distribution. Reliability + scalability win — token cost is the only tradeoff and not a constraint. | `poc/WEB_RESEARCH_UI_GEN.md` §"NEW patterns A/B" |
| **JIT UI instructions (Sidekick pattern).** Per-tool UI guidance ships in tool *responses*, not the system prompt. | Shopify Sidekick proved this scales to many tools without prompt bloat. Extends our existing `response_transforms` infra naturally. | `poc/WEB_RESEARCH_UI_GEN.md` §"Pattern 7" |
| **Trusted Surface bypasses LLM entirely.** High-stakes UI (T&C, confirm, future payment) is server-rendered directly from a typed payload — no LLM in the path. | AP2 spec mandates the trusted surface be non-agentic. Architectural seam we already have in the T&C modal. | `poc/AP2/UI_INSIGHTS.md` |
| **Composite `Tile` primitive for list items.** Renderer owns the layout; LLM fills typed slots (media/eyebrow/title/subtitle/body/attributes/actions/density). One `ui_op` per item replaces a hand-composed Card+Image+Text+Tag+Button subtree. Slot-filled UI is uniform by construction; the LLM can't half-emit a card. | Real-traffic smoke showed LLM-composed cards drift in completeness/order across items in the same Carousel. Industry consolidates on this pattern (A2UI templates, MCP Apps widgets, Slack Block Kit, OpenAI Apps SDK). ~80% fewer ops per card; bulletproof uniformity. | First live smoke 2026-05-19 (screenshots showed half-cards); see §"Real-traffic learnings" and §"Already shipped → Sprint 1.5". |
| **Per-template primitive-group registry.** Catalog partitions into named groups (`core`/`composite`/`graphs`/`metrics`/`forms`/`media`/`data`). Template's `configurations.ui_catalog.enabled_groups` declares which groups the merchant gets. Server validates ops against the resolved allowlist (drops disabled types with `primitive_disabled:<type>`, distinct telemetry from `unknown_type`). System prompt auto-renders only the enabled primitives' schemas — LLM never even sees disabled types. | Generalizes the "what UI does this merchant get" question without proliferating use-case-specific primitives. New primitive families (graphs, forms) ship behind a group flag — existing templates unaffected. The LLM-only-sees-enabled principle prevents accidental emission and shrinks per-merchant prompt size. | Designed during Sprint 1.5 polish loop; replaces the deleted `gen_ui` allowlist at the right abstraction layer. |
| **Zero commerce primitives in runtime — vertical flavor lives entirely in template + MCP.** Deleted the `Money` primitive (the only commerce-coded type in the catalog). Replaced its usage with `key_value` body rows carrying upstream display strings (e.g. `{key:"Price", value:"₹699.95"}`). Renamed `MessageResolution.requires_buyer_*` → `requires_user_*` (commerce-vocabulary leak). Replaced `scale_money_amount` (currency-aware) with `scale_by_exponent` (pure arithmetic, exponent from template args). | The runtime is now domain-neutral: a fitness, scheduling, finance, or real-estate template can adopt the same catalog + healer + builder without any commerce baggage. Price formatting is the upstream tool's responsibility (Shopify already returns formatted amounts); the LLM passes display strings through verbatim. Removes the entire `_PROP_ALIASES` Money entries + 2 Money-only healer rules + ISO 4217 currency tables. ~250 LoC net deletion. | Triggered by user audit 2026-05-19 questioning "why is Money a primitive at all?" + "scale_money_amount is hard commerce." Both calls were correct. |

What's subsumed by these decisions (items that were separate, now collapse into the above):

- ~~Three-channel result envelope~~ → natural in SpecStream (`content` is prose, JSONL stream is `structuredContent`, `_meta` for host directives)
- ~~Typed `<Money>` `<Message>` `<Handoff>` primitives~~ → become schema entries in the SpecStream component catalog
- ~~A2A DataPart promotion~~ → SpecStream IS the DataPart format; just publish the AgentCard
- ~~English `description` on every primitive~~ → schema descriptions are first-class in component catalog
- ~~Hybrid DSL/JSONL with compile step~~ → pure JSONL, no compile step
- ~~Split `StatusUpdate` vs `ArtifactUpdate`~~ → SpecStream's stateful patches subsume this
- ~~UI subagent (4-week build)~~ → deleted; healer replaces it

### Considered and rejected (don't re-bring these up without new evidence)

| Idea | Why rejected | Could revisit if |
|---|---|---|
| **UI subagent invoked as tool call** | v0/Sidekick/Artifacts all produce best-in-class UI without one. Cognition's "subagents fragment context, UI needs coherent context" applies. JIT instructions + healer + constrained catalog hit the same quality ceiling without the cost/complexity. | Single-agent quality plateaus measurably below target after S1.1+S1.3+S1.2 land. Have data, not vibes. |
| **Hybrid DSL/JSONL (DSL author surface, JSONL wire)** | Compile step is an extra failure mode at the seam. Two formats to maintain. Reliability + scalability lose to pure SpecStream. LLM ergonomics solved by JIT instructions anyway. | Never — pure SpecStream is strictly simpler. |
| **Keep current OpenUI Lang DSL end-to-end** | Bespoke parser/grammar maintenance forever. No industry tooling. Token-efficient but token cost isn't a constraint. Misses progressive render, stateful patches, multi-framework renderers, MCP Apps compat. | Never. The DSL was the right call 6 months ago when no standards existed; not now. |
| **Pre-built widget bundles (OpenAI Apps SDK pattern)** | One-tool ↔ one-widget rigidity caps creativity. Wrong for open-ended commerce composition. | Only as the deferred `@Widget(...)` escape valve, IF SpecStream composition genuinely can't express a canonical UI elegantly. High bar. |
| **Vercel AI SDK RSC (React Server Components from LLM)** | Vercel themselves paused it Q1 2026 — flickering on `.done()`, suspense crashes, quadratic data transfer. We dodged a bullet. | Never — Vercel walked away. |
| **MCP `_meta` injection for cart_id (golden insight recommendation)** | Pipecat MCP client doesn't expose `_meta` on the wire. We did argument injection instead — same semantic, different target. Forward-compatible: when proxy/UCP cutover lands, retarget rules from arg-keys to meta-keys with no template authoring change. | If a future proxy gives us `_meta` plumbing AND there's evidence it's better than arg injection. |
| **Three-channel envelope as a separate sprint item** | Subsumed by SpecStream — the JSONL stream IS the structuredContent channel. Building it separately would be duplicate work. | Never. |
| **Fine-tuned tiny healer model (v1)** | Deterministic rules cover the common cases; let's measure catch rate before investing in a model. | Deterministic catch rate < ~85% after Sprint 1 ships. |

---

## `agent_session_state` — substrate + leverage map

The per-session JSONB store added in migration 030 carries three keys today (`cart_id`, `checkout_url`, `policy_links`). Architecturally it's a substrate, not a feature: a server-side memory layer driven entirely by template `state_reducers` + `tool_arg_injection` rules (with `generators:` for synthesised values), with **zero vertical coupling in the Python runtime**.

Recording the leverage so future-us doesn't reinvent it. Every entry below is "1–3 reducer rules + maybe an injector + a system-prompt branch" — no Python.

### Engine properties that make this cheap

1. **JSONB is shapeless.** Adding `state.return_flow.step` or `state.preferences.size` needs no migration.
2. **Template-only wiring.** Each use is a few lines in `state_reducers` + `tool_arg_injection`; the engines (`apply_state_reducers`, `inject_tool_args`) stay vertical-blind.
3. **Composable with `generators:`.** `uuid_v4`, `uuid_v7`, `timestamp_iso8601`, `timestamp_unix_ms` work for any synthesised value (request ids, mandate ids, trace ids, idempotency keys — already used).
4. **Survives refresh, compaction, and provider swap.** Reliability properties accumulate per session without any per-vertical persistence work.

### Near-term — already implied by the UCP roadmap

| Future use | What gets captured | Trigger |
|---|---|---|
| **`customer_id`** | Identity-link tool response → `state.customer_id`; injected into every subsequent cart / order call | UCP `identity_linking` extension lands; or post-auth flow ships |
| **`order_id`** | Checkout-complete event → `state.order_id`; injected into WISMO / receipt / status follow-up tools | UCP `order` extension stable (replaces Nautilus WISMO) |
| **`discount_code` / `applied_discounts`** | Reducer captures applied codes from cart response; injector keeps them sticky on every `update_cart` | UCP `discount` extension lands |
| **`shipping_address_id`** | Captured at address-selection step; injected into subsequent checkout-step tools so the user doesn't reselect | Multi-step checkout flow gets built |
| **AP2 mandate ids** (`cart_mandate_id`, `payment_mandate_id`) | Generated client-side or returned from mandate creation; ride out-of-band, never in LLM prose | AP2 payment scope unlocks |

### Session preferences (sticky across turns + refresh)

| Future use | Why this table |
|---|---|
| **`locale` / `currency`** | Captured from first cart/product response or geolocation tool; injected into every subsequent catalog call so a USD user doesn't see INR mid-session |
| **`size_preference` / `color_preference` / `negative_constraints`** | LLM extracts "I'm a US 10, never leather" early via a synthetic `set_user_preferences` tool; injected as filters into every future `search_catalog`. Cures the "you forgot what I said two turns ago" complaint with no model changes. |
| **`a_b_variant_id`** | Session-sticky experiment bucket; consistent treatment across refresh |
| **`accepted_terms` / `marketing_opt_in`** | One T&C accept survives refresh; user not re-prompted |

### Workflow state machines (the underrated use)

For multi-step flows where the current step matters, the **template + reducer IS the FSM** — no Python state machine needed.

- **Returns / refunds**: `state.return_flow.step = "awaiting_reason"` → `"awaiting_photos"` → `"submitted"`. System prompt branches on the key.
- **Address verification**: `pending` → `verified` → `corrections_needed`.
- **Future verticals** (appointment booking, travel itinerary, etc.): `step = service → time → confirm → paid`. Each tool advances the state.

Pattern is identical to `cart_id` — runtime engine doesn't know what `return_flow.step` *means*; the template's reducers + injectors + prompt branching wire it up.

### Telemetry hooks (naturally feed Sprint 4 eval harness)

Once the eval harness lands (NP-1), these are the per-session signals worth scoring on:

- `tool_call_counts: {search_catalog: 4, get_product: 7, …}` — flag sessions that grind on browse without converting
- `last_intent: "browsing" | "comparing" | "checkout" | "support"` — slice quality by intent bucket
- `refusal_count` — bumped on every "I can't help with that"; high count → escalate
- `detected_pii: bool` — set on first credit-card / email mention; drives retention policy

### Safety + guardrails

- **Loop detection**: `state.loop_count` bumped when the same intent repeats; past a threshold, prompt branches to offer-help-different-way.
- **Refusal escalation**: 3rd refusal in a session → auto-emit a Handoff to human support.
- **Per-session friendly throttle**: not infra-level rate-limiting; the "you've asked me a lot, want a summary?" UX.

### Multi-surface continuity (voice ↔ chat)

`chat_session.voice_lead_id` already exists; `agent_session_state` is where cross-surface scratch lives:

- `voice_started_at`, `last_interrupt_at` — voice-specific telemetry
- `cards_shown: [tile_id, …]` — so the user voice-saying "the second one" disambiguates
- `pending_voice_confirm: {action, payload}` — captured before a voice-initiated action; consumed by the next yes/no turn

### A2A / cross-agent handoffs (deferred bucket — see NP-5)

When A2A federation becomes real:

- `state.a2a_session_id` — correlation id for handoff
- `state.handoff_context: {tool_history, identifiers, summary}` — the bundle to forward
- `state.federation_trace_id` — tracing across hops

### What this table is NOT for (worth saying)

- **Cross-session data** (user profile, purchase history) — belongs in customer-level tables. This row dies with the session.
- **High-write hot state** (every keystroke / token) — it's upsert-per-turn, not append-log. Use a separate audit table for that.
- **Facts the LLM needs to reason over** — those go in `content_blocks`. `agent_session_state` holds identifiers + flags the **runtime** threads on the LLM's behalf, not facts the LLM needs to weigh.

### Most valuable next single use

After the eval harness lands (NP-1), session-level **preference capture** (size / colour / negative constraints) is the highest-leverage 1-day addition. Two reducer rules + one prompt branch; visibly improves multi-turn shopping without any model change.

---

## Already shipped

### Sprint 1.8 — Widget polish wave (shipped 2026-05-21)

A multi-session pass to ready the panel for production. All of these landed in `loom/packages/breeze-buddy-assist-widget/` and `loom/packages/client-sdk/`; one cross-cutting Clairvoyance change (cancel-bus + UiAction.display in catalog) landed in `clairvoyance/app/api/routers/breeze_buddy/chat/` and `clairvoyance/app/ai/voice/agents/breeze_buddy/template/`.

**Server-side cancel for in-flight chat turns** — the widget Stop button now actually releases the per-session Redis lock instead of waiting on the 180s TTL. Multi-pod safe via Redis pubsub channel `breeze-buddy:chat:cancel`:
- `clairvoyance/app/api/routers/breeze_buddy/chat/cancel_bus.py` — NEW. Per-pod `Dict[session_id, asyncio.Task]` + Redis subscriber loop with exponential backoff. `register()` / `unregister()` / `cancel()` / `start_subscriber()` / `stop_subscriber()`.
- `chat/handlers.py` — `stream()` registers `asyncio.current_task()` on entry, unregisters in finally. Catches `CancelledError` → yields clean `turn_end: CANCELED`. Critical Python-3.11 detail: explicit `current_task.uncancel()` after the catch + `asyncio.shield(lock.release())` in finally to prevent re-cancellation losing the Redis `DEL`.
- New routes: `POST /chat/session/{id}/cancel` (admin auth) + `POST /widget/session/{id}/cancel` (widget-token auth). Both fire-and-forget 202.
- App lifespan in `main.py` starts + stops the subscriber.
- SDK `widget.ts:cancel()` fires `POST /cancel` alongside the local AbortController abort.

**TTFB-only thinking indicator** — `BbThinkingRow.svelte` (NEW) renders the buddy orb + a shimmering verb (one of 12 random: Thinking / Pondering / Cooking / Brewing / Mulling / ...) + sequential dots that build up (`.` → `..` → `...`) and reset. Tool-name override map for known tools (`search_products` → "Searching products"). Shown ONLY between user-send and first assistant token/UI op (predicate: `sending=true && last message is user`). No bubble chrome — chrome-less status row makes it visually distinguishable from messages.

**Dual-channel display for actions** — `UiAction.to_assistant` now accepts optional `display?: string`. LLM payload (`msg`) always reaches the backend with full GIDs/identifiers; user bubble shows `display` when present, or falls back to a client-side heuristic that maps `gid://shopify/<Type>/<id>` → "this <type>". Touched: `client-sdk/.../ui/types.ts`, `client-sdk/.../store/_buddy-chat-store.ts` (`send` accepts `displayText`), `widget/.../ui/UiNode.svelte`, `widget/.../ui/UiRenderer.svelte`, `widget/.../ui/primitives/Tile.svelte`, `widget/.../BuddyAssist.svelte` (new `humanizeForBubble()`). Backend: `clairvoyance/.../template/ui_catalog.py` `ToAssistantAction.display: Optional[str]` (extra="forbid" was rejecting). Template: `canonical.template.json` instructions for `search_catalog` / `get_product` / `create_cart` now include `display:` constructed from `product.title` / `line.item.title`.

**New-chat confirmation flow** — header `+` icon button to the left of close; click opens `BbConfirmSheet.svelte` (NEW — bottom-sheet, chosen over modal for narrow-panel readability), confirm calls `resetSession()` + `ensureSession()` for a fresh thread. `BbWindow.svelte` gained an optional `overlay` snippet so the sheet stays scoped to the panel.

**BbTurn segment-id stability fix** — the typewriter was re-typing previously-revealed text whenever the model called a tool that emits NO UI op (text → tool → text adjacent in blocks). Root cause: `buf.ids.join('+')` flipped segment key from `'asst_1'` to `'asst_1+asst_2'`, triggering keyed `{#each}` remount and `revealedLen` re-init. Fix: use `buf.ids[0]!` so the segment key is invariant across joins.

**Composer redesign** — send button moved INSIDE the input pill (single rounded shape), placeholder changed to "Message…", "buddy by Breeze" watermark above composer (subtle font + 0.28 opacity), panel background unified to `--panel-bg-2` (cream) end-to-end so no seam between chat body and footer, send icon flips to a stop square while `status==='sending'` (wired to the cancel flow above).

**Mascot launcher** — `BbBuddy.svelte` (pearly orb with pill eyes + lavender stroke border) and `BbSolidOrb.svelte` (oil-slick swirl variant, three lavender/magenta/coral blobs at reduced chroma + bright central wisp + heavy SVG film grain) — both consumed by `BbLauncher.svelte` which renders a configurable pill ("Talk to an Agent" or empty for bare orb) with a phone-mode size reduction at `<=480px`.

**Dev harness uplift** — `index.html` (dev harness) gained: scenario testing chips (TTFB / long-stream / heavy-carousel / text-only / tool-with-side-effects / 300+ char user-bubble), Launcher variants toggle (mascot↔solid, label on/off), "Open Use-Case Showcase →" link. `index-shopify.html` repointed from swaroop-juspay to Milton (same key as dev harness).

### Sprint 1.9 — Use-case showcase (shipped 2026-05-21)

`showcase.html` + `Showcase.svelte` (NEW, ~1200 lines) demonstrate the widget repurposed across 20 diverse verticals: Restaurant ordering · Healthcare booking · Insurance quote · EdTech tutor · Legal triage · Real estate · Travel itinerary · HR onboarding · Bank account opening · Telecom plan finder · Fitness coach · Recipe assistant · Event ticketing (StagePass) · SaaS onboarding · Government services · Mental health check-in · Pet adoption · Job application · Auto repair (Cascade Auto) · Hotel concierge.

Each preview renders the REAL widget chrome (imports `BbHeader`, `BbBuddy`, `BbAssistantTextBubble`, `UiRenderer`, `BbWatermark`, `BbComposer`) so demos look exactly like the production widget — not generic cards. Mock transport was retired (`createMockWidgetChatSession` throws), so previews use hand-authored `UiOp[]` arrays fed through the real renderer (Strategy B from the rebuild brief). Token scope: `.panel-scope` re-declares `--*` and `--openui-*` tokens that `tokens.css` normally scopes to `:host` since we render outside the Shadow DOM.

9+ distinct visual archetypes: text-only markdown (EdTech, Recipe) · image-dominant carousel (Restaurant, Real Estate, Pet Adoption) · table/comparison (Insurance, Telecom) · slot/picker grid in a Carousel (Healthcare, Event Ticketing seat tiers, Auto Repair bays) · checklist (HR) · form-flow step card (Bank, Government) · mood/chip selection (Mental Health, Fitness) · handoff-driven (Legal) · tile stack + handoff (Jobs) · narrative multi-section (Travel) · SaaS feature tour (SaaS onboarding).

Three slot-grid scenarios were initially rendered as `Row` and overflowed; switched to `Carousel` after first review for proper horizontal snap-scroll.

Image sources: `picsum.photos/seed/{seed}/{w}/{h}` for realistic photos; inline SVG data: URLs for decorative bits (SaaS feature screenshot). Replaced every dead `source.unsplash.com` URL.

Footer disclaimer notes previews are static; button actions fire into a no-op handler that flashes a hint pill ("Preview only — '…' would dispatch in a real session.").

Link from the dev harness landing: "Open Use-Case Showcase →" pill, top of `index.html`.

### Sprint 1.7 — Session resume on refresh (shipped + live-validated 2026-05-20)

Cross-cutting fix for the cart-orphaning bug discovered during the A6 refresh smoke. Before this sprint, a Cmd+R reload created a brand-new chat session, breaking the `agent_session_state` row's link to the Shopify cart_id — every refresh effectively dropped the cart on the floor (and visually wiped all prior Tiles). The widget now persists the session id + widget_token in `localStorage` (keyed by tenant + shop) and resumes the prior session via `GET /widget/session/{id}`, replaying both chat bubbles AND persisted UI ops.

| Surface | Change |
|---|---|
| `database/migrations/030_chat_session_persistence.sql` | Adds `ui_blocks JSONB` column on `chat_message` (alongside the `content_blocks` + `agent_session_state` additions consolidated into this single migration). Independent of `content_blocks` — the LLM never sees prior ui_ops on replay (by design). |
| `schemas/breeze_buddy/chat.py` | `ChatMessage.ui_blocks: Optional[List[Dict]]` added. |
| `database/{queries,decoder,accessor}/breeze_buddy/chat_session.py` | `_MESSAGE_COLUMNS` adds `ui_blocks`; `insert_chat_message_query` accepts `ui_blocks_json`; `decode_chat_message` parses it; `insert_chat_message` plumbs `ui_blocks` through. |
| `ai/voice/agents/breeze_buddy/chat/agent.py` | Per-cycle `turn_ui_ops: List[Dict]` accumulator. The stream loop captures each successful `ui_op` SSE event (post-healer, post-allowlist) into the list and passes it as `ui_blocks=turn_ui_ops or None` to both in-loop and final `insert_chat_message` calls. |
| Server resume endpoint | `GET /agent/voice/breeze-buddy/widget/session/{id}` already existed (returns `WidgetSessionStateResponse`) — now serves `ui_blocks` for free via the schema/decoder changes. **No new endpoint needed.** |
| `loom/.../client-sdk/src/lib/chat/widget.ts` | localStorage helpers (`resumeStorageKey`, `readStoredSession`, `writeStoredSession`, `clearStoredSession`). Resume probe runs BEFORE the create flow: if a non-expired stored entry exists, do `GET /widget/session/{id}` with the stored bearer; on 200 adopt the sessionId+widgetToken, backfill SDK `transcripts`, and **schedule a `setTimeout(0)` emit of the `resumed` event** so consumers attaching via `session.on('resumed', …)` after the factory promise resolves still receive it (queueMicrotask would have fired before the consumer's `.then()` ran — load-bearing distinction discovered during A6 smoke). On any failure (404/401/network/JSON parse) the storage entry is dropped and we fall through to a normal `POST /widget/session`. After a successful create, the new session is written to storage with `expiresAt = Date.now() + ttl_seconds*1000`. Storage is cleared on `end()`, on `410` idle-timeout, and on `410` already-ended. |
| `loom/.../client-sdk/src/lib/chat/types.ts` | New `resumed` event added to `WidgetChatSessionEventMap`; new `ResumedSessionDetails` + `ResumedMessage` types. |
| `loom/.../client-sdk/src/lib/store/_buddy-chat-store.ts` | `attach()` registers `session.on('resumed', …)` that REPLACES `state.messages` with a fresh array built from the resume payload — one `ChatTextMessage` per user content, one per assistant content, plus one `ChatUiMessage` for any assistant turn with persisted `ui_blocks`. The existing snapshot-transcript hydration (assistant-only) is harmless because the replay-replace runs after it. |
| Widget package | **No code changes.** The widget's `BbUiPane` already consumes `ChatUiMessage.ops` via the same `applyOp` pipeline the live SSE stream uses, so replayed ops repaint Tiles/Carousels/cart views identically to live. |

**Architectural property gained**: refresh becomes lossless. Cart-id continuity, chat history, and rendered UI all survive a page reload — and a tomorrow-morning reload (24h widget_token TTL). No `agent_session_state` rows get orphaned; one user = one shopify cart per session lifetime.

**Verification**: A6 smoke passes — fresh session → 2 user prompts → refresh → GET 200 hits `/widget/session/<id>`, both user + assistant bubbles repaint, localStorage carries `{sessionId, widgetToken, expiresAt}` keyed `bb:chat:session:v1:<baseUrl>|<widgetKey>|<shopUrl>`. Network tab confirms POST /session NOT called on the refreshed mount.

**Notable trap recorded for future-us**: `queueMicrotask` is NOT sufficient when the factory is itself an async function — its returned promise's `.then` is ALSO a microtask, scheduled AFTER the body's `queueMicrotask`. The emit fires before the consumer's `.then` runs and the event is silently lost. `setTimeout(handler, 0)` (a macrotask) is the right defer primitive here. See widget.ts comments around line 595 for the full rationale.

### Sprint 1.6 — Commerce-scrub (shipped 2026-05-19, live-validated 2026-05-20)

**Smoke results (rev 7)**: A1 (snowboard carousel), A2 (no drops/heals), A3 (product detail Tile after JIT root-id fix), A4 (cart-id reuse + row layout + thumbnails + trailing slot), A5 (skip_ui FAQ), A6 (refresh persistence — covered by Sprint 1.7 below), B1 (Milton catalog — numeric tag filter + empty alt handled by LLM/JIT, no healer needed), B2 (Milton cart flow — cart-amount transforms added during B2 to fix `₹316.0` → `₹316.00` formatting) all green. Two carts in DB confirm session-scoped cart-id reuse across `update_cart` + `get_cart`.

**Post-smoke template patch**: added 8 `tool_response_transforms` entries (4 each for `update_cart` + `get_cart`) covering `cart.cost.total_amount`, `cart.cost.subtotal_amount`, `cart.lines[*].cost.total_amount`, `cart.lines[*].cost.subtotal_amount`. UCP returns cart amounts as decimal-already strings (e.g. `"316.0"`); the transform's "decimal-string reformatting" path normalises them to `"316.00"` with thousands separators (`"1,029.00"` for the DUO DLX 1000 ml). Originally the template only scaled `search_catalog` / `get_product_details` paths.



User-audit-driven cleanup: the runtime carried more commerce vocabulary than the agreed principle "runtime commerce-agnostic; commerce in template JSON only" allows. This sprint scrubbed the runtime to genuine zero-commerce. No new features; pure architectural cleanup.

| Surface | Change |
|---|---|
| `template/ui_catalog.py` | **Money primitive deleted.** Class removed; dropped from `UI_CATALOG`, `PRIMITIVE_GROUPS["core"]`, `PRIMITIVE_RENDER_ORDER`, `__all__`. `TileBodyKind.money` enum value + `TileBodyItem.money` field also removed (Tile body is now `text`/`key_value`/`message`). |
| `template/ui_catalog.py` | **`MessageResolution` enum renamed**: `requires_buyer_input` → `requires_user_input`; `requires_buyer_review` → `requires_user_confirmation` (also drops "review" in favour of the more accurate "confirmation" since the rendered affordance is a confirm CTA). |
| `chat/ui_healer.py` | **Two Money-specific rules deleted** (`_rule_money_infer_currency`, `_rule_money_coerce_string_amount`). ISO 4217 `_CURRENCY_EXPONENT` table + `_exponent_for` helper removed. `_PROP_ALIASES` entries for Money (`value→amount`, `price→amount`) gone. `HealerContext.default_currency` kwarg removed. |
| `handlers/transport/utils/response_transform.py` | **`scale_money_amount` → `scale_by_exponent`**. Pure arithmetic now: takes an explicit `{exponent: int}` arg, no ISO 4217 lookup, no currency knowledge. Templates declare the exponent for their data source. `_ZERO_DECIMAL_CURRENCIES` / `_THREE_DECIMAL_CURRENCIES` / `_minor_unit_scale` all deleted. |
| `template/ui_prompt.py` | Dropped Money from `_EXAMPLES`. Genericised remaining example messages (`"Tell me more about <id>"` etc.). Updated docstring refs from Money → Message in nested-render examples. Footer composition rules drop the "never compose Card+Image+Text+Money" advice (no Money to compose). |
| `chat/ui_stream.py`, `chat/tool_result_normalizer.py` | Docstring sweep — removed Money examples; updated "vertical-agnostic" framing for normalizer. |
| `template/types.py` | Updated `ResponseTransform` docstring to reference `scale_by_exponent`. |
| `static/buddy-assist-agent/canonical.template.json` | `tool_response_transforms` now use `scale_by_exponent` + `{exponent: 2}` args. All 4 `tool_ui_instructions` (search_catalog, get_product_details, update_cart, get_cart) rewritten — no Money emission. Price/total rendered as `key_value` body rows with `"₹" + amount` from the upstream-scaled value. Cart total moved into `CardHeader.subtitle` (was a standalone Money op). |
| `packages/breeze-buddy-assist-widget/src/lib/ui/primitives/Money.svelte` | **Deleted.** |
| `packages/breeze-buddy-assist-widget/src/lib/ui/UiNode.svelte` | Money import + Money dispatch branch removed. `TileNarrowedBodyItem` union narrowed to drop `money` variant. `TileMessageResolution` renamed buyer→user. `narrowBody` simplified (one fewer kind). |
| `packages/breeze-buddy-assist-widget/src/lib/ui/primitives/Tile.svelte` | `TileMoneyPayload` interface removed. `TileBodyKind` narrowed to `text`/`key_value`/`message`. Money import + body switch case dropped. `TileMessageResolution` renamed. |
| `packages/breeze-buddy-assist-widget/src/lib/ui/primitives/Message.svelte` | `Resolution` type renamed; docstring updated. |
| Tests | `tests/test_response_transform.py` rewritten for `scale_by_exponent` API (currency-aware tests replaced with exponent-aware). Money-specific tests removed from `test_ui_stream.py` / `test_ui_healer.py` / `test_tile_validation.py`. `test_ui_prompt.py` swapped Money→Tag in allowlist tests. `test_ui_catalog_groups.py` added explicit assertion that Money is no longer registered. **All 133 tests pass.** |

**Verification done**: 133/133 pytest pass, widget `pnpm build` succeeds (175.45 kB / 57.03 kB gzip, +0 errors), `svelte-check` reports 0 errors / 0 warnings.

**Verification pending** (needs user-owned clairvoyance + re-provision):
- Live smoke on both merchants — confirm `key_value` price rendering looks visually equivalent to the old Money chip.
- Confirm no `props_validation_failed` events for stale `kind:"money"` body rows from cached prompts.
- Confirm `requires_user_input` / `requires_user_confirmation` flow once a Message-with-resolution arrives (Sprint 3 form work depends on this).

**Net delta**: ~250 LoC removed, 0 LoC added on architectural surface (catalog/healer/builder/transform); template + widget refactored for the simpler primitive set. Architectural property gained: **runtime is now formally vertical-agnostic** — same code can power non-commerce verticals without any catalog edit.

### Sprint 1 — SpecStream + healer + JIT instructions (live-validated 2026-05-19)

The OpenUI Lang DSL is deleted across all 3 repos. Replaced with SpecStream/A2UI JSONL ops + in-stream healer + Sidekick-pattern JIT UI instructions in tool response envelopes.

| Layer | Files | What ships |
|---|---|---|
| **Server: catalog** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/ui_catalog.py` | 14 Pydantic primitives (Stack, Row, Card, CardHeader, Image, Text, Carousel, Tag, Button, Buttons, Table + new typed Money, Message, Handoff). `UI_CATALOG: Dict[str, Type[BaseModel]]`. |
| **Server: wire format** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_stream.py` | `UiStreamExtractor` (replaces UiMarkerExtractor) — `<ui_stream>` marker FSM yielding per-line JSONL ops. `parse_op_line` validates against catalog. `process_op_line` runs (healer → parse → SSE-emit) per line. `strip_ui_stream_markers` for persistence. |
| **Server: healer** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_healer.py` | Deterministic rule layer; runs before validation. 7 transform rules (incl. `_PROP_ALIASES` rename, unknown-props strip, Money currency inference, Money amount coercion, Button default label, Tag array-flatten, id dedupe) + 2 drop rules (unknown type, orphan add). Emits `healer_applied` SSE events. |
| **Server: JIT instructions** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/types.py` + `mcp/__init__.py` | `ToolUiHint` Pydantic model on `McpServerConfig.tool_ui_instructions`. `_maybe_inject_ui_instructions` splices `_ui_instructions` / `_ui_examples` / `_ui_skip` into tool result envelopes (Sidekick pattern). |
| **Server: chat agent** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/agent.py` | `UiStreamExtractor` replaces `UiMarkerExtractor`; per-line ops fan out to SSE; session-wide `_known_ui_ids` set for dedupe. Markers stripped before persistence so replay history is prose-only. |
| **Server: deletions** | `chat/ui_emit.py`, `ui_marker.py`, `ui_parser.py`, `ui_resolver.py`, `ui_prompt.py` (all gone); 5 corresponding test files deleted. | OpenUI Lang DSL fully retired server-side. |
| **Widget: store** | `nautilus/packages/breeze-buddy-assist-widget/src/lib/ui/ui_state.svelte.ts` | Svelte 5 `$state` tree store. `applyOp` (add/replace/remove), `getNode`, `reset`. Recursive subtree removal. One store per UI block, mounted per assistant turn by `BbUiPane`. |
| **Widget: renderer** | `src/lib/ui/UiRenderer.svelte`, `UiNode.svelte` | Stateful patch-applier. Walks tree from `rootId`; dispatches by `type` to primitive component; maps catalog props → existing primitive props (gap tokens, variant names, etc). |
| **Widget: new primitives** | `src/lib/ui/primitives/Money.svelte`, `Message.svelte`, `Handoff.svelte` | Money uses `Intl.NumberFormat` with ISO 4217 exponent table (handles JPY zero-decimal, KWD three-decimal). Message has severity + resolution → fixed affordance. Handoff is typed CTA with lifecycle (popup/same_tab/polling). |
| **Widget: turn integration** | `src/lib/components/BbUiPane.svelte`, `BbTurn.svelte`, `BbTextPane.svelte`, `BuddyAssist.svelte` | BbUiPane owns a per-block UiStore + an `appliedCount` watermark — applies only new ops as they stream in, never re-applies. Plumbs `UiAction` (not `ParsedAction`). |
| **Widget: deletions** | `src/lib/ui/_evaluator.ts`, all 3 stale smoke tests | DSL evaluator gone. |
| **SDK** | `~/Repos/loom/packages/client-sdk/` v0.7.0 | `_lang-core/` directory deleted (~3kLOC vendored parser). `_parse.ts`, `_action-bus.ts`, `_evaluator.ts`, `primitives/`, `UiRenderer.svelte`, `UiNode.svelte` all gone. `ui/types.ts` shrunk to 3 exports: `UiOp`, `UiAction`, `UI_CATALOG_VERSION`. `_turn-engine.ts` handles `ui_op` / `healer_applied` / `ui_op_dropped` SSE events. `chat/types.ts` `ChatUiMessage` shape: `{id, kind:'ui', role, ops: UiOp[], createdAt}`. `store/_buddy-chat-store.ts` accumulates ops per-turn into an active live array. `mock/index.ts` stubbed (throws `NotImplementedError`; SpecStream mock returns in Sprint 4). |
| **Template** | `nautilus/static/buddy-assist-agent/canonical.template.json` | System prompt's UI section <300 chars; declares SpecStream op shapes; no DSL grammar. `tool_ui_instructions` map for `search_catalog` / `get_product_details` / `update_cart` / `get_cart` / `search_shop_policies_and_faqs` (last one `trigger: skip_ui`). `gen_ui` block removed. `max_tokens: 16384` (was 1024 — bumped after first live run showed JSONL truncation). |
| **Tests** | `clones/clairvoyance/tests/{test_ui_stream,test_ui_healer,test_jit_instructions}.py` | 67 tests pass (23 ui_stream + 19 healer + 6 JIT + 19 session_state). SDK: 50 tests pass after `tsc --noEmit` clean. Widget: svelte-check 0 errors / 0 warnings. |

### Sprint 1 live-validation status

First real-traffic smoke ran 2026-05-19 against `swaroop-juspay.myshopify.com` with Claude Sonnet 4.6 via Vertex.

| Scenario | Status | Evidence |
|---|---|---|
| Product search → carousel of cards | ✅ validated (pre-Tile) | Real SSE stream emitted `ui_op` events with stable ids (`c-<product.id>`, `c-<product.id>-img|title|price|btn`), Money with minor-unit int + INR currency, Button action `{type:"to_assistant",msg:"Tell me about gid://shopify/Product/..."}`. Surfaced card-uniformity issue → fixed in Sprint 1.5 via Tile composite. |
| JIT instructions in tool response | ✅ validated | `function_call_completed.result_summary` carries `_ui_instructions` verbatim — LLM grounded card layout on it. |
| In-stream healer | ✅ validated | `healer_applied: renamed_alias_props:Tag:label->text` fires per Tag op in real traffic. |
| Block persistence + replay | ✅ validated (indirect) | Next-turn LLM context shows prior `tool_use` + `tool_result` blocks in canonical Anthropic shape — meaning the codec round-trips correctly. |
| **Tile renders uniformly (Sprint 1.5)** | ⬜ pending Milton/swaroop restart smoke | Expected: ONE `ui_op` with `type:"Tile"` per product (vs prior 5-6 child ops), every Tile pixel-uniform by construction. |
| **System prompt has rendered primitives section (Sprint 1.5)** | ⬜ pending restart | `template/ui_prompt.py::render_primitives_section` should produce a section with Tile + Carousel + core primitives; LLM gets it via builder splice. |
| **`primitive_disabled:<type>` telemetry (Sprint 1.5)** | ⬜ pending — needs a deliberate-failure test | Disable e.g. `Table` via `disabled_primitives`; emit a Table op; confirm `ui_op_dropped` with reason `primitive_disabled:Table` (not `unknown_type`). |
| Cart-id reuse (the OG fix) | ⬜ not yet exercised | Needs "add to cart" + "add another" flow. ~2 min to test. |
| `skip_ui` trigger (FAQ prompt) | ⬜ not yet exercised | "what's your return policy?" should yield zero `ui_op` events. ~1 min. |
| Refresh-page persistence | ⬜ not yet exercised | Refresh mid-conversation; prior turns' cards repaint from history. ~1 min. |
| Cross-provider (Anthropic → OpenAI) | ⬜ not yet exercised | Template swap `llm_configurations.sdk:"anthropic"` → `"openai"`, re-provision, retry. Proves block-codec neutrality. ~5 min. |
| **Second-merchant smoke (Milton)** | ⬜ in progress | milton-india-store provisioned; widget dev harness pointed at it. Anticipated quirks documented in §"Real-traffic learnings" #4 (numeric tags, empty alt_text). |

### Real-traffic learnings (post-ship telemetry-driven tweaks)

The first live smoke surfaced two issues that needed an immediate fix rather than waiting for the consolidated e2e pass. Recording the pattern so future post-ship checks know what to look for:

1. **LLM emits Tag with `label` instead of `text`.** The catalog's Tag schema requires `text`; LLM consistently reaches for `label` (the prop name on Button). Healer's "strip unknowns" rule stripped `label`, leaving Tag with no `text`, which then failed Pydantic validation → op dropped → no chip rendered. Fix: added `_rule_rename_prop_aliases` BEFORE the strip step, with a `_PROP_ALIASES: Dict[(type, alias), canonical]` table. Currently covers:
   - `Tag`: `label`/`name`/`value` → `text`
   - `Text`: `content`/`value` → `text` (legacy DSL prop name)
   - `Button`: `title`/`text` → `label`
   - `Image`: `url`/`image` → `src`; `title` → `alt`
   - `Money`: `value`/`price` → `amount`
   - `CardHeader`: `heading`/`header` → `title`

   Semantics: rename only when the canonical key is empty (LLM intent wins). Extend the table as new `ui_op_dropped` events surface different near-misses.

2. **`max_tokens` undersized for JSONL.** Template default was `1024`, which truncated a 10-product carousel mid-emission (`stream ended inside <ui_stream> block (dropping 136 chars)`). JSONL is ~3× more tokens than the prior DSL would have been. Bumped to `16384` (~50 cards' worth of ops + prose headroom). Claude Sonnet 4.6 supports up to 64k output tokens; `32768` or `65536` are the natural next breakpoints if a single reply ever needs to express more than ~50 cards. Token cost is not a constraint per user direction.

3. **LLM-composed cards drift in completeness.** Screenshots from the swaroop-juspay smoke showed Card subtrees with inconsistent children — some cards rendered Image+Title+Price+Button, some only Image, some only Text. The Card primitive's flex layout was sound; the failure was upstream — the LLM emitted partial card subtrees in the same Carousel. Fix shipped in Sprint 1.5: the **composite `Tile` primitive replaces hand-composed Card+children for list items**. Renderer owns the layout; LLM fills typed slots; missing slots simply don't render (no awkward empty space). One `ui_op` per Tile vs the prior 6-7 per card. See §"Sprint 1.5" ship report.

4. **Milton-specific data quirks (anticipated, to confirm in live smoke).** Second-merchant provisioning surfaced two product-data shapes that may need healer extensions:
   - **Numeric tags**: Milton products have `tags: ["1008", "5019", "6010"]` instead of human-readable labels. JIT instruction tells the LLM to "skip internal tags (BLOCK_COD, SKU patterns, vendor handles)" but pure-numeric strings aren't in that list. If the LLM emits numeric chips, extend the JIT filter or add a healer rule: `Tile.attributes` items whose `label` matches `^\d+$` get dropped.
   - **Empty `alt_text`**: Milton's `media[0].alt_text` is `""`. The Tile schema requires `media.alt` non-empty (Pydantic `min_length=1`). The JIT explicitly maps `alt: product.title` so the LLM should fall back to title; if it instead pulls the empty `alt_text` directly, the Tile drops with `props_validation_failed`. Watch `ui_op_dropped` events on the first Milton smoke; if hit, add a healer rule that defaults `Tile.media.alt` to the Tile's `title` when empty.

### Sprint 1.5 — Composite `Tile` + template-driven primitive groups (shipped 2026-05-19)

Polish layer over Sprint 1. Triggered by first-live-smoke observation: LLM-composed Card+children subtrees produced visually inconsistent cards (some half-emitted, some missing slots). The fix: replace hand-composed list items with a single composite primitive (`Tile`) whose layout the renderer owns. To keep the catalog extensible (graphs/metrics/forms in future), partitioned all primitives into named groups with per-template enable/disable.

| Layer | Files | What ships |
|---|---|---|
| **Catalog restructure** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/ui_catalog.py` | `PRIMITIVE_GROUPS: Dict[str, List[str]]` — 7 groups (`core`/`composite`/`graphs`/`metrics`/`forms`/`media`/`data`). `core` has the 14 Sprint-1 primitives; `composite` has `Tile`; future groups are empty placeholders for the extension pattern. `PRIMITIVE_RENDER_ORDER` controls the system-prompt rendering order (composite-first so LLM defaults to Tile for list items). Helpers: `group_for(name)`, `is_known_type(name)`, `resolve_allowlist(enabled_groups, enabled_primitives, disabled_primitives) → Set[str]`. |
| **Tile schema** | same file | `Tile` Pydantic model with slots: `media?: TileMedia`, `eyebrow?`, `title*`, `subtitle?`, `body: List[TileBodyItem] = []` (polymorphic `kind: text\|money\|key_value\|message`), `attributes: List[TileAttribute] = []`, `actions: List[TileAction] = []`, `density: "compact"\|"default"\|"spacious" = "default"`. Reuses existing `Money`, `Message`, `ActionUnion` for nested validation. |
| **Template config** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/types.py` | New `UiCatalogConfig` Pydantic model wired onto `ConfigurationModel.ui_catalog: Optional[UiCatalogConfig]`. Fields: `enabled_groups: List[str] = ["core"]`, `enabled_primitives: List[str] = []`, `disabled_primitives: List[str] = []`. Absent config defaults to enabling just `core` — backward compatible with pre-Tile templates. |
| **Stream allowlist gate** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_stream.py` | `parse_op_line(line, *, allowlist=None)` and `process_op_line(..., allowlist=None)` accept the resolved allowlist set. Ops whose type is in catalog but not in allowlist drop with `error == "primitive_disabled:<Type>"` — distinct from `unknown_type` so telemetry separates "merchant turned off" from "LLM hallucinated". `allowlist=None` means no template-level filtering (all known catalog types pass). |
| **Agent allowlist resolution** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/agent.py` | `ChatAgent.__init__` reads `template.configurations.ui_catalog` and calls `resolve_allowlist(...)` once per turn, stashing the result on `self._ui_allowlist`. Pipes it into `process_op_line` (validation) AND into `flow_builder.build_flow_config(..., ui_allowlist=...)` (system prompt rendering). |
| **System-prompt primitives section** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/ui_prompt.py` (NEW) | `render_primitives_section(allowlist: Set[str]) -> str`. Introspects each enabled primitive's Pydantic `model_fields` to produce a Markdown section: name + 1-line purpose (from docstring) + prop schema with `*`/`?` required markers (handles `Literal[...]`, nested Pydantic models, `List[T]`, `Optional[T]`) + a hardcoded round-trip-validated example op. Walks `PRIMITIVE_RENDER_ORDER` so Tile appears first. Header + composition rules + action-shape reference appended. |
| **Builder splice** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/builder.py` | `FlowConfigBuilder.build_flow_config(template, *, ui_allowlist=None)` — added keyword arg without changing constructor signature. If the template's `system_prompt` (direct mode) or `task_messages`/`role_messages` (flow mode) contains the literal placeholder `{{ui_primitives_section}}`, it's replaced with the rendered section. `ui_allowlist=None` substitutes empty string (voice templates without the placeholder are unaffected). |
| **Widget Tile renderer** | `nautilus/packages/breeze-buddy-assist-widget/src/lib/ui/primitives/Tile.svelte` (NEW, 398 LOC) | Owns the entire layout: media (cover-fit, edge-bleed to card corners) → eyebrow (small-caps) → title (2-line clamped) → subtitle → polymorphic body rows (dispatched by kind: text→Text, money→Money, message→Message, key_value→key/value span pair) → attribute chips (mapped to Tag tone tokens) → action row (single Button stretched, multiple wrapped in Buttons group). Density tokens map to `--openui-gap-{s,m,l}` and tune padding. `min-height: 320px`, `min-width: 240px`, `max-width: 280px` for Carousel-context uniformity. |
| **Widget dispatch** | `nautilus/packages/breeze-buddy-assist-widget/src/lib/ui/UiNode.svelte` | Added Tile import + `{:else if node.type === 'Tile'}` branch. Full TS narrowing via `narrowMedia`/`narrowBody`/`narrowAttributes`/`narrowActions`/`narrowAction`/`narrowDensity` helpers — coerces `Record<string, unknown>` props into the Tile schema with proper discriminated unions. **No `any` types** per project rule. |
| **Template config** | `nautilus/static/buddy-assist-agent/canonical.template.json` | New `"ui_catalog": {"enabled_groups": ["core", "composite"]}` under `configurations`. `flow.system_prompt` gets the literal `{{ui_primitives_section}}` placeholder line. All 4 product-touching tool_ui_instructions (`search_catalog`/`get_product_details`/`update_cart`/`get_cart`) rewritten to emit Tiles: one Tile per product/line, slot mapping from MCP response to Tile slots, stable ids (`t-<product.id>`, `t-l-<line.id>`). FAQ stays `skip_ui`. |
| **Tests** | `clones/clairvoyance/tests/{test_ui_catalog_groups,test_tile_validation,test_primitive_disabled,test_ui_prompt}.py` | **139 tests pass total** (was 67 before Sprint 1.5). New: 11 ui_catalog_groups (resolve_allowlist precedence/edge cases, group_for) + 14 tile_validation (full happy path, polymorphic body kinds, required-title, extra-prop rejection) + 7 primitive_disabled (`primitive_disabled:<type>` reason distinct from `unknown_type`, allowlist=None passthrough, allowlist=set() disables all, remove/replace ops bypass gate) + 11 ui_prompt (filtering, render-order, slot-name presence, validated examples, header/footer composition rules). |
| **Widget build** | — | `pnpm check`: 0 errors / 0 warnings. `pnpm build`: 189 modules, `dist/assist.js` 177 kB / gzip 57.6 kB. |

**Live state (post-Sprint-1.5)**:
- swaroop-juspay merchant: template `595650c5-b3c1-4b68-a3e9-5f22630283e0`, public_widget_key `DKt-j-YE-qCZeNcUt_Fh2v4OQ9LjqbyreDjBi7Poe5s`
- **milton-india-store merchant**: template `760a45f5-68c3-4941-8a73-558758f366d0`, public_widget_key `LT_uwSgXxRBDfPxf529QaX9MfP1PRgkUKtkjr0mhp7o` (newly provisioned 2026-05-19 to test against a second real Shopify storefront)
- Both templates declare `ui_catalog: {enabled_groups: ["core", "composite"]}`, `max_tokens: 16384`, Tile-based `tool_ui_instructions`, `{{ui_primitives_section}}` placeholder in system_prompt
- Dev harness `nautilus/packages/breeze-buddy-assist-widget/index.html` currently points at milton (`tenant="LT_uwSg..."`, `shop="milton-india-store.myshopify.com"`). Swap back to swaroop by changing those two attributes.
- Redis caches (`bb:tpl:*`, `bb:mcp:tools:*`) invalidated after each re-provision

### Cart-id loss fix (hybrid: block persistence + session state + arg-injection)

Implements the cart-id loss fix from `poc/GOLDEN_INSIGHTS.md`. Three layers, all generic (commerce flavour lives in template JSON):

| Layer | Files | What it does |
|---|---|---|
| **Block persistence** | `clones/clairvoyance/app/database/migrations/030_chat_session_persistence.sql` (consolidated migration covering all three additions below), `app/database/queries/breeze_buddy/chat_session.py`, `app/database/accessor/breeze_buddy/chat_session.py`, `app/database/decoder/breeze_buddy/chat_session.py`, `app/schemas/breeze_buddy/chat.py`, `app/ai/voice/agents/breeze_buddy/chat/block_codec.py`, `app/ai/voice/agents/breeze_buddy/chat/agent.py`, `app/api/routers/breeze_buddy/chat/handlers.py` | Stop stripping `tool_use`/`tool_result` blocks; persist Anthropic-shape content arrays; reconstruct OpenAI-shape `LLMContextMessage` on replay. |
| **Generic session state** | `030_chat_session_persistence.sql` (same consolidated migration), plus the accessor/decoder/schema files above | `agent_session_state(chat_session_id PK, data JSONB, updated_at)`. Generic — no commerce columns. |
| **Reducer + arg-injection engines** | `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/session_state.py`, `app/ai/voice/agents/breeze_buddy/template/types.py` (new `StateReducer`, `ToolArgInjection`) | Declarative JMESPath rules in template config lift identifiers from tool results into state and inject them into subsequent tool args. |
| **Commerce config** | `nautilus/static/buddy-assist-agent/canonical.template.json` | The only commerce-aware code in the change. Declares: `update_cart`/`get_cart` → lift `cart.id` to `state.data.cart_id`; inject `state.data.cart_id` into `update_cart`/`get_cart` args if missing. |
| **Tests** | `clones/clairvoyance/tests/test_session_state.py` | 19 tests; end-to-end reproduces the original logs.txt failure. All pass. |

Consolidated migration `030_chat_session_persistence.sql` applied to dev DB (previously shipped as three separate files 030/031/032; squashed pre-release since the PR hadn't gone out). Template re-provisioned. Server restart was needed at the time.

Provider neutrality: confirmed — the on-disk Anthropic-shape blocks round-trip through our codec to OpenAI-shape `LLMContextMessage`, and Pipecat's per-provider adapters convert on the wire. Verified by reading `pipecat/adapters/services/anthropic_adapter.py:261-307` (converts OpenAI-shape tool_calls → Anthropic tool_use blocks; role:tool → tool_result inside user message) and `open_ai_adapter.py:162` (passes OpenAI shape through). Swapping Claude → GPT/Gemini is a template config edit (`llm_configurations.sdk: "anthropic"` → `"openai"`), no code change.

**Canonical template excerpt** (in `nautilus/static/buddy-assist-agent/canonical.template.json`) — the commerce-specific config that the runtime is otherwise agnostic to. Reference shape:

```jsonc
"configurations": {
  "state_reducers": [
    {
      "tool_name": "update_cart",
      "set_paths": {
        "cart_id": "cart.id",
        "checkout_url": "cart.checkout_url",
        "currency": "cart.cost.total_amount.currency"
      }
    },
    {
      "tool_name": "get_cart",
      "set_paths": {
        "cart_id": "cart.id",
        "checkout_url": "cart.checkout_url",
        "currency": "cart.cost.total_amount.currency"
      }
    }
  ],
  "tool_arg_injection": [
    {
      "tool_name": "update_cart",
      "set_paths": { "cart_id": "state.data.cart_id" }
    },
    {
      "tool_name": "get_cart",
      "set_paths": { "cart_id": "state.data.cart_id" }
    }
  ],
  "mcp": {
    "servers": [
      {
        "name": "shopify-storefront",
        "url": "https://{shop_url}/api/mcp",
        "auth": { "type": "none" },
        "tool_response_transforms": { /* scale_money_amount rules */ }
      }
    ]
  }
}
```

Sprint 1 (S1.3) extends this section with a top-level `tool_ui_instructions` map; Sprint 3 (S3.1) adds a second MCP server entry for `/api/ucp/mcp` with `included_tools` scoping.

### Architectural rationale for the cart-id fix (preserved for future-us)

The fix is hybrid because no single layer is sufficient. (a) Block persistence alone leaves compaction broken — when sessions outgrow context, summarization strips ids unless we explicitly preserve them. (b) Session state alone doesn't fix same-turn hallucination — the LLM still hallucinates if its own prior `tool_use.input` isn't in history. (c) Arg injection alone is the cleanest end state but needs upstream cooperation (a proxy or UCP cutover) we don't fully have. Together they form the architecture all major refs (`shop-chat-agent`, `claude-cookbooks`, `openai-apps-sdk`, `AP2`, `LangGraph`, `stripe-ai`, `ACP`) converged on.

We chose **argument injection** over MCP `_meta` injection because pipecat's MCPClient doesn't expose `_meta` on the wire today. Same semantic, different target. The engine is forward-compatible: when a proxy or UCP cutover lets us write `_meta`, retarget rules from arg-keys to meta-keys with no template-authoring change.

---

## Voice readiness audit (2026-05-21)

Recorded so we can pick voice up next sprint without re-discovering what's already wired vs what's missing. Net: text widget ships now; voice is one bounded sprint away.

### What's already in place (~70%)

**Backend** — `/widget/session/{id}/voice/connect` + `/widget/session/{id}/voice/end` routes are live; the chat ↔ voice handoff state machine flips `current_channel` on connect/end; Daily.co rooms are provisioned per session; the `lead_call_tracker` row carries chat history + `cart_id` + `agent_session_state` seed across the handoff so the voice agent inherits the chat's context. The Milton template declares `supported_channels: ["chat", "voice"]`. The voice runtime itself is the standalone Breeze Buddy voice product — Pipecat + Daily.co + the existing TTS/STT plumbing — reused, not rebuilt.

**SDK** — full `VoiceSession` type surface already exported: VAD events (`speech_started` / `speech_stopped`), TTS events (`tts_started` / `tts_stopped`), transcript events (`transcript_partial` / `transcript_final`), control (`mute` / `unmute` / `end`). No new types needed.

**Widget** — `modes` + `defaultMode` custom-element props already declared on `<breeze-buddy-assist>`. `BbComposer.voiceEnabled` prop exists and the voice-toggle button is rendered (currently hardcoded `false`). Mascot has `speaking` / `thinking` / `idle` animation states defined. The container is voice-aware in shape; just not wired.

### What's missing (~30%)

- **SDK**: `ChatSession.transferTo('voice')` is a `NotImplementedError` stub. ~120 LOC to wire — call `POST /voice/connect`, return a `VoiceSession` proxy that joins the Daily room.
- **SDK**: no `createWidgetVoiceSession()` factory for starting in voice mode directly. ~150 LOC.
- **Widget**: no `mode: 'text' | 'voice'` state on the panel. ~25 LOC.
- **Widget**: no `BbVoicePane.svelte` for the in-call UI (mute / end-call / waveform / transcript). ~250 LOC.
- **Widget**: pane swap + voice-live-conflict banner in `BuddyAssist.svelte` (one mode active at a time). ~70 LOC.

### Architectural drift

Very low. No new DB schema; no new transport (Daily + Pipecat as-is); no SDK refactor (the chat store stays unchanged and the voice session is a sibling, not a parent). One small product decision — **transcript-during-call vs minimal call-screen** — affects ~80 LOC of pane UI but not the architecture.

### Effort estimate

7–8 dev-days end-to-end:
- SDK wiring: 1.5–2 days
- Widget UI (pane + banner + transitions): 3–4 days
- Backend: ~0 days for happy path (routes already exist)
- Testing across happy path + drop + reconnect: 1.5 days
- Polish (animations, voice-toggle affordance, accessibility): 1.5 days

### Recommendation

Don't release voice in this cut. Ship the text widget; voice is a clean next-sprint item once the open product question (call-screen UX) is decided. The longer we sit on the half-wired surface without shipping, the more the chat-only widget's release timeline slips for an item that, by the spec audit, isn't holding any of the rest back.

---

## Sprint 1 — SpecStream migration + healer + JIT instructions (✅ SHIPPED 2026-05-19)

> **Status: shipped + live-validated.** Detailed delta lives under §"Already shipped → Sprint 1". The original plan below stays for posterity so future readers can see the design intent. Reference for what was actually built: §"Already shipped → Sprint 1" table. Reference for what was learned during the first live smoke: §"Real-traffic learnings".

The foundational sprint. Everything downstream depends on this landing cleanly.

### S1.1 — Replace OpenUI Lang DSL with SpecStream JSONL wire format

**Goal**: LLM emits a flat JSONL stream of JSON-patch operations against a typed component catalog. Widget applies patches incrementally to a stateful session-wide UI tree.

**Wire format** (mirrors A2UI v0.9 / Vercel json-render SpecStream):

```jsonl
{"op":"add","id":"root","type":"Stack"}
{"op":"add","id":"c1","type":"Card","parent":"root"}
{"op":"add","id":"c1-img","type":"Image","parent":"c1","props":{"src":"...","alt":"..."}}
{"op":"add","id":"c1-price","type":"Money","parent":"c1","props":{"amount":69995,"currency":"INR"}}
{"op":"replace","id":"c1-price","props":{"amount":59995,"currency":"INR"}}
{"op":"remove","id":"c2"}
```

**Files to delete** (clean cut, no living legacy):
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_parser.py`
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_resolver.py`
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_marker.py`
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_emit.py` (rewritten, not deleted)
- `clones/loom/packages/client-sdk/src/lib/ui/_lang-core/` (entire directory)
- `nautilus/packages/breeze-buddy-assist-widget/src/lib/ui/_evaluator.ts`

**Files to create / rewrite**:
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_stream.py` (NEW) — JSONL stream parser + emitter; validates each op against component catalog
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/ui_catalog.py` (NEW) — typed Pydantic schemas for each primitive
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/types.py` — extend with `UiCatalog`, `PrimitiveSpec` types
- `nautilus/packages/breeze-buddy-assist-widget/src/lib/ui/UiRenderer.svelte` — rewrite as stateful patch-applier (currently a stateless re-renderer)
- `nautilus/packages/breeze-buddy-assist-widget/src/lib/ui/ui_state.ts` (NEW) — Svelte 5 `$state` store for the session UI tree

**Primitives in v1 catalog** (port from current 11, plus 3 new typed):

| Primitive | Source | Notes |
|---|---|---|
| Stack, Row, Card, CardHeader | Existing | Layout |
| Image, Text, Carousel | Existing | Content |
| Button, Buttons | Existing | Actions |
| Table, Tag | Existing | Data display |
| Money | NEW | `{amount: int (minor units), currency: ISO 4217}` — never string-concat money again |
| Message | NEW | `{severity, resolution, content, param?}` — closed enum drives fixed UI affordance |
| Handoff | NEW | `{reason, label, url, lifecycle: popup\|same_tab\|polling}` — widening `@OpenUrl` |

**Catalog schema shape** — full Pydantic for each primitive, lives in `template/ui_catalog.py`. Sketch (target shapes for the 3 new typed primitives):

```python
class Money(BaseModel):
    """Render an amount with currency-aware formatting. The LLM emits minor-unit
    integers (paisa for INR, cents for USD); the widget formats using ISO 4217
    exponents. Three-decimal (KWD/OMR/BHD) and zero-decimal (JPY/KRW/VND) handled
    natively. NEVER use Text for money — the schema requires Money for amounts."""
    amount: int = Field(..., description="Amount in ISO 4217 minor units")
    currency: str = Field(..., description="ISO 4217 code (INR, USD, JPY, KWD…)")
    display_text: Optional[str] = Field(None, description="Seller-authored override (UCP/ACP pattern)")

class MessageSeverity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    success = "success"

class MessageResolution(str, Enum):
    recoverable = "recoverable"                  # silent retry; no UI
    requires_buyer_input = "requires_buyer_input"  # render inline form bound to `param`
    requires_buyer_review = "requires_buyer_review"  # render confirm CTA
    unrecoverable = "unrecoverable"              # render Handoff with continue_url

class Message(BaseModel):
    """Spec-driven notification. Severity + resolution mechanically drive UI."""
    severity: MessageSeverity
    resolution: MessageResolution
    content: str
    param: Optional[str] = Field(None, description="JSONPath into cart/checkout for input binding")

class HandoffLifecycle(str, Enum):
    popup = "popup"        # window.open + 10s token poll
    same_tab = "same_tab"  # location.href
    polling = "polling"    # widget shows shimmer + polls completion endpoint

class Handoff(BaseModel):
    """Typed handoff URL — replaces ad-hoc @OpenUrl. Carries reason + lifecycle."""
    reason: str   # "checkout" | "auth" | "3ds" | "compliance_review" | "escalation" | …
    label: str    # human-visible button text
    url: HttpUrl
    lifecycle: HandoffLifecycle
```

**Wire format comparison** (DSL → SpecStream, for posterity — so future-us understands why):

```
OpenUI Lang (deleted):                       SpecStream JSONL (new):
─────────────────────                        ────────────────────
products = search_catalog.products           {"op":"add","id":"root","type":"Carousel"}
root = Carousel(@Each(products, "p",         {"op":"add","id":"c1","type":"Card","parent":"root"}
  Card([                                     {"op":"add","id":"c1-img","type":"Image",
    Image(p.image, p.title),                  "parent":"c1","props":{"src":"...","alt":"..."}}
    Text(p.title),                           {"op":"add","id":"c1-title","type":"Text",
    Text("₹" + p.price),                      "parent":"c1","props":{"text":"Dawn snowboard"}}
    Button("Add to cart",                    {"op":"add","id":"c1-price","type":"Money",
      Action([@ToAssistant(...)]))            "parent":"c1","props":{"amount":69995,"currency":"INR"}}
  ])))                                       {"op":"add","id":"c1-btn","type":"Button",
                                              "parent":"c1","props":{"label":"Add to cart",
                                              "action":{"type":"to_assistant","msg":"add gid://...Dawn"}}}
                                             ... (one block per product)
```

Tradeoffs (as evaluated, for the record):
- SpecStream is ~30% more tokens (not a constraint per user)
- SpecStream wins on reliability (JSONL parsing bulletproof, schema-validates per-op), scalability (industry standard, multi-framework, MCP Apps compat), and **stateful** UI (patches against persistent tree → progressive render, edits, removals).
- DSL author surface elegance was the only DSL win; solved by JIT instructions giving the LLM rich per-tool examples without prompt bloat.

**Effort**: M+ (~2 weeks). ~600 server LOC + ~400 widget LOC + tests + prompt migration.

**Acceptance criteria**:
- LLM emits JSONL successfully against canonical product / cart / FAQ prompts
- Widget renders Money/Message/Handoff correctly across INR, JPY (zero-decimal), KWD (three-decimal)
- Old DSL files deleted; no references remain in any `import`/`require`
- 19 existing session_state tests still pass (cart-id fix unaffected)

### S1.2 — In-stream healer (deterministic v1)

**Goal**: Catch the 5-10 most common LLM emission mistakes mid-stream before they reach the renderer. <250ms latency, no extra LLM call.

**Scope (v1 — deterministic only, per user direction)**:

| Rule | Input (broken) | Output (healed) |
|---|---|---|
| Unknown `type` | `{"op":"add","id":"x","type":"ProductCarousel","parent":"root"}` | Reject op, log warning. (Type wasn't in catalog → likely LLM hallucination.) |
| Missing `parent` on non-root op | `{"op":"add","id":"x","type":"Card"}` | Drop op, log. Root ops are exempt (no `parent` required). |
| Unknown primitive props | `{"op":"add","id":"m1","type":"Money","props":{"amount":99,"currency":"INR","tone":"red"}}` | Strip `tone`, keep `amount`+`currency`. Log stripped key. |
| Money missing `currency` | `{"op":"add","id":"m1","type":"Money","props":{"amount":69995}}` | Infer from `agent_session_state.data.currency` (set by reducer). Fallback to merchant default. |
| Action missing `label` | `{"op":"add","id":"b1","type":"Button","props":{"action":{...}}}` | Default to "Continue" or intent-derived label. |
| Duplicate `id` in same session | `{"op":"add","id":"c1",...}` after `c1` already exists | Rename to `c1__2`, log. (Or `replace` if same parent+type — heuristic.) |
| Malformed JSONL line | `{"op":"add","id":"x"` (truncated) | Skip line, continue stream. |
| Money string instead of int | `{"props":{"amount":"₹699.95"}}` | Parse → 69995 (using INR exponent). Log. |
| Tag with raw JSON array | `{"type":"Tag","props":{"text":"[\"a\",\"b\"]"}}` | Emit one Tag op per array element. |

Each rule = a single function in `ui_healer.py` with one unit test. Healer is a synchronous pure function: `(jsonl_line, session_state) → Optional[jsonl_line]`. Emits `healer_applied` SSE event per fix for observability.

**Files to create**:
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_healer.py` (NEW) — deterministic rule engine, ~200-300 LOC
- `clones/clairvoyance/tests/test_ui_healer.py` (NEW) — one test per rule

**Out of scope for v1**:
- Tiny fine-tuned model (vercel-autofixer-01 style) — deferred until we measure deterministic catch rate
- Semantic fixes ("this card looks bad") — not addressable by healer; that's a prompt-quality concern

**Effort**: S (~3 days).

**Acceptance criteria**:
- Each rule has a unit test that demonstrates the broken input → healed output
- Healer integrated into ui_stream pipeline; emits a `healer_applied` SSE event per fix (for observability)

### S1.3 — JIT (Just-in-Time) UI instructions in tool responses

**Goal**: Per-tool UI guidance ships in the tool's response, not the system prompt. Solves prompt bloat without a subagent.

**Mechanism**: extend the existing `response_transforms` engine on `McpServerConfig` with a sibling `tool_ui_instructions` map. Engine appends the matching tool's instructions to the tool's result envelope before the LLM sees it. Per-merchant overrides via template config.

Full shape (as it would land in `canonical.template.json`):

```jsonc
"tool_ui_instructions": {
  "search_catalog": {
    "trigger": "on_success",
    "instructions": "Render results as a Carousel of Cards. Skip if products is empty (let prose handle no-results message). Each card must include: Image (src + alt from product.image), Text (title), Money (amount + currency from price_range.min), 1-2 Tag chips from top differentiating attributes (skip internal tags like BLOCK_COD, SKU patterns, vendor handles). Button 'View' with @ToAssistant('tell me about ' + p.id). Aim for visual polish — multiple Tags, clear hierarchy, image-leading. NEVER inline product data as string literals.",
    "examples": [
      {
        "scenario": "3 products, INR",
        "input_sketch": "{products: [{id, title, price_range, image, tags}, …]}",
        "expected_jsonl": [
          {"op":"add","id":"root","type":"Carousel"},
          {"op":"add","id":"c1","type":"Card","parent":"root"},
          "..."
        ]
      }
    ]
  },
  "update_cart": {
    "trigger": "on_success",
    "instructions": "Replace the cart card with the updated state. Show line items (Image, Text title, Tag for variant, Money for line subtotal), then a CardHeader 'Total' with Money for cart.cost.total_amount, then a Button 'Checkout' as Handoff{lifecycle:popup, url:cart.checkout_url}.",
    "examples": []
  },
  "search_shop_policies_and_faqs": {
    "trigger": "skip_ui",
    "instructions": "No UI for FAQ answers — let prose handle. Don't emit a ui_stream tool call."
  }
}
```

`trigger` values: `on_success` (only on 2xx tool results), `on_any` (success or error), `skip_ui` (suppress UI emission for this tool's results entirely).

**Files to touch**:
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/types.py` — add `tool_ui_instructions: Dict[str, ToolUiHint]` on `McpServerConfig`
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/mcp/__init__.py` — when a tool result returns, append JIT UI instructions to the result envelope BEFORE handing to LLM
- `nautilus/static/buddy-assist-agent/canonical.template.json` — declare per-tool UI hints
- System prompt: shrink the "## Generative UI" section dramatically; just declare "Use ui_stream tool to emit UI; tool responses will carry per-result guidance."

**Effort**: S (~3 days). ~100 server LOC + template JSON.

**Acceptance criteria**:
- System prompt's UI section reduced from ~1200 chars to <300 chars
- Tool result for `search_catalog` carries the rendering hint
- LLM emits richer product cards than today (verified manually + via golden screenshots in Sprint 4)

### S1.4 — Update system prompt for SpecStream emission

**Goal**: Teach the LLM the new emission protocol with examples per scenario.

**Files to touch**:
- `nautilus/static/buddy-assist-agent/canonical.template.json` — system_prompt + new "## UI emission" section with 5-10 worked examples
- Per-merchant template overrides ship their own `tool_ui_instructions` via the JIT mechanism

**Effort**: S (~2 days, mostly prompt iteration).

**Acceptance criteria**:
- 90%+ success rate on a 20-prompt golden set (manual eval; CI eval comes in Sprint 4)

### Sprint 1 sequencing

```
Week 1:  S1.1 wire format design + Pydantic catalog + Svelte renderer skeleton
Week 2:  S1.1 finish (12 primitives end-to-end), S1.2 healer in parallel
Week 3:  S1.3 JIT, S1.4 prompt migration, integration smoke
```

**Total**: 4 deliverables, ~3 weeks. S1.1 is the load-bearing one; others stack on top.

---

## Sprint 2 — Voice parity + cross-host + iteration tool (~2 weeks)

Lower-risk follow-on. Each item is independent.

### S2.1 — Voice/chat UI parity (same SpecStream over RTVI + SSE)

**Goal**: Cards visible during voice calls. Same patch-applier widget mounts on both surfaces.

**Files to touch**:
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_stream.py` — fan out SpecStream JSONL to BOTH SSE (chat) and RTVI server-message (voice)
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/handlers/transport/rtvi.py` — extend `SseRtviForwarder` to carry JSONL ops
- `loom/packages/breeze-buddy-assist-widget/src/lib/BuddyAssist.svelte` — subscribe to RTVI server messages, route to the same `ui_state` store as SSE

**Effort**: S (~1 week). ~150 LOC.

**Acceptance criteria**: voice-initiate, ask for snowboards, cards stream into widget during the call. Voice-end, return to chat, cards persist.

### S2.2 — MCP Apps emission wrapper (cross-host distribution)

**Goal**: Free distribution to Claude/ChatGPT/Goose/VS Code by emitting MCP Apps format alongside our native SpecStream.

**Mechanism**: a tool response can include an additional `text/html;profile=mcp-app` resource that wraps the SpecStream output in a sandboxed iframe stub. Hosts that speak MCP Apps render the iframe; our native widget ignores it.

**Files to touch**:
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/mcp_apps_wrapper.py` (NEW) — wrap SpecStream JSONL → HTML iframe stub per MCP Apps spec
- Tool dispatcher — opt-in flag per merchant template

**Effort**: S (~3 days). ~150 LOC.

**Acceptance criteria**: emit a sample tool response with both native + MCP Apps resources; validate against MCP Apps spec.

### S2.3 — `refine_ui(card_id, intent)` tool

**Goal**: Iteration flows — "make this card more compact", "show me a comparison view of these two" — produce targeted JSON-patch diffs, not whole-tree re-renders.

**Mechanism**: a new tool `refine_ui` that the LLM calls when the user requests an edit to existing UI. Takes the stable `card_id` (assigned by SpecStream) + the user's intent. LLM emits a small set of `replace`/`add`/`remove` ops scoped to the target.

**Files to touch**:
- `nautilus/static/buddy-assist-agent/canonical.template.json` — register `refine_ui` tool; teach via system prompt
- The healer applies (same rules)

**Effort**: S (~3 days).

**Acceptance criteria**: "make this card smaller" produces a single `replace` op on the card's content, not a full re-render.

### S2.4 — Publish AgentCard at `.well-known/`

**Goal**: A2A federation forward-compat. Declare our component catalog as a versioned extension URI.

**Files to touch**:
- `nautilus/static/buddy-assist-agent/.well-known/agent-card.json` (NEW)
- `nautilus/static/buddy-assist-agent/spec/openui-lang/v1.json` (NEW — the catalog schema)

**Effort**: S (~2 days).

**Acceptance criteria**: AgentCard validates against A2A spec; extension URI is fetchable and serves the catalog schema.

---

## Sprint 3 — Full UCP cutover (✅ SHIPPED + live-validated 2026-05-20, deadline 2026-06-15)

**Sprint scope was rebuilt three times during execution as live probing exposed Shopify's incomplete UCP rollout. Final shape — single endpoint, declared schemas, no legacy.**

### Reality-check timeline (compressed)

1. Initial assumption: legacy + UCP share tools; just URL-swap. **Wrong** — UCP renames tools (`get_product_details` → `get_product`), renames args (`cart_id` → `id`), uses a different cart schema (`cart.line_items[{item:{id}, quantity}]` not `cart.add_items[{product_variant_id}]`), and introduces a separate `create_cart` tool.
2. Plan A: hybrid via `discovery_url` (Pipecat MCPClient against legacy for schemas; route calls to UCP). **Abandoned** — legacy tool names + arg shapes don't match UCP, so the abstraction leaks.
3. Plan B: OAuth client_credentials + per-tool `tool_auth` for the policies tool. **Removed** — even with a valid bearer + the (undocumented) `Shopify-Buyer-IP` header, `search_shop_policies_and_faqs` returns "Tool not found" on UCP. Shopify hasn't migrated it yet.
4. Plan C (final): **UCP-only, declared tool schemas, direct HTTP poster**. Pipecat MCPClient is bypassed entirely for this server because UCP rejects `initialize` (no profile placement Shopify accepts on the handshake).

### Final architecture (S3.1)

**Profile hosting** — [PR juspay/loom#129 merged](https://github.com/juspay/loom/pull/129). Live at `https://breezebuddy.ai/.well-known/ucp/agent.json` with `Cache-Control: public, max-age=300`. Declares 5 capabilities: catalog.search, catalog.lookup, cart, checkout, dev.shopify.catalog. ES256 P-256 public JWK in `signing_keys[]` (private key at `/tmp/ucp-keys/`; move to stable location before prod).

**Template (`canonical.template.json`)** — single MCP server, no legacy.
- 5 `tool_schemas` declared inline: `search_catalog`, `lookup_catalog`, `get_product`, `get_cart`, `create_cart`, `update_cart` (6 entries — `create_cart` is separate from `update_cart` on UCP).
- `default_args.meta.ucp-agent.profile` injected on every call by the handler's `_deep_merge_defaults` helper.
- `state_reducers` capture `id → cart_id`, `continue_url → checkout_url`, `links → policy_links` on all three cart tools (response shape is FLAT, no `cart.` wrapper).
- `tool_arg_injection` fills `id` from `state.data.cart_id` for update/get_cart.
- `tool_response_transforms` scale UCP minor-units → decimals at `totals[*]`, `line_items[*].item` (with `amount_field: price`), `line_items[*].totals[*]`, `products[*].price_range.min/max`, `product.list_price_range.*`, `product.variants[*].price/list_price`.
- JIT instructions rewritten for UCP shape: `line_items[*].item.image_url` (image baked in — no reuse heuristic), `line_items[*].item.title`, `totals` per-line and per-cart, `continue_url` for checkout Handoff.

**Code (`clones/clairvoyance/app/ai/voice/agents/breeze_buddy/`)**:
- `mcp/__init__.py` — new `_create_direct_http_tool_handler` (UCP path: stateless JSON-RPC POST per call, no Pipecat session). `_create_mcp_tool_handler` keeps the Pipecat path for any non-UCP server. Branch on `server.tool_schemas` to choose path. `_deep_merge_defaults` merges `default_args` into args (caller wins).
- `template/types.py` — `McpServerConfig` gains `default_args` and `tool_schemas`. (`tool_auth` + `Oauth2ClientCredentialsConfig` + `HttpAuthType.OAUTH2_CLIENT_CREDENTIALS` + `mcp/oauth.py` + `discovery_url` + `included_tools` were all built and then removed as the plan compressed.)
- `tests/test_mcp_default_args.py` — 8 new tests. 143/143 total pass.

**Live-validated 2026-05-20 against Milton**:
- A1–A4 catalog search → product detail → create_cart → cart view all run cleanly via direct HTTP poster.
- Scaling fires correctly: `61300` (minor units) → `"613.00"` (decimal string) on `price` and `totals[*].amount` and `line_items[*].totals[*].amount`.
- JIT improvements landed: MRP moved to `subtitle` slot (muted, above body) on get_product detail card; carousel gets a description-snippet subtitle + "Save X%" / sizes / colours attribute chips with strict filter for noise tags (`gst_update_mrp`, ALL_CAPS_SNAKE, digit codes, vendor handles).

### Policies-tool — deferred until Shopify migrates

`search_shop_policies_and_faqs` is NOT on UCP for our credential set. Two-step fallback:
1. **Cart links workaround (active)** — cart responses include a `links[]` array with `{refund_policy, privacy_policy, terms_of_service, shipping_policy}` URLs. Captured via `state_reducer` into `state.data.policy_links`. System prompt instructs the agent to emit a Handoff to the matching URL when a cart exists. No tool call needed.
2. **Real UCP migration** — when Shopify exposes the tool, add a fresh `tool_schemas` entry. OAuth scaffolding can be re-added in ~30 LOC at that point (it was removed cleanly).

### Sprint 3 follow-ups — status as of 2026-05-20

- **S3.2 Idempotency-Key** ✅ **SHIPPED 2026-05-20.** See block below.
- **S3.3 Dual-tier error envelope** ⏬ **demoted to deferred-on-demand.** Trigger: ≥3 distinct error categories with bad UX show up in production telemetry. Current behaviour (catch-all "technical hiccup") is acceptable until eval data says otherwise. Mechanism notes preserved below for the day it gets picked up.
- **S3.4 ConfirmCard Trusted Surface** ⏬ **demoted to deferred-on-demand.** Trigger: positioning commits to "agentic pay-in-chat" as a differentiator. Current `continue_url` Handoff already gets shoppers to Shopify hosted checkout with Apple Pay / GPay / cards / Shop Pay all working. Mechanism notes preserved below.

### What ships next (release-readiness mode)

This release: refine + smoke + ship the work above. **Next phase** queue (priority order):

1. **NP-1 — S4.1 Eval harness** (~1 week) — reliability foundation. Recommended start.
2. **NP-2 — S2.1 Voice parity** (~1 week) — only if voice is in scope.
3. **NP-3 — S2.2 MCP Apps emission** (~3 days) — cross-host distribution.
4. **NP-4 — S2.3 `refine_ui`** (~3 days) — targeted UI edits.
5. **NP-5 — S2.4 A2A AgentCard** (~2 days) — federation forward-compat. **Distinct from the UCP profile we shipped** (different spec, different endpoint).

### S3.2 — Idempotency-Key on every mutating MCP call (✅ SHIPPED 2026-05-20)

**Goal achieved**: every `create_cart` and `update_cart` carries a fresh UUID v4 `idempotency_key`, generated by the engine before dispatch. Same UUID round-trips on retry (driven by intent, not turn) once the chat agent layers retry on top.

**Generic engine change** (commerce-agnostic; benefits any MCP server with mutating tools):

- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/types.py` — `ToolArgInjection` gains a `generators: Dict[arg_key, generator_name]` field, sibling to `set_paths`. Honours `only_if_missing` (caller value wins).
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/session_state.py` — adds `_GENERATORS` registry with four built-ins: `uuid_v4`, `uuid_v7` (falls back to v4 if stdlib lacks it), `timestamp_iso8601`, `timestamp_unix_ms`. Unknown generator name logs a warning and skips — defensive stance matches the existing JMESPath path.

**Template wiring** (vertical-specific; lives in `canonical.template.json`):

```jsonc
"tool_arg_injection": [
  {"tool_name": "create_cart", "generators": {"idempotency_key": "uuid_v4"}},
  {"tool_name": "update_cart", "set_paths": {"id": "state.data.cart_id"},
                                 "generators": {"idempotency_key": "uuid_v4"}},
  {"tool_name": "get_cart",    "set_paths": {"id": "state.data.cart_id"}}
]
```

**Tests**: 151/151 pass (was 143/143; +8 generator tests). Covers: uuid_v4 fills missing arg + parses as v4, `only_if_missing` honoured, force-override works, unknown generator silently skipped, each call produces fresh value, iso8601 + unix_ms shapes, generators + set_paths coexist on one rule.

**Known open question (for the smoke pass)**: UCP's exact accepted placement for `idempotency_key` isn't documented. We know `meta.ucp-agent.profile` works via `default_args` deep-merge; whether `idempotency_key` belongs at the top level or under `meta.idempotency_key` is TBD. Top-level is the conservative default — UCP typically passes unknown args through silently. If Milton smoke shows UCP rejects, the fix is either (a) move under `meta.` (requires nested-key support in the generators engine — ~5 LOC), or (b) emit as an HTTP header in the direct-HTTP poster. Both are 1-evening tasks.

**What this engine unlocks beyond UCP**:
- Stripe-style `Idempotency-Key` for any future MCP integration with mutating endpoints
- Client-side `request_id` for distributed tracing across tool calls
- `timestamp_unix_ms` for any tool that wants client-clock signals (avoids ML model fabricating timestamps)
- Future: easy to add `nanoid` / `ulid` / monotonic-counter generators (all ~3 LOC each)

### S3.3 — Dual-tier error envelope (resolution-driven retry) — ⏬ DEFERRED-ON-DEMAND (2026-05-20)

> **Demoted on 2026-05-20.** Trigger to re-prioritize: ≥3 distinct error categories with bad UX surface in production telemetry from the eval harness (NP-1). Current "technical hiccup" catch-all is acceptable until then. Notes below preserved for the day this gets picked up.

**Goal**: LLM stops guessing whether to retry vs ask user. Resolution enum drives a fixed state machine.

**Mechanism**: 
- Main agent handles protocol-level vs business-level errors distinctly
- Business errors with `resolution: recoverable` → silent retry (no LLM round-trip)
- `requires_buyer_input` → emit a `Message` primitive with the inline param
- `requires_buyer_review` → emit a `Message` with a confirm CTA
- `unrecoverable` → emit a `Handoff` with `continue_url`

**Files to touch**:
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/mcp/__init__.py` — parse UCP/ACP response envelope, surface error envelope distinctly
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/session_state.py` — extend reducer to lift `escalation_reasons`
- System prompt — explicit retry/ask/escalate rules

**Effort**: M (~1 week).

### S3.4 — `ConfirmCard` primitive (Trusted Surface foundation) — ⏬ DEFERRED-ON-DEMAND (2026-05-20)

> **Demoted on 2026-05-20.** Trigger to re-prioritize: positioning commits to "agentic pay-in-chat" as a differentiator. Current `continue_url` Handoff already gets shoppers to Shopify hosted checkout where Apple Pay / GPay / cards / Shop Pay all work; UX is fine, conversion is fine. ConfirmCard is the upgrade that lets the shopper never leave chat. Strategic bet, not a deadline item. Notes below preserved.

**Goal**: T&C / future-payment confirm UI is server-rendered directly from a typed payload — no LLM in the path. AP2-conformant by construction.

**Mechanism**:
- New primitive `ConfirmCard` in the SpecStream catalog
- BUT — when the LLM tries to emit it, the server intercepts: the primitive's `props` come ONLY from a typed `confirm_request` payload that came from a tool, never from LLM-emitted strings
- Widget renders from the typed payload directly
- "Show your work" expandable panel (AP2 pattern) shows the underlying confirm_request structure to the user

**Files to touch**:
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_stream.py` — special handling for ConfirmCard ops (server replaces LLM-emitted props with typed payload)
- `loom/packages/breeze-buddy-assist-widget/src/lib/ui/primitives/ConfirmCard.svelte` (NEW)
- Replace today's T&C modal as the first ConfirmCard consumer

**Effort**: M (~1 week).

### Sprint 3 sequencing — final outcome

```
S3.1 + S3.2 — shipped + live-validated 2026-05-20 (well ahead of the 2026-06-15 Shopify UCP deadline)
S3.3 + S3.4 — demoted to deferred-on-demand; see triggers above
```

---

## Sprint 4 — Golden-screenshot eval pipeline (parallelizable with Sprint 2 or 3)

### S4.1 — Eval harness

**Goal**: Regression testing for generative UI. Catch quality drops before they ship.

**Pattern** (ArtifactsBench-style):
1. Frozen prompt list (~30 canonical prompts: product search, cart view, FAQ, comparison, etc.)
2. CI runs the agent against each prompt
3. Capture 3 temporal screenshots of the rendered widget output
4. Multimodal LLM-as-judge scores each against a per-prompt checklist + golden screenshot
5. Fail the CI if any prompt drops below threshold

**Files to create**:
- `nautilus/eval/golden-prompts.json` — the frozen prompt list with checklists
- `nautilus/eval/golden-screenshots/` — reference renders
- `nautilus/eval/run_eval.ts` — Playwright + agent invocation + judge call
- `.github/workflows/ui-eval.yml` — CI job

**Effort**: M (~1 week).

**Acceptance criteria**: CI passes on a known-good baseline; deliberately broken prompt fails the gate.

---

## Deferred — on-demand, not in next 8 weeks

| Item | Trigger to start | Source |
|---|---|---|
| **Fine-tuned tiny healer model** (vercel-autofixer-01 style) | If deterministic healer catch rate is insufficient after measurement | `poc/WEB_RESEARCH_UI_GEN.md` §"Vercel v0" |
| **`@Widget(...)` escape valve for registered widgets** | Only if we have a canonical UI that SpecStream composition can't express elegantly | `poc/GOLDEN_UI_INSIGHTS.md` §8 |
| **AP2 mandate signing infrastructure** | Payment scope unlocked | `poc/AP2/INSIGHTS.md` |
| **Identity linking (dev.ucp.common.identity_linking)** | Customer-account flows become required | UCP spec |
| **Discount extension (dev.ucp.shopping.discount)** | Discount UI requested by merchants | UCP spec |
| **Order MCP (dev.ucp.shopping.order)** | Replaces Nautilus WISMO when UCP order spec stable | UCP spec |
| **Programmatic Tool Calling / `batch_tool`** | Voice latency becomes measurable regression | `poc/claude-cookbooks/INSIGHTS.md` |
| **AgentCard JWS signing** | Third-party clients call us as A2A peer | `poc/A2A/INSIGHTS.md` |
| **Session compaction with ID-preserving summary** | Session length hits context limits | `poc/claude-cookbooks/INSIGHTS.md` |
| **Vision-LLM-as-judge in render loop** | "Design-conscious merchants" tier; per-render visual correctness checks | `poc/WEB_RESEARCH_UI_GEN.md` §"Pattern G" |

---

## Validation strategy

**One consolidated e2e pass at the end of all planned sprints, not piecemeal.**

*Update (2026-05-19): the post-Sprint-1 smoke test surfaced two fixes (max_tokens, alias rename) that needed to ship before the consolidated pass. Smoke checks immediately after each sprint are explicitly carved out — see §"Real-traffic learnings". Validation status table is in §"Already shipped → Sprint 1 live-validation status".*

### What to cover

1. **Cart-id reuse** (already-shipped fix):
   - New session → "Add Dawn to cart" → "Add Ski Wax to cart" → confirm single cart_id reused, no orphan, no "cart does not exist" error
   - `agent_session_state.data.cart_id` populated; `chat_message.content_blocks` contains tool_use/tool_result pairs

2. **SpecStream wire format** (S1.1):
   - LLM successfully emits JSONL on canonical prompts (product search, cart view, FAQ, comparison)
   - Widget renders progressively (Card frame → image → text → button)
   - Stable IDs persist across turns
   - Old DSL imports return ImportError if anyone references them

3. **In-stream healer** (S1.2):
   - 7 deliberately-broken inputs heal correctly via deterministic rules
   - `healer_applied` SSE events observable

4. **JIT UI instructions** (S1.3):
   - Tool response for `search_catalog` carries UI instructions
   - System prompt UI section measurably shorter
   - Product cards visibly richer than today's baseline

5. **Voice/chat parity** (S2.1):
   - Chat: open, see cart card. Voice-connect mid-session, ask for another product, card streams into voice surface. Voice-end, card history intact in chat.

6. **MCP Apps emission** (S2.2):
   - Tool response carries both native SpecStream + `text/html;profile=mcp-app` resource
   - Both validate

7. **`refine_ui`** (S2.3):
   - "Make the third card smaller" produces ≤3 patch ops, not full re-render

8. **UCP cutover** (S3.1):
   - Catalog calls hit `/api/ucp/mcp`; cart calls still on `/api/mcp`
   - Capability negotiation field present on responses

9. **Resolution-driven retry** (S3.3):
   - Force `recoverable` error → silent retry, no LLM round-trip
   - Force `requires_buyer_input` → inline `Message` primitive with form

10. **Trusted Surface** (S3.4):
    - T&C modal replaced with ConfirmCard; LLM-emitted prose for `props` is ignored

11. **Idempotency** (S3.2):
    - Network-disconnect an `update_cart`; reconnect; same key used; no double-add

12. **Golden screenshots** (S4.1):
    - CI passes on baseline; deliberately broken prompt fails

13. **Cross-provider** (validates provider-neutrality claim from cart-id fix):
    - Swap template `llm_configurations.sdk: "anthropic"` → `"openai"`; same flows work

### What NOT to do

- No piecemeal browser tests between sprint items
- No promoting individual items to "done" before this e2e pass

---

## Failure UX policy

Explicit policy for what the user sees when generation fails. Avoid silent-failure-on-the-long-tail; it's the killer of trust.

| Failure mode | Detection | User-visible behavior |
|---|---|---|
| Healer rule fires (mechanical fix applied) | `healer_applied` SSE event | None — silent fix. Telemetry only. |
| Healer can't fix (op dropped) | Catalog/schema reject | UI continues rendering remaining ops. Missing card is just absent. No banner. |
| Whole `ui_stream` tool call fails (LLM emits nothing valid) | No ops received before tool result | Prose-only response (degraded). No skeleton/error widget. Main agent's prose carries the answer. |
| Tool result with `trigger: skip_ui` | Config | No UI, prose handles. Working as intended. |
| MCP tool failure (cart create returns 4xx) | Resolution-driven retry envelope (S3.3) | `recoverable` → silent retry. `requires_buyer_input` → Message primitive inline. `requires_buyer_review` → confirm CTA. `unrecoverable` → Handoff. |
| `refine_ui` targets nonexistent `card_id` | Server validation | Server returns error to LLM (`{ok: false, reason: "card_id not found"}`); main agent handles in prose. |
| ConfirmCard with malformed typed payload | Server-side validation before render | Server logs error, returns `{ok: false}` to LLM. Main agent falls back to plain-prose summary + Handoff to web checkout. |
| SSE/RTVI stream disconnect mid-render | Client detects | Widget keeps already-applied ops; resumes from session UI state on reconnect. Partial cards stay partial; user can ask "show me again." |
| Healer catch rate < 85% in production telemetry | Sprint 4 eval pipeline | Trigger: invest in fine-tuned tiny healer model (deferred item). |

Principle: **degraded but coherent > broken-looking**. If we can't render perfectly, the conversation continues in prose and the user can re-ask.

---

## Open questions / deferred decisions

Things we explicitly chose NOT to decide yet. Each entry has a "decide when" trigger so we don't paralyze on them.

| Question | Current default | Decide when |
|---|---|---|
| **Voice barge-in policy** — does user interrupt clear in-flight UI cards? | Cards persist; user can ask to clear. | Sprint 2 (S2.1), once voice parity demos surface the UX feel. |
| **Session UI state persistence** — do we save the SpecStream tree to DB for resume across reloads? | Rehydrate on reconnect from session state + last tool results (no separate UI persistence). | If reload-loses-cards bugs appear in beta. |
| **Per-merchant UI prompt themes** — common base prompt vs per-merchant fork? | Per-merchant via `tool_ui_instructions` in template config. Common base might emerge as a default block merchants override. | Once we have ≥3 merchants and observe drift. |
| **`@Widget(...)` registered-widget escape valve** | Don't ship. SpecStream composition is expressive enough. | Concrete case where SpecStream can't elegantly express a canonical UI. High bar. |
| **JIT instruction token cost ceiling** | Unbounded for now (cost not a constraint). | If a single tool's instructions exceed ~2KB or merchants ship hundreds. |
| **Healer rule-set governance** — code-defined vs template-configurable? | Code-defined in `ui_healer.py`. Merchants don't extend. | If merchants need merchant-specific heal rules (likely never). |
| **MCP Apps host targeting** — emit by default for all merchants, or opt-in flag? | Opt-in via template flag `emit_mcp_apps: true`. Default off. | When a merchant asks for cross-host distribution. |
| **Eval pipeline judge model** — Sonnet, Opus, or external? | Sonnet for cost; revisit if scores drift from human review. | After 4 weeks of CI eval data. |
| **Refine-ui scope** — only adjacent card edits, or arbitrary tree mutations? | Adjacent edits (replace one card's content; add/remove cards from same root). | When users start asking for cross-card refinements. |
| **Component catalog versioning** — single `v1` URI, or per-primitive? | Single `v1` URI for the catalog; primitives bump together. | When a merchant pins to a stale version. |

---

## Operational notes

### Server restart required after each Clairvoyance code change

The running `python run.py` process holds Python modules in memory. Schema changes to `template/types.py`, new modules, or any updated handler code require a restart to take effect. Migrations apply live (DB-level); template JSON re-provision is live; code changes are not.

### Template cache invalidation

Templates are cached in Redis with TTL. After a template re-provision, either wait for TTL expiry or invalidate keys matching `bb:tpl:*` and `bb:mcp:tools:*`.

### Local dev token mint (RBAC admin)

```bash
cd clones/clairvoyance && set -a && source .env && set +a && .venv/bin/python <<'PY'
from datetime import timedelta
from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.schemas.breeze_buddy.auth import UserRole
token = rbac_token_manager.create_access_token_with_rbac(
    user_id='local-admin', username='local-admin', role=UserRole.ADMIN,
    reseller_ids=['*'], merchant_ids=['*'], email='admin@local',
    expires_delta=timedelta(hours=6),
)
open('/tmp/clairvoyance_admin_token.txt', 'w').write(token)
PY
```

Trap I hit (record so you don't): use `BreezeBuddyRBACTokenManager.create_access_token_with_rbac`, NOT `JWTManager.create_access_token`. The RBAC verifier reads `sub` (not `id`) — the generic JWTManager mints `id`, gets rejected by the RBAC route with `"Could not validate credentials"`.

Second trap: **don't `stdout` the token in Python**. Pipecat's `import` writes its `ᓚᘏᗢ Pipecat 1.1.0 …` logger banner to stdout at module-load time. The token gets concatenated with log noise. Always write via `open(path, 'w').write(token)` from inside the Python heredoc.

### Re-provision command

Run from `nautilus/` cwd:

```bash
cd ~/Repos/BreezeBuddy/nautilus && \
CLAIRVOYANCE_ADMIN_TOKEN="$(cat /tmp/clairvoyance_admin_token.txt)" \
  CLAIRVOYANCE_BASE_URL="http://localhost:8000" \
  MERCHANT_DOMAIN=swaroop-juspay.myshopify.com \
  bash scripts/provision-buddy-assist-merchant.sh
```

The script is idempotent: first run creates template + widget_config; subsequent runs PUT updates. Swap `MERCHANT_DOMAIN` to provision a second merchant (each gets its own `template_id` + `widget_config_id` + `public_widget_key`).

`STOREFRONT_TOKEN` is optional but warns if absent; for both `swaroop-juspay` and `milton-india-store` dev stores, MCP is open (no auth header required).

### Live merchants (post-Sprint-1.5)

| Merchant | Template ID | Widget Key | MCP base | Notes |
|---|---|---|---|---|
| `swaroop-juspay.myshopify.com` | `595650c5-b3c1-4b68-a3e9-5f22630283e0` | `DKt-j-YE-qCZeNcUt_Fh2v4OQ9LjqbyreDjBi7Poe5s` | `https://swaroop-juspay.myshopify.com/api/mcp` | Original smoke target. Shopify demo inventory (snowboards, shoes). |
| `milton-india-store.myshopify.com` | `760a45f5-68c3-4941-8a73-558758f366d0` | `LT_uwSgXxRBDfPxf529QaX9MfP1PRgkUKtkjr0mhp7o` | `https://milton-india-store.myshopify.com/api/mcp` | Real-storefront smoke. Water bottles, kitchenware. Tags are numeric codes; `alt_text` empty — anticipated healer/JIT tweaks in §"Real-traffic learnings" #4. |

Both share the same `canonical.template.json` source; each provisioning produces an independent merchant-scoped template row. To switch the dev harness between merchants, edit `loom/packages/breeze-buddy-assist-widget/index.html` — change the `tenant=` (widget-key) + `shop=` attributes on the `<breeze-buddy-assist>` element.

### Testing SpecStream emission locally (Sprint 1+)

To be defined as part of S1.1 implementation. Likely: a `--dry-run` mode in the chat agent that emits JSONL to stdout for inspection without hitting the widget.

### Validating the AgentCard (Sprint 2+)

```bash
# After S2.4 publishes the AgentCard
curl http://localhost:5180/.well-known/agent-card.json | jq .
# Validate against A2A spec
```

---

## File map (where to find things across the monorepo)

Three repos involved. Routinely mixed up. Map for clarity:

| Path root | Repo | Purpose |
|---|---|---|
| `~/Repos/BreezeBuddy/nautilus/` | nautilus (Shopify-side) | Shopify app, widget, storefront extensions, canonical template JSON, docs, migrations for Shopify-side tables |
| `~/Repos/BreezeBuddy/clones/clairvoyance/` | clairvoyance (agent backend) | Python backend: chat agent, voice agent, MCP client, template engine, DB accessors, FastAPI routes, migrations for chat_session/agent_session_state/etc. |
| `~/Repos/loom/packages/client-sdk/` | client-sdk | Framework-agnostic JS SDK published as `@juspay/breeze-buddy-client-sdk`. Used to host the OpenUI Lang parser; will be slimmed when DSL is deleted in S1.1. |
| `~/Repos/BreezeBuddy/poc/` | research scratch (not deployed) | 15 cloned reference repos + 3 synthesis docs (GOLDEN_INSIGHTS, GOLDEN_UI_INSIGHTS, WEB_RESEARCH_UI_GEN) + 15 per-repo INSIGHTS.md + 9 UI_INSIGHTS.md. Source for every architectural decision. |

Quick lookup (Sprint 1 + 1.5 — current state):

- Chat agent turn loop: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/agent.py` (resolves `_ui_allowlist` per turn from `template.configurations.ui_catalog`)
- Chat history persistence: `clones/clairvoyance/app/database/{queries,accessor,decoder}/breeze_buddy/chat_session.py`
- Anthropic block ↔ OpenAI LLMContextMessage codec: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/block_codec.py`
- Reducer + arg-injection engines: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/session_state.py`
- MCP client wrapper + JIT-instruction injection: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/mcp/__init__.py`
- Template types (Pydantic): `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/types.py` (`ToolUiHint`, `ToolUiTrigger`, `ToolUiExample`, **`UiCatalogConfig`**)
- **SpecStream UI catalog** (15 primitives + group registry): `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/ui_catalog.py` — `PRIMITIVE_GROUPS`, `PRIMITIVE_RENDER_ORDER`, `resolve_allowlist`, `group_for`, **`Tile` + `TileMedia`/`TileBodyItem`/`TileAttribute`/`TileAction`**
- **System-prompt primitives section renderer**: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/ui_prompt.py` — `render_primitives_section(allowlist)` introspects Pydantic schemas
- Builder + system-prompt splice: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/template/builder.py` (`build_flow_config(..., ui_allowlist=...)`, replaces `{{ui_primitives_section}}` placeholder)
- **UI wire format / extractor / parser / allowlist gate**: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_stream.py` (`parse_op_line(..., allowlist=)`, `process_op_line(..., allowlist=)`)
- **In-stream healer + alias table**: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/ui_healer.py` (`_PROP_ALIASES`)
- Response-transform engine: `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/handlers/transport/utils/response_transform.py`
- Chat API router: `clones/clairvoyance/app/api/routers/breeze_buddy/chat/handlers.py`
- Migration runner: `clones/clairvoyance/scripts/migrate.py`
- Migrations (chat-side): `clones/clairvoyance/app/database/migrations/`
- Migrations (Shopify-side): `nautilus/database/migrations/`
- Canonical template (commerce config + JIT instructions + system prompt + ui_catalog config): `nautilus/static/buddy-assist-agent/canonical.template.json`
- Provisioning script: `nautilus/scripts/provision-buddy-assist-merchant.sh`
- **Widget package (lives in loom)**: `loom/packages/breeze-buddy-assist-widget/`. Built via `pnpm run build` at loom root (root build calls `build:widget` which copies output to `loom/build/widget/`). Served at `https://breezebuddy.ai/widget/assist.js`.
- **Widget UI state store** (Svelte 5 $state tree): `loom/packages/breeze-buddy-assist-widget/src/lib/ui/ui_state.svelte.ts`
- Widget renderer (stateful patch applier): `loom/packages/breeze-buddy-assist-widget/src/lib/ui/UiRenderer.svelte` + `UiNode.svelte` (Tile dispatch with TS narrowing)
- Widget primitives: `src/lib/ui/primitives/` (Stack/Row/Card/CardHeader/Image/Text/Carousel/Button/Buttons/Table/Tag + typed Message/Handoff + composite `Tile.svelte`)
- Widget UI pane (per-block ops application): `src/lib/components/BbUiPane.svelte`
- Widget shell: `src/BuddyAssist.svelte` (registers the `<breeze-buddy-assist>` custom element)
- Widget dev harness: `loom/packages/breeze-buddy-assist-widget/index.html` — edit `tenant=` and `shop=` to switch between merchants.
- **Use-case showcase landing (Sprint 1.9)**: `loom/packages/breeze-buddy-assist-widget/showcase.html` + `Showcase.svelte` — 20 vertical demos using real widget chrome + `UiRenderer`.
- **TTFB thinking indicator (Sprint 1.8)**: `loom/packages/breeze-buddy-assist-widget/src/lib/components/BbThinkingRow.svelte`
- **New-chat bottom-sheet (Sprint 1.8)**: `loom/packages/breeze-buddy-assist-widget/src/lib/components/BbConfirmSheet.svelte`
- **Mascot + launcher + footer wordmark (Sprint 1.8)**: `loom/packages/breeze-buddy-assist-widget/src/lib/components/BbBuddy.svelte` + `BbSolidOrb.svelte` + `BbLauncher.svelte` + `BbWatermark.svelte`
- **Cross-pod cancel pubsub + per-pod task registry (Sprint 1.8)**: `clairvoyance/app/api/routers/breeze_buddy/chat/cancel_bus.py`
- SDK UI types (now data-only): `~/Repos/loom/packages/client-sdk/src/lib/ui/types.ts` (`UiOp`, `UiAction`, `UI_CATALOG_VERSION`)
- SDK chat store (per-turn ops accumulator): `~/Repos/loom/packages/client-sdk/src/lib/store/_buddy-chat-store.ts`
- SDK turn engine (SSE → events): `~/Repos/loom/packages/client-sdk/src/lib/chat/_turn-engine.ts`
- Tests (cart-id fix): `clones/clairvoyance/tests/test_session_state.py` (19 tests)
- Tests (response transforms): `clones/clairvoyance/tests/test_response_transform.py`
- Tests (UI wire format): `clones/clairvoyance/tests/test_ui_stream.py` (23 tests)
- Tests (healer): `clones/clairvoyance/tests/test_ui_healer.py` (19 tests — incl. alias-rename cases)
- Tests (JIT instructions): `clones/clairvoyance/tests/test_jit_instructions.py` (6 tests)
- **Tests (catalog groups)**: `clones/clairvoyance/tests/test_ui_catalog_groups.py` (11 tests — resolve_allowlist precedence, group_for, edge cases)
- **Tests (Tile validation)**: `clones/clairvoyance/tests/test_tile_validation.py` (14 tests — slot coverage, polymorphic body kinds, required-title rejection)
- **Tests (primitive_disabled)**: `clones/clairvoyance/tests/test_primitive_disabled.py` (7 tests — `primitive_disabled:<type>` distinct from `unknown_type`)
- **Tests (ui_prompt render)**: `clones/clairvoyance/tests/test_ui_prompt.py` (11 tests — allowlist filtering, render order, slot-name presence, validated examples)

**Deleted in Sprint 1** (don't try to import these — they're gone):
- `clones/clairvoyance/app/ai/voice/agents/breeze_buddy/chat/{ui_parser,ui_resolver,ui_marker,ui_emit,ui_prompt}.py`
- `~/Repos/loom/packages/client-sdk/src/lib/ui/_lang-core/` (entire directory ~3kLOC vendored parser)
- `~/Repos/loom/packages/client-sdk/src/lib/ui/{_parse,_action-bus,_evaluator}.ts`
- `~/Repos/loom/packages/client-sdk/src/lib/ui/{UiRenderer,UiNode}.svelte` (SDK-side; widget owns the renderer now)
- `~/Repos/loom/packages/client-sdk/src/lib/ui/primitives/` (entire directory)
- `nautilus/packages/breeze-buddy-assist-widget/src/lib/ui/_evaluator.ts`

## References

### Internal docs (read in this order if catching up)

1. **`poc/WEB_RESEARCH_UI_GEN.md`** — production-system research; source of the three pivots that shape this roadmap
2. **`poc/GOLDEN_UI_INSIGHTS.md`** — open-source UI synthesis (15 repos)
3. **`poc/GOLDEN_INSIGHTS.md`** — cart-id loss + generic-vs-flavoured verdict (informs already-shipped sprint)
4. **`poc/REPOS.md`** — index of the 15 cloned open-source repos
5. **`docs/features/BREEZE_BUDDY_ASSIST.md`** — primary design doc (older — predates this roadmap)
6. Individual `poc/<repo>/INSIGHTS.md` and `poc/<repo>/UI_INSIGHTS.md` — per-repo deep reads

### External references for the new direction

- [Vercel json-render](https://github.com/vercel-labs/json-render) — SpecStream wire format reference impl (Apache 2.0)
- [A2UI v0.9](https://developers.googleblog.com/a2ui-v0-9-generative-ui/) — Google's open generative-UI spec
- [MCP Apps spec](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/) — cross-host UI standard (Jan 2026)
- [Vercel v0 composite model](https://vercel.com/blog/v0-composite-model-family) — healer architecture
- [Shopify Sidekick engineering](https://shopify.engineering/building-production-ready-agentic-systems) — JIT instructions pattern
- [Cognition: Don't Build Multi-Agents](https://cognition.ai/blog/dont-build-multi-agents) — context-coherence argument against UI subagent
- [Anthropic multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) — the counter-position; where multi-agent wins (research, not UI)
- [AG-UI protocol](https://docs.ag-ui.com/) — stateful-agent UI assumption; informs our patch-based renderer
- [ArtifactsBench paper](https://arxiv.org/pdf/2507.04952) — multimodal eval pipeline pattern for S4.1

External references for the new direction:
- [Vercel json-render](https://github.com/vercel-labs/json-render) — SpecStream wire format reference impl
- [A2UI v0.9](https://developers.googleblog.com/a2ui-v0-9-generative-ui/) — Google's open generative-UI spec
- [MCP Apps](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/) — cross-host UI standard
- [Vercel v0 composite model](https://vercel.com/blog/v0-composite-model-family) — healer architecture
- [Shopify Sidekick](https://shopify.engineering/building-production-ready-agentic-systems) — JIT instructions pattern
- [Cognition: Don't Build Multi-Agents](https://cognition.ai/blog/dont-build-multi-agents) — why no UI subagent
