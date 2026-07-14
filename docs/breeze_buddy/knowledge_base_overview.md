# Knowledge Base (RAG) — How It Works

This describes the existing pgvector-backed Knowledge Base system (three merged
commits: `feat: knowledge base core`, `feat: knowledge base runtime`, plus the
Google Sheets connector on `feat/knowledge-base-sheets`). It is the
"embedding/RAG" system that templates attach via `configurations.knowledge_base`.
Everything in this doc already exists on `upstream/release` — nothing here was
built by this plan; it's the foundation the tab-retrieval addition (see
`docs/breeze_buddy/kb_tab_retrieval.md`) builds on top of.

## The shape of it

A merchant-scoped **Knowledge Base** holds one or more **documents** (a file
upload or a Google Sheet). Each document is split into **chunks** — for prose,
heading-aware ~450-token chunks; for a spreadsheet, **one chunk per row**, with
the tab name and column headers folded into the chunk text and metadata.
Chunks are embedded and stored in Postgres with pgvector.

```
knowledge_base          (merchant/reseller-scoped entity)
  └── kb_document        (one row per uploaded file or ingested Sheet tab range)
        └── kb_chunk     (embedded rows/paragraphs; halfvec(768) + tsvector + trigram)
```

Migration: `app/database/migrations/034_knowledge_base.sql`.

## Ingestion pipeline

```
Upload / Sync
  → kb_document row inserted, status = PENDING
  → scheduler task claims it (FOR UPDATE SKIP LOCKED, batched)
  → connector.load()          — FileConnector or GoogleSheetsConnector
  → build_chunks()            — chunking.py: prose or table rows
  → hash-diff (SHA-256 per chunk) — only re-embed what actually changed
  → embed changed chunks, upsert into kb_chunk
  → status = READY
  → bump_kb_version()         — INCR kb:ver:{kb_id} in Redis
```

Code: `app/services/knowledge_base/ingestion.py`, `chunking.py`,
`connectors/{base,file_upload,google_sheets}.py`.

**Why chunk hashing matters**: a 1,000-row sheet where 3 rows changed
re-embeds only those 3 chunks — not the whole document. This is a real cost
control, not an optimization detail; embedding calls are the expensive part
of ingestion.

**Sheets freshness**: `kb_sheets_poll` (a `BackgroundTaskScheduler` task)
probes each ingested sheet's Drive `modifiedTime` on an interval (15 min
default, 5 min debounce) and requeues the document to PENDING if it changed.
There's also a manual `POST .../documents/{id}/sync` for "sync now."

## What a chunk actually looks like

For a spreadsheet row, `chunking.py::chunk_table` builds:

```python
text = f"{table.name} — col1: val1 | col2: val2 | ..."
metadata = {"table": table.name, "row_key": f"{table.name}:{row_index}"}
```

So **every row-chunk carries its source tab name in `metadata.table`**. This
one fact is the entire basis for the tab-retrieval addition — the data
needed for deterministic "give me this tab's rows" was already there, just
never queried that way.

## Retrieval — four ways the LLM sees KB content

Set per-template via `configurations.knowledge_base.mode`
(`KnowledgeBaseConfig` in `template/types.py`):

| Mode | Mechanism | When content reaches the LLM |
|---|---|---|
| `full_injection` | Whole KB text concatenated into the initial node's `role_messages`, fetched at call boot | Always in context from turn 1 |
| `auto_retrieve` | `KnowledgeRetrievalProcessor` (a Pipecat frame processor) runs hybrid search per user turn, injects a transient system message that **replaces** (not accumulates) the previous one | Per-turn, LLM never sees it as a tool call |
| `tool` | LLM explicitly calls the builtin `query_knowledge_base(query)` function | Only when the LLM decides to ask |
| `auto` (default) | Resolves to `full_injection` if the KB is small enough (`full_injection_token_threshold`), else `auto_retrieve` | Decided once, at boot |

Realtime speech-to-speech LLMs can't do mid-stream tool calls or injected
system messages reliably, so they're restricted to `full_injection` and
`tool` (`validate_template_compat` enforces this).

## Hybrid retrieval — the actual search

`app/services/knowledge_base/retrieval.py::retrieve()` is one SQL round trip
that fuses three signals with Reciprocal Rank Fusion:

| Leg | Index | Catches |
|---|---|---|
| Vector KNN | HNSW, `halfvec_cosine_ops` | Semantic similarity |
| Full-text search | GIN, `tsvector` | Exact keyword matches |
| Trigram | GIN, `gin_trgm_ops` | Typos, partial SKU matches |

`score = Σ 1/(60 + rank_leg)` across whichever legs matched a chunk. Only
`READY` chunks are ever served — chunks mid-re-embed or in an ERROR state are
invisible to retrieval.

Callers own their own timeout and **always fail open**: voice budgets ~0.4s,
chat ~1s, tool mode ~5s. A KB miss or timeout means the LLM answers without
that context, not that the call breaks.

## The version-stamped cache — why it needs no invalidation logic

`get_full_kb_text()` and `get_kb_token_count()` cache their results in Redis
under a key that embeds the KB's current version counter:

```python
versions = [redis.get(f"kb:ver:{kb_id}") for kb_id in kb_ids]
cache_key = f"{prefix}:{sha1(kb_ids + versions)}"
```

Ingestion does `INCR kb:ver:{kb_id}` on every successful publish. That's the
entire invalidation mechanism — old cache keys aren't deleted, they just
become unreachable once the version moves, and expire on their own via TTL.
No `SCAN`/pattern-delete, no explicit cache-bust call anywhere in the
ingestion path. This pattern is reused as-is by the tab-retrieval addition.

## Key files

| File | Role |
|---|---|
| `app/database/migrations/034_knowledge_base.sql` | Schema: 3 tables, HNSW + GIN indexes |
| `app/services/knowledge_base/ingestion.py` | PENDING → READY pipeline |
| `app/services/knowledge_base/chunking.py` | Prose + table chunking, hash-diff |
| `app/services/knowledge_base/connectors/` | `FileConnector`, `GoogleSheetsConnector` |
| `app/services/knowledge_base/retrieval.py` | `retrieve()`, `get_full_kb_text()`, `get_kb_token_count()` |
| `app/database/accessor/breeze_buddy/knowledge_base.py` | DB access layer |
| `app/database/queries/breeze_buddy/knowledge_base.py` | Raw parameterized SQL builders |
| `app/ai/voice/agents/breeze_buddy/processors/knowledge_retrieval.py` | `KnowledgeRetrievalProcessor` (auto_retrieve mode) |
| `app/ai/voice/agents/breeze_buddy/handlers/internal/query_knowledge_base.py` | `query_knowledge_base` builtin (tool mode) |
| `app/ai/voice/agents/breeze_buddy/template/types.py` | `KnowledgeBaseConfig` |

## What this system deliberately does not do

- No tab-scoped retrieval — `full_injection`/`auto_retrieve`/`tool` all treat
  a spreadsheet as an undifferentiated pool of row-chunks to search or dump.
  There was no way to say "give me exactly the Pricing tab" — that's the gap
  `kb_tab_retrieval.md` closes.
- No separate "data source" abstraction — Sheets and files are both just
  `kb_document`s. Anything ingested becomes chunks; there's no raw-fetch path
  that bypasses ingestion.
