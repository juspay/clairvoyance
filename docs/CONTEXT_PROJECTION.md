# Tool-result context projection (chat mode)

> **Two config sources, one engine.** The compactor (`compact_tool_results`) is
> **tool-agnostic** — it keys stale `tool_result` blocks by tool *name* and
> knows nothing about MCP vs global functions. Only the *config* comes from two
> places, both merged in `chat/agent.py`:
>
> 1. **MCP tools** — `McpServerConfig.tool_context_retention` /
>    `tool_context_projection` (aggregated over `configurations.mcp.servers`).
> 2. **Global HTTP / builtin / custom functions** — the per-function
>    `context_retention` / `context_projection` on `BaseGlobalFunction`
>    (aggregated over `flow["functions"]`).
>
> So a global-function template (no MCP servers) can now opt into
> `last_turn_only` + projection too. Still prefer trimming at the source with
> each function's `expected_response_schema` (JMESPath whitelist) — that keeps
> the payload small *before* it enters context; retention is the cross-turn
> backstop for what does land.

## Problem

Chat-mode MCP tools like `search_catalog` return large payloads (~33k tokens
for a 10-product result — full `variants[]`, descriptions, media). To bound
input-token cost, the template marks such tools `tool_context_retention:
last_turn_only`, and `context_compactor` rewrites every stale `tool_result`
(all but the single most-recent) to a 1-line stub:

```
[pruned: search_catalog({"query":"pink top"}) ran earlier — re-call this tool if you need fresh data]
```

The stub drops **everything**, including the stable identity the shopper refers
back to: a product's `url`/`handle`, its `price`, and the **variant id** needed
to re-add it to cart. Consequences observed in production:

- **Mid-turn thrash** — building a multi-item combo ("top + bottom + socks") in
  one turn, the model finds the top (search 1), searches the bottom (search 2),
  and the top's result is immediately stubbed. Having lost it, the model
  re-searches — looping until it trips `_MAX_TOOL_CYCLES` and the turn ends with
  no reply.
- **Lost data on follow-ups** — several turns later "give me the product links"
  finds only stubs in context, so the model either refuses ("I don't have the
  URLs") or *guesses* a handle (`/products/crossover-bra` when the real handle
  is `crossover-bra-pink` — a 404).

Keeping the full results in `session` instead is not an option: a single search
is ~33k tokens; a whole session of them is ~250k.

## Design — project, don't stub

`context_compactor` keeps the **most-recent** result full (so the model fully
understands each search the moment it lands), and compacts every **older**
`last_turn_only` result to an **identity projection** instead of a bare stub —
*when the tool declares a keep-list*. The projection keeps only the whitelisted
paths and drops the heavy fields. On the real 33k payload this is **~1.7k tokens
(5%)** while preserving 100% of what follow-up turns need.

Two-tier retention:

| Tier | Scope | Content |
|---|---|---|
| full | most-recent result (`recent_keep=1`) | whole payload — variants, descriptions, media |
| projection | older `last_turn_only` results | only the keep-list paths (identity) |

This fixes both failures: the model keeps prior-search identity (so it stops
re-searching and re-adds/links from memory), and turns stay well under the
cycle cap. `_MAX_TOOL_CYCLES` was also raised 8 → 20 for headroom on legitimate
multi-item flows.

## Config — `McpServerConfig.tool_context_projection`

Per-tool map of *tool name → list of keep-paths*. Path grammar matches
`tool_response_transforms`: `a.b` descends keys, `a[*].b` iterates a list at
`a` and descends `b` on each item.

```jsonc
"tool_context_projection": {
  "search_catalog": [
    "products[*].id", "products[*].title", "products[*].handle", "products[*].url",
    "products[*].price_range.min.amount", "products[*].price_range.min.currency",
    "products[*].variants[*].id", "products[*].variants[*].title",
    "products[*].variants[*].options",     // axis values, e.g. Color=Purple
    "products[*].variants[*].availability", // note: 'availability', not 'available'
    "products[*].variants[*].price",        // per-variant price object
    "pagination"
  ]
}
```

Keeping `variants[*].{id,title,options,availability,price}` means a
variant-sensitive query ("show purple bottles") retains every variant's
identity — so "add the purple one", "is purple in stock?", and "what's purple's
price?" all work later without a re-search, and the shopper can pivot to another
variant too. There is no per-variant page URL in the UCP result; build it from
`product.url` (`?variant=<id>`) when needed.

Behavior is opt-in and graceful:

- Tool with a keep-list → projected.
- `last_turn_only` tool **without** a keep-list → old 1-line stub (unchanged).
- `session` tool (e.g. cart calls) → kept full, never compacted.
- Keep-path pointing at a missing field (schema drift) → silently omitted.
- Projection comes back empty → falls back to the stub.

The engine is domain-blind: it only knows "keep these paths." The field list is
the sole domain-specific part and lives in the template — same as
`tool_context_retention` and `state_reducers`.

## Rollout order

The engine change must deploy **before** a template adds
`tool_context_projection`. Old code ignores the unknown field (`McpServerConfig`
is `extra="ignore"`), so adding it early is harmless but inert; the projection
only takes effect once this code is live. Recommended:

1. Merge + deploy this change.
2. `templates update <id>` to add `tool_context_projection` to the four
   Shopify-assist merchants' MCP server config (search_catalog / lookup_catalog
   / get_product).
3. Verify via a session: confirm a follow-up "give me the link" answers with the
   real handle and no re-search.
