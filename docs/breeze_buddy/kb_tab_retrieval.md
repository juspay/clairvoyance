# KB Tab Retrieval — What We Built and Why

Companion to `docs/breeze_buddy/knowledge_base_overview.md` (read that first
if you haven't). This describes the addition on branch `feat/kb-tab-retrieval`:
two new builtin global functions, `get_tab_data` and `list_kb_tabs`, that let
an LLM (or the custom-Python function calling it) pull the exact text of one
already-ingested spreadsheet tab — no semantic search, no new tables, no
second ingestion pipeline.

## The problem this solves

The KB's existing modes (`full_injection`, `auto_retrieve`, `tool`) all treat
an ingested spreadsheet as an undifferentiated pool of row-chunks:
`full_injection` dumps everything, `auto_retrieve`/`tool` semantically search
across all rows regardless of which tab they came from. There was no way to
say "give me exactly the Pricing tab's rows, deterministically, right now" —
which is what you want when the conversation reaches a point where you know
precisely which structured table is relevant (pricing, clinic directory,
FAQ) and don't want to gamble on a similarity search picking the right rows.

Two systems were considered and rejected before this one:

- **PR #878** (`feat/data-sources-runtime-release`) — a parallel `data_source`
  table, its own Google Sheets adapter, its own Redis cache, its own CRUD API.
  Rejected because it duplicates everything the KB ingestion pipeline already
  does (auth, fetch, cache, freshness) as a second system.
- **This branch's own earlier `data_sources.py`** (on `feat/data-sources-on-demand-loading`)
  — same problem, different shape: its own migration, its own connector
  registry, its own `load_data_source` builtin.

Both work by re-implementing "fetch a Google Sheet tab." This addition
instead reads a tab straight out of data the KB **already ingested** — same
`kb_chunk` table, same cache invalidation, same auth path, zero new storage.

## How it works

Every row-chunk from a Sheets ingestion already carries its source tab name:

```python
# chunking.py::chunk_table — already existed, unmodified by this work
metadata = {"table": table.name, "row_key": f"{table.name}:{row_index}"}
```

So "give me the Pricing tab" is just: query `kb_chunk` filtered by
`metadata->>'table' = 'Pricing'`, concatenate the READY rows in order, cache
the result under the same version-stamped key scheme the KB already uses for
`full_injection`. That's the whole mechanism — three thin layers cloned from
existing code:

```
Template attaches a knowledge_base (as it does today)
  │
  LLM calls the builtin function  get_tab_data(tab_name="Pricing")
  │
  handlers/internal/kb_tab_data.py :: get_tab_data(context, args)
  │   reads context.configurations.knowledge_base.knowledge_base_ids
  │   (same field query_knowledge_base already reads)
  │
  services/knowledge_base/retrieval.py :: get_kb_tab_text(kb_ids, "Pricing")
  │   version-stamped Redis cache (kb:tab:Pricing:<hash>), same pattern
  │   as get_full_kb_text() — ingestion's existing INCR kb:ver:{id}
  │   invalidates this for free, no new invalidation code
  │   cache miss ↓
  │
  database/accessor/.../knowledge_base.py :: get_kb_tab_rows(kb_ids, "Pricing")
  │   SELECT c.text, d.name FROM kb_chunk c JOIN kb_document d ...
  │   WHERE c.kb_id = ANY($1) AND d.status = 'READY'
  │     AND c.metadata->>'table' = $2
  │   ORDER BY d.created_at, c.document_id, c.chunk_index
  │
  ↓
  "### Health Companion Sheet\niPhone 15,$999\niPhone 16,$1099\n..."
  returned to the LLM as the function-call result
```

`list_kb_tabs` is the same shape, one level up: `SELECT DISTINCT
metadata->>'table' ...` — so the LLM (or your routing code) can discover
valid tab names instead of guessing them and silently getting nothing back.

## Files touched

| Layer | File | What was added |
|---|---|---|
| Query | `app/database/queries/breeze_buddy/knowledge_base.py` | `get_kb_tab_rows_query`, `list_kb_tab_names_query` |
| Accessor | `app/database/accessor/breeze_buddy/knowledge_base.py` | `get_kb_tab_rows`, `list_kb_tab_names` |
| Retrieval/cache | `app/services/knowledge_base/retrieval.py` | `get_kb_tab_text`, `list_kb_tabs` |
| Handlers | `app/ai/voice/agents/breeze_buddy/handlers/internal/kb_tab_data.py` (new) | `get_tab_data`, `list_tabs` |
| Registration | `.../handlers/internal/builtin_dispatcher.py` | `BUILTIN_HANDLERS["get_tab_data"]`, `["list_kb_tabs"]` |

Zero new migrations, zero new Pydantic config fields, zero new API routes —
everything is authored directly in template JSON like any other builtin
function. 16 new tests across the four layers, all passing; full suite
(603 tests) green with no regressions.

## Example: template configuration

Attach a knowledge base as usual, then declare the two functions on whatever
node(s) need them — exactly like adding `query_knowledge_base` or any other
builtin:

```json
{
  "configurations": {
    "knowledge_base": {
      "enabled": true,
      "knowledge_base_ids": ["3f9a1c2e-...-a1b2"],
      "mode": "tool"
    }
  },
  "flow": {
    "nodes": {
      "pricing_inquiry": {
        "task_messages": [
          {"role": "system", "content": "Help the caller with pricing questions. Use get_tab_data to look up the current price list before answering; call list_kb_tabs first if you're unsure of the exact tab name."}
        ],
        "functions": [
          {
            "type": "builtin",
            "name": "get_pricing_tab",
            "handler": "get_tab_data",
            "description": "Fetch the current pricing table as CSV-style text.",
            "properties": {
              "tab_name": {
                "type": "string",
                "description": "Exact tab name, e.g. 'Pricing'."
              }
            },
            "required": ["tab_name"]
          },
          {
            "type": "builtin",
            "name": "list_kb_tabs",
            "handler": "list_kb_tabs",
            "description": "List the data tab names available in the knowledge base."
          }
        ]
      }
    }
  }
}
```

**On `mode`:** `get_tab_data`/`list_kb_tabs` only need the KB *attached*
(`enabled` + `knowledge_base_ids`) — they ignore `mode` entirely, so any mode
works, and they're available in both voice and chat (neither is in
`CHAT_DISABLED_NAMES`). `mode: "tool"` is the natural pairing (the KB isn't
auto-injected; the LLM pulls exactly the tab it needs), which is why the
example uses it. Avoid pairing them with `full_injection` — or `auto` on a
small KB that resolves to full injection — because there the whole KB is
already in the prompt, so a `get_tab_data` call just re-hands the model a tab
it already has.

## Example: what the LLM actually sees

Turn 1 — caller asks about price, LLM doesn't know the exact tab name yet:

```
LLM calls: list_kb_tabs()
→ {"status": "success", "tabs": ["Overview", "Pricing", "Clinic Directory"]}

LLM calls: get_pricing_tab(tab_name="Pricing")
→ {
    "status": "success",
    "tab_name": "Pricing",
    "content": "### Health Companion Sheet\niPhone 15 — Price: $999\niPhone 16 — Price: $1099\n..."
  }

LLM: "The iPhone 15 is $999 and the iPhone 16 is $1099."
```

A tab name that doesn't exist (typo, or the sheet hasn't been ingested with
that tab yet) fails closed but informatively, not silently:

```
LLM calls: get_pricing_tab(tab_name="Pricng")
→ {
    "status": "not_found",
    "message": "No tab named 'Pricng' was found. Call list_kb_tabs to see available tab names."
  }
```

A DB/Redis hiccup fails open the same way every other KB path does — the
call keeps going, the LLM just doesn't get that tab this turn:

```
→ {"status": "error", "message": "Tab lookup timed out; answer without it or tell the user you could not check."}
```

## Example: manual end-to-end verification

1. Create a KB, ingest a Google Sheet that has a "Pricing" tab (existing KB
   sheets connector — nothing new here).
2. Attach the KB to a test template with the JSON above.
3. Run a chat turn: `POST /breeze-buddy/chat/message` with something like
   "how much is the iPhone 15" for that template/session.
4. Confirm in logs: `[get_tab_data] loaded tab 'Pricing' for call ... (N chars)`,
   and the assistant's reply reflects the sheet's actual price.
5. Edit the sheet's Pricing tab, wait for `kb_sheets_poll` to pick up the
   change (or trigger `POST .../documents/{id}/sync` manually), re-ask —
   confirm the answer reflects the new price (proves the shared version-stamp
   cache invalidation works without any new code for it).

## What's intentionally not handled (v1 scope)

- **Large tabs are truncated, not paginated**: `get_kb_tab_text` caps its
  result at `_TAB_TEXT_MAX_CHARS` (~32K chars ≈ 8K tokens) so a tab of up to
  `MAX_SHEET_ROWS` (10K) rows can't dump tens of thousands of tokens into the
  LLM in one tool result. Over the cap, the first portion is returned with
  `truncated: true` and a note telling the LLM to ask the user to narrow the
  request. There's no cursor/offset to page through the rest — if you
  routinely need whole large tabs, that's a `query_knowledge_base` (semantic)
  job, not a `get_tab_data` one.
- **Multi-document tab-name collisions**: if a KB ingests two sheets that
  each have a tab called "Pricing," both get concatenated under one
  `get_tab_data(tab_name="Pricing")` call. Fine for one-sheet-per-KB, which
  is the assumed setup; add a `document_id`/`source` disambiguator later if
  that assumption breaks.
- **No index on `kb_chunk.metadata`**: the tab filter still narrows by
  `kb_id` first via the existing index, so this is a bounded scan (≤25K
  chunks per KB, the existing ingestion quota), not a full-table scan. Add a
  functional index if this becomes measurably hot.
- **Freshness is ingestion-paced, not live**: a tab's content is only as
  fresh as the last successful re-ingestion (`kb_sheets_poll`, 15 min default
  + 5 min debounce, or a manual re-sync). This is consistent with how the
  rest of KB already behaves — not a new constraint introduced here — but
  worth knowing if a tab like live pricing needs faster refresh than that.
