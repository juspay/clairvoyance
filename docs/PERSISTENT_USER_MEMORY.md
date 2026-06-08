# Persistent User Memory — Chat + Voice (Breeze Buddy)

> **Status:** Design / integration spec (not yet implemented).
> **Scope:** Breeze Buddy telephony (voice) + chat (text). Does not touch the Automatic agent.
> **Related:** [`CHAT_MODE.md`](./CHAT_MODE.md), [`BREEZE_BUDDY_ARCHITECTURE.md`](./BREEZE_BUDDY_ARCHITECTURE.md)

## 1. Context & goal

Breeze Buddy talks to the same end-customers repeatedly — across both **voice** (Twilio/Plivo/Exotel/Daily) and **chat** (text) — but every conversation starts cold. There is no persistent, cross-conversation memory of a customer's preferences, prior outcomes, or facts.

We want **one memory layer**, shared by both modes, that:

- Remembers **personalizations / facts** about a customer — *not* raw transcripts.
- **Updates with embeddings**: new facts are deduped / consolidated against what we already know, and stale facts are superseded.
- Works **identically for voice and chat** — written from either, read into either.
- Is **isolated per merchant**.

The outcome: a repeat caller/chatter is "remembered" — the agent opens with relevant context, and downstream logic can read structured facts.

## 2. Design decisions

| Dimension | Choice | Notes |
|---|---|---|
| **Identity** | Canonical `customer_id`, **phone fallback** | See §2.1. Voice reliably has only the phone number; chat/commerce has `customer_id` but usually no phone; there is **no customer table**. We resolve a `customer_key` per conversation and store voice memory under `phone:<normalized>` when no `customer_id` exists, merging into the real id later. |
| **Backend** | **Pluggable** — `pgvector` (default) or `supermemory` | A `MemoryBackend` provider interface (§3.2). `pgvector` is our thin-DIY Postgres store (three-layer DB pattern, our own embeddings + extraction); `supermemory` is the hosted [supermemory.ai](https://supermemory.ai) memory API. Selected by `BUDDY_MEMORY_BACKEND` env, overridable per-template via `MemoryConfig.backend`. |
| **Vector storage** | pgvector column `vector(1536)` **+ app-side cosine** | Each customer has only ~10–50 facts, so dedup only ever compares against *one user's small set* — no global ANN index needed. The pgvector column is there to future-proof cross-user search. |
| **What we store** | LLM-extracted **structured facts** | The transcript is only the *input* to extraction. We never persist conversation turns as "memory". |
| **Embeddings / extraction LLM** | Azure OpenAI | `text-embedding-3-small` (1536-d) for embeddings; existing Azure chat model for extraction. Reuses creds already in `static.py`. |
| **Retrieval** | Phased | **Phase 1:** inject a `<user_memory>` profile block at conversation start (both modes). **Phase 2:** on-demand `recall_about_user` global function (semantic search over the user's own facts). |
| **Scope** | Per merchant | Every query filters `reseller_id` **AND** `merchant_id`. Memory keyed by `(reseller_id, merchant_id, customer_key)` where `customer_key` is a real `customer_id` or provisional `phone:<normalized>`. |

## 2.1 Identity resolution (critical)

The hardest part of this feature is *what identifies the customer at runtime*. Verified in code (not assumed):

- **Voice → phone only.** The lead `payload` is free-form ([core.py:155](../app/schemas/breeze_buddy/core.py#L155)); `customer_mobile_number` is the only identity present across every example template, and **inbound hard-codes phone-only** — `payload={"customer_mobile_number": from_number}` ([inbound.py:87](../app/ai/voice/agents/breeze_buddy/agent/inbound.py#L87)). A `customer_id` is essentially never present today, and inbound can *never* have one at call start.
- **Chat → customer_id, usually no phone.** `customer_id` is a real concept in commerce/widget templates ([chat.py:111](../app/schemas/breeze_buddy/chat.py#L111), loaded into `agent_session_state`), but a web user usually has no phone, and the id may only resolve mid-session.
- **No customer table exists.** Tables today: `lead_call_tracker, template, users, merchants, credentials, outbound_number, call_execution_config, blacklisted_numbers, chat_session, chat_message, agent_session_state, widget_config`. There is no phone↔customer_id mapping to reuse.

**End goal:** memory keyed by the canonical `customer_id`. **Reality:** voice usually only has a phone. So we resolve a `customer_key` with a fallback chain and reconcile later.

**Resolution chain** — `memory/identity.py` → `resolve_customer_key(reseller_id, merchant_id, payload_or_vars, memory_cfg) -> Optional[(customer_key, key_type)]`, run at conversation start:

1. `customer_id` present in payload/template_vars → `(customer_id, "customer_id")`.
2. Phone present → look up the `customer_identity` alias (cache of prior resolutions). If mapped → `(customer_id, "customer_id")`.
3. Phone present, no alias → `("phone:" + normalize_phone_number(phone), "phone")` — **provisional**. Reuses the existing `normalize_phone_number` helper (blacklist keys). Voice/inbound is remembered immediately.
4. Neither → `None` ⇒ memory off for this conversation.

**Resolving an id from a phone "in some flow"** uses existing plumbing, *not* a memory-specific API call: a template's HTTP global function / pre-check that calls the merchant's "lookup by phone" endpoint already **merges its response back into `lead.payload`** ([func_action_handlers.py:36](../app/ai/voice/agents/breeze_buddy/template/func_action_handlers.py#L36), [http_handler.py:205-257](../app/ai/voice/agents/breeze_buddy/handlers/transport/http_handler.py#L205)). If a template is wired that way, step 1 simply finds the merged `customer_id`. Memory never makes its own external call.

**Merge (phone → customer_id).** When a real `customer_id` becomes linked to a phone (a resolver fires, or a chat/widget session carries both), the extraction worker upserts the `customer_identity` alias and **re-points provisional `phone:*` memory rows to the `customer_id`**, then re-runs dedup. End state: everything consolidates under the canonical id.

## 3. Architecture

One shared module; two thin call-sites per mode (one read, one write).

```text
                    ┌─────────────────────────────────────────────┐
                    │  app/ai/voice/agents/breeze_buddy/memory/    │
                    │  • identity.py    resolve_customer_key()     │
                    │  • service.py     MemoryService  (facade)    │
                    │       - get_profile_block(key)   [read,fast] │
                    │       - search(key, query)       [phase 2]   │
                    │       - enqueue_extraction(...)  [write]     │
                    │  • worker.py      drain queue → backend      │
                    │  • backends/                                 │
                    │      base.py    MemoryBackend + MemoryIdentity│
                    │      __init__   get_memory_backend(name)     │
                    │      pgvector/  extract + embeddings + store │
                    │      supermemory/ hosted API client + backend│
                    └─────────────────────────────────────────────┘

  READ  (conversation start)            WRITE (conversation end → queue → worker)
  ──────────────────────────            ─────────────────────────────────────────
  voice: agent/flow.py                  voice: end_conversation.py  → enqueue
         prepare_initial_node(          chat:  end_chat_session_handler + cleanup.py
            ..., memory_block)                  → enqueue
  chat:  ChatAgent(memory_block=...)    worker: scheduler task drains Redis queue,
            → _seed_context                     re-reads transcript, extracts,
                                                consolidates → user_memory,
                                                links phone↔id, merges phone:* rows
```

### 3.1 Why a queue + drain worker (not fire-and-forget)

Voice runs **one subprocess per call**, torn down immediately after `end_conversation`. An `asyncio.create_task()` started there can be killed mid-extraction. So on conversation end we only do a cheap **enqueue** (a Redis list item keyed by id + identity). A single background task — registered on `BackgroundTaskScheduler`, distributed-locked one-pod-per-tick — drains the queue, re-reads the transcript from the DB, runs the LLM extraction, and upserts memory.

Benefits: survives subprocess teardown and pod restarts; decouples ~1–2s extraction latency from the call; naturally rate-limits the extraction LLM. This mirrors the existing dispatch queue/worker idiom (`breeze_buddy/dispatch/queue.py`, `dispatch/worker.py`).

### 3.2 Pluggable backends

"Where memory lives and how it is extracted" is a strategy behind the `MemoryBackend` interface (`memory/backends/base.py`). Everything else — identity resolution, the Redis queue, the worker's transcript fetch, the read/enqueue call-sites — is backend-agnostic. `MemoryService` is a thin facade that delegates reads/merge to the selected backend; the worker delegates `merge_identity` + `ingest`.

```python
@dataclass
class MemoryIdentity:
    reseller_id, merchant_id, customer_key, key_type
    phone=None, explicit_customer_id=None          # only set when one convo carried both
    @property
    def scope_tag(self) -> str:                     # "reseller:merchant:customer_key"

class MemoryBackend(ABC):
    name: ClassVar[str]
    async def get_profile_block(identity, max_facts) -> Optional[str]   # <user_memory> block
    async def ingest(identity, transcript, source_channel) -> None      # extract + persist
    async def search(identity, query, k) -> list[str]                   # phase-2 recall
    async def merge_identity(identity) -> MemoryIdentity                # phone:* -> customer_id
```

**Extraction lives inside each backend** — `ingest(identity, transcript)` is the seam. Each provider plays to its strengths (the supermemory column is the *target* design — see status below):

| | **pgvector** (default) | **supermemory** |
|---|---|---|
| Store | our Postgres `user_memory` + `customer_identity` | hosted supermemory.ai |
| Extraction | app-side LLM `consolidate()` → ADD/UPDATE/DELETE ops | supermemory owns it; we POST the transcript |
| Embeddings / dedup | Azure `text-embedding-3-small` + app-side cosine | server-side |
| Namespacing | `(reseller_id, merchant_id, customer_key)` row scope | one `containerTag = scope_tag` (exact-array match) |
| Merge (phone→id) | `upsert_alias` + repoint `phone:*` rows | re-tag documents to the canonical `scope_tag` |
| Reads | SQL fetch / cosine | `POST /v3/search` filtered by container tag |

**Selection** — `get_memory_backend(name)` (`backends/__init__.py`) resolves `name` → global `BUDDY_MEMORY_BACKEND` env → `"pgvector"`, with an unknown name falling back to pgvector. A template overrides per-template via `MemoryConfig.backend`. The enqueue call-site stamps the chosen `backend` into the queue payload so the worker reconstructs the **same** backend the template chose.

**Off by default.** Nothing touches the DB, Redis, Azure, or supermemory unless `BUDDY_MEMORY_ENABLED=true`. The drain worker is registered only when enabled (`app/main.py`), the enqueue is gated (`end_conversation.py`), backend/Azure/httpx clients are created lazily on first use, and the pgvector codec registration (`app/database/__init__.py` `_init_connection`) runs **only** when `BUDDY_MEMORY_ENABLED` *and* `BUDDY_MEMORY_BACKEND=="pgvector"` (wrapped so a missing `vector` extension / un-applied migration 032 degrades with a warning instead of killing the pool). A memory-off deployment never requires the pgvector extension.

**supermemory client** — thin async wrapper (`backends/supermemory/client.py`) over the **official `supermemory` Python SDK** (`AsyncSupermemory`, typed Pydantic responses), constructed with our proxy-aware `create_http_client()` as its `http_client` so calls still honour the AWS proxy. SDK method map: `client.add(content, container_tags=[scope_tag], metadata)` (POST /v3/documents); `client.search.memories(q, container_tag=scope_tag, limit)` (POST /v4/search — returns *extracted facts*, used by both `get_profile_block` and `search`); `client.documents.list(container_tags=[scope_tag])` + `client.documents.update(id, container_tags=[…])` (merge re-tag). All best-effort: log + swallow, mirroring pgvector's fail-open posture (no key / 401 / network error → empty result). Result text is read defensively (`_memory_text`: `.memory` → chunk content). Inert unless `BUDDY_MEMORY_ENABLED=true` selects it.

## 4. Data model

New migration `032_create_memory_tables.sql` (latest is `031`; never edit existing migrations).

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- requires managed-PG support (see §11)

CREATE TABLE IF NOT EXISTS user_memory (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id    varchar(255) NOT NULL,
    merchant_id    varchar(255) NOT NULL,        -- '' for null-merchant tenants; scope is always (reseller, merchant)
    customer_key   varchar(255) NOT NULL,        -- canonical: real customer_id, OR provisional 'phone:<normalized>'
    key_type       varchar(16)  NOT NULL,        -- 'customer_id' | 'phone' (phone = provisional, mergeable)
    fact           text NOT NULL,                -- one extracted personalization, e.g. "Prefers morning calls"
    category       varchar(64),                  -- preference | attribute | outcome | context
    structured     jsonb NOT NULL DEFAULT '{}',  -- optional machine-readable form for downstream logic
    embedding      vector(1536),                 -- nullable; populated by the extraction worker
    source_channel varchar(16),                  -- 'voice' | 'chat'
    confidence     real DEFAULT 1.0,
    superseded_at  timestamptz,                  -- non-null = retired by a newer contradicting fact (kept for audit)
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_memory_identity
    ON user_memory (reseller_id, merchant_id, customer_key)
    WHERE superseded_at IS NULL;

-- Alias / resolver cache / merge ledger — the "customer table" that doesn't exist yet.
-- Records a phone <-> customer_id link the moment both are seen (a resolver fires, or a
-- session carries both). Used to (a) resolve phone->id on later voice calls without
-- re-calling the merchant API, and (b) drive the phone:* -> customer_id memory merge.
CREATE TABLE IF NOT EXISTS customer_identity (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id   varchar(255) NOT NULL,
    merchant_id   varchar(255) NOT NULL,
    phone         varchar(32)  NOT NULL,         -- normalized
    customer_id   varchar(255) NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (reseller_id, merchant_id, phone)
);
```

The memory-extraction queue lives in **Redis**, not Postgres. No ANN index in Phase 1 (reads are by identity, not similarity). The pgvector column lets Phase 2 / future cross-user search add an `ivfflat`/`hnsw` index with no schema change.

## 5. Database layer (mirror `lead_call_tracker`)

Follow the three-layer pattern exactly (`run_parameterized_query`, `decode_*`, accessor try/except + logging — see `.claude/rules/breeze-buddy.md`). All UPDATE queries explicitly set `updated_at = now()` — there is no DB trigger; the `DEFAULT now()` on the column covers INSERT only.

- `app/database/queries/breeze_buddy/user_memory.py` — `insert_user_memory_query`, `list_active_memories_query(reseller_id, merchant_id, customer_key)`, `update_memory_query`, `supersede_memory_query`, `repoint_memory_key_query` (phone→id merge). All parameterized (`$1, $2, …`).
- `app/database/queries/breeze_buddy/customer_identity.py` — `upsert_alias_query(reseller_id, merchant_id, phone, customer_id)`, `get_customer_id_for_phone_query(...)`. Drives resolution step 2 and the merge.
- `app/database/accessor/breeze_buddy/user_memory.py` — `list_user_memories(...)`, `consolidate_user_memories(...)` (upsert/supersede transaction), `merge_phone_key_into_customer_id(...)`.
- `app/database/accessor/breeze_buddy/customer_identity.py` — `upsert_alias(...)`, `get_customer_id_for_phone(...)`.
- `app/database/decoder/breeze_buddy/{user_memory,customer_identity}.py` — pure `decode_*` functions (`parse_json` for `structured`).
- `app/schemas/breeze_buddy/memory.py` — Pydantic `UserMemory` model.
- `app/database/__init__.py` — register the pgvector type on connection init (`pgvector.asyncpg.register_vector`); store/read embeddings as Python lists.

## 6. Shared memory module — `app/ai/voice/agents/breeze_buddy/memory/`

- **`identity.py`** *(backend-agnostic)* — `resolve_customer_key(reseller_id, merchant_id, payload, id_field, phone_field, allow_phone_key) -> Optional[(customer_key, key_type)]` implementing the §2.1 chain (direct `customer_id` → `customer_identity` alias → provisional `phone:<normalized>` → `None`). `merchant_id` is always a non-empty application-level value on both leads and chat sessions, so the empty-string ambiguity never reaches the DB layer.
- **`service.py`** — `MemoryService`, a thin facade holding the selected `MemoryBackend` (`get_memory_backend()`):
  - `get_profile_block(reseller_id, merchant_id, customer_key, key_type, max_facts) -> Optional[str]` — builds a `MemoryIdentity` and delegates to the backend.
  - `search(...)` — Phase-2 recall; delegates to the backend (each backend embeds/searches internally; takes a `query: str`).
  - `enqueue_extraction(kind, record_id, key…, source_channel, backend=None)` — backend-agnostic write. Idempotent per `(kind, record_id)` via a Redis `SET NX EX 7200` guard on `memory:extract:dedup:{kind}:{record_id}` (both the user-end and idle-sweep paths can reach it); stamps the chosen `backend` into the queue payload, then `rpush` onto `memory:extract:queue`.
- **`worker.py`** *(backend-agnostic)* — `drain_memory_queue()`: per item, re-read the transcript (voice: `lead.metaData["transcription"]`; chat: `list_chat_messages_for_session`), build the `MemoryIdentity`, pick the backend from `item["backend"]`, and — if the source carried **both** phone and a real `customer_id` — `backend.merge_identity(...)`, then `backend.ingest(identity, transcript, channel)`. Idempotent + best-effort per item.
- **`backends/base.py`** — the `MemoryBackend` ABC + `MemoryIdentity` dataclass (see §3.2).
- **`backends/__init__.py`** — `get_memory_backend(name)` registry/factory (default `pgvector`, unknown → pgvector).
- **`backends/pgvector/`** — `backend.py` (`PgVectorMemoryBackend`: read = SQL render, `ingest` = `consolidate` + embed + cosine-dedup + upsert/supersede, `merge_identity` = alias upsert + repoint), plus `extract.py` (Azure LLM `consolidate()` → ADD/UPDATE/DELETE ops; cosine helpers) and `embeddings.py` (`embed_texts`/`embed_single` via `AsyncAzureOpenAI`; best-effort `embedding=NULL` on failure).
- **`backends/supermemory/`** — `client.py` (wraps the official `AsyncSupermemory` SDK via our proxy-aware http client; `add`/`search_memories`/`list_documents`/`update_document`) and `backend.py` (`SupermemoryMemoryBackend`: `ingest` adds the transcript under `containerTag=scope_tag`, `get_profile_block`/`search` use v4 memories search, `merge_identity` re-tags documents). supermemory owns extraction/embedding/dedup server-side.

## 7. Write path (both modes → queue → worker)

1. **Voice** — `handlers/internal/end_conversation.py`, right after transcript collection (where `filtered_transcript` already exists): resolve key from `context.lead`; if present, `MemoryService.enqueue_extraction("voice_lead", context.lead.id, key, "voice")`. Wrapped best-effort like the existing widget-drain block — must never block `end_conversation`.
2. **Chat** — `app/api/routers/breeze_buddy/chat/handlers.py` `end_chat_session_handler` (under the session lock) **and** `chat/cleanup.py` `end_idle_chat_sessions`: resolve key from `session.metadata["template_vars"]`; enqueue `("chat_session", session_id, key, "chat")`. Both end paths are covered, mirroring how each sets `ended_reason`.
3. **Drain worker** — registered once in `app/main.py` lifespan: `scheduler.register_task("memory-extraction-drain", drain_memory_queue, interval_seconds=...)`.
4. **Identity link + merge** (same worker item) — see §2.1 / §6 `worker.py`: upsert the alias and re-point provisional `phone:*` rows to the canonical `customer_id` when both are known.

## 8. Read path — Phase 1 (profile block at start)

Symmetric; one injection point per mode, appended **after** `inject_language_rules` so memory lands in `role_messages`.

- **Voice** — add optional `memory_block: Optional[str] = None` to `prepare_initial_node` and `prepare_resume_node` (`agent/flow.py`); when present, append `{"role": "system", "content": memory_block}` to `role_messages`. Fetch in `agent/__init__.py` just before those call-sites: `resolve_customer_key(self.lead.reseller_id, self.lead.merchant_id, self.lead.payload, ...)`, then `memory_block = await MemoryService().get_profile_block(key)` (guarded by config + key presence). The fetch sits in the same `async` method that already `await`s `flow_manager.initialize`.
- **Chat** — add `memory_block: Optional[str] = None` to `ChatAgent.__init__` (`chat/agent.py`); inject it as a system message into `role_messages` in `_seed_context` before assembling `messages`. The handler `send_chat_message_handler` fetches it per turn (cheap DB read) where it already builds `template_vars` / passes `agent_state`, then `ChatAgent(..., memory_block=memory_block)`. Per-turn fetch also picks up memory written between turns.

Example injected block:

```text
<user_memory>
- Prefers morning calls
- Address updated May 2026
- Declined upsell twice
</user_memory>
```

## 9. Read path — Phase 2 (on-demand recall tool)

Add a built-in global function `recall_about_user(query)` (registered alongside the existing built-in global functions; chat can strip it via `CHAT_DISABLED_NAMES` if desired). The handler calls `MemoryService.search(key, query, k)` → returns matching facts to the LLM mid-conversation. Embeddings at read time, but only cosine over the user's ~dozens of facts ⇒ still fast. Ship after the write path is proven.

## 10. Configuration & opt-in

- **Template** (`template/types.py` → `ConfigurationModel`): `memory: Optional[MemoryConfig]`. Today `MemoryConfig` carries `enabled: bool = False` and `backend: Optional[str] = None` (per-template override of the global `BUDDY_MEMORY_BACKEND`). The fuller identity/retention surface (`id_field`, `phone_field`, `allow_phone_key`, `max_facts_injected`, `retention_days`) lands with the M2 read-injection wiring. Phone→id *resolution* is just a normal template HTTP pre-check/global-function that merges `customer_id` into the payload — no memory-specific config needed.
- **Static** (`core/config/static.py` + `.env.example`): `BUDDY_MEMORY_ENABLED` (global kill-switch), `BUDDY_MEMORY_BACKEND` (`pgvector` | `supermemory`, default `pgvector`); pgvector-only: `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `MEMORY_EXTRACTION_INTERVAL_SECONDS`, `MEMORY_EXTRACTION_BATCH_SIZE`; supermemory-only: `SUPERMEMORY_API_KEY`, `SUPERMEMORY_BASE_URL`.
- Optional **dynamic** flag (DevCycle) for staged rollout per reseller.

## 11. New dependencies / infra

- **pgvector backend:** `uv add pgvector` (the small asyncpg `vector` type adapter, **not** `mem0ai`); the **pgvector Postgres extension** enabled on the managed instance (`CREATE EXTENSION vector`, **verify first** — see §13/§15); an **Azure embedding deployment** (`text-embedding-3-small`) via existing Azure creds.
- **supermemory backend:** the official **`supermemory` Python SDK** (`uv add supermemory`; httpx + pydantic, no native/onnx deps). Needs a `SUPERMEMORY_API_KEY` and outbound reachability to `api.supermemory.ai` (the SDK is handed our proxy-aware httpx client, so it honours the AWS proxy config). The pgvector extension/Azure embedding deployment are **not** required when running supermemory-only.

## 12. Files to create / modify

**Create**

- `app/database/migrations/032_create_memory_tables.sql`
- `app/database/queries/breeze_buddy/{user_memory,customer_identity}.py`
- `app/database/accessor/breeze_buddy/{user_memory,customer_identity}.py`
- `app/database/decoder/breeze_buddy/{user_memory,customer_identity}.py`
- `app/schemas/breeze_buddy/memory.py`
- `app/ai/voice/agents/breeze_buddy/memory/{__init__,identity,service,worker}.py`
- `app/ai/voice/agents/breeze_buddy/memory/backends/{__init__,base}.py`
- `app/ai/voice/agents/breeze_buddy/memory/backends/pgvector/{__init__,backend,extract,embeddings}.py`
- `app/ai/voice/agents/breeze_buddy/memory/backends/supermemory/{__init__,backend,client}.py`

**Modify**

- `app/ai/voice/agents/breeze_buddy/agent/flow.py` — `memory_block` param on `prepare_initial_node` / `prepare_resume_node`
- `app/ai/voice/agents/breeze_buddy/agent/__init__.py` — resolve key + fetch + thread the memory block
- `app/ai/voice/agents/breeze_buddy/handlers/internal/end_conversation.py` — enqueue extraction
- `app/ai/voice/agents/breeze_buddy/chat/agent.py` — `memory_block` ctor param + inject in `_seed_context`
- `app/api/routers/breeze_buddy/chat/handlers.py` — fetch in `send_chat_message_handler`; enqueue in `end_chat_session_handler`
- `app/ai/voice/agents/breeze_buddy/chat/cleanup.py` — enqueue on idle end
- `app/ai/voice/agents/breeze_buddy/template/types.py` — `MemoryConfig` (incl. `backend`) on `ConfigurationModel`
- `app/database/__init__.py` — register pgvector type on connection init
- `app/main.py` — register the `memory-extraction-drain` background task
- `app/core/config/static.py` + `.env.example` — new env vars (`BUDDY_MEMORY_BACKEND`, `SUPERMEMORY_*`)

## 13. Verification

1. **Migration** — run `032`; confirm `CREATE EXTENSION vector` succeeds and both `user_memory` (with the `vector(1536)` column) and `customer_identity` exist.
2. **Write (voice, phone-only)** — place an **inbound / phone-only** test call (no `customer_id`); after end, confirm a queue item, then (after a drain tick) `user_memory` rows keyed `customer_key='phone:<normalized>'`, `key_type='phone'`, non-null `embedding`, sensible facts (no raw transcript text). Proves voice works day one without a resolver.
3. **Dedup** — second call restating a known fact ⇒ UPDATE (no duplicate row); a contradicting call (e.g. new address) ⇒ old row `superseded_at` set, new row active.
4. **Read (voice)** — third call from the same number ⇒ the initial-node `role_messages` contain the `<user_memory>` block and the agent references prior context.
5. **Merge (phone → customer_id)** — run a conversation carrying **both** the phone and a real `customer_id` (chat with both, or a voice lead whose template resolved one) ⇒ `customer_identity` alias upserted and the earlier `phone:*` rows re-pointed to `customer_id` (`key_type='customer_id'`), deduped.
6. **Cross-mode** — write via a **voice** call (resolved to `customer_id`), then start a **chat** session with the same `customer_id` ⇒ the chat seed includes the same block (shared identity + per-merchant scope).
7. **Scope isolation** — same key under a different `merchant_id` ⇒ no memory leaks across merchants.
8. **Graceful off** — conversation with neither id nor phone, or `memory.enabled=false` ⇒ no queue items, no fetch, no behavior change.
9. **Quality gates** — `uv run black . && uv run isort . --profile black && uv run pyrefly check`.

## 14. Phasing

- **M1** — both tables + DB layer (`user_memory` + `customer_identity`) + `identity` resolution chain (incl. phone-key fallback) + `embeddings`/`extract`/`service` + enqueue on both ends + drain worker writing facts under the resolved `customer_key`. *(Write path provable for voice phone-only.)*
- **M2** — Phase 1 read injection (voice + chat) + template/static config + opt-in + the alias-upsert & `phone:* → customer_id` **merge** in the worker. *(End-to-end "remembered customer," consolidating under the canonical id.)*
- **M3** — Phase 2 `recall_about_user` global function.

## 15. Risks / open items

- **Identity linkage** — until a merchant has a phone→id resolver (or a session that carries both), voice memory stays under provisional `phone:*` keys and never merges into a `customer_id`. That's acceptable (it still works and is correctly scoped), but cross-channel unification (voice phone ↔ chat customer_id) only happens once a linkage moment occurs. A `phone:*` key also assumes one human per number — shared/reassigned numbers could blend memory; keep facts conservative and consider confidence/age decay.
- **pgvector availability** is the one hard external dependency — verify `CREATE EXTENSION vector` is permitted on the managed Postgres before M1. **Fallback:** store the embedding as `jsonb` (float array) and keep app-side cosine; nothing else changes (per-user dedup never needed the extension). Only the column type is affected.
- **Embedding deployment** — needs an Azure `text-embedding-3-small` deployment; if unavailable, dedup degrades to exact/normalized-text match (`embedding = NULL`) until provisioned.
- **PII / retention** — the extraction prompt must avoid retaining sensitive PII; add `retention_days` + a periodic purge task (can ride the same scheduler). Per-merchant scope is enforced in every query — **never** query by `customer_key` alone.
- **Extraction cost** — one LLM call per ended conversation, off the hot path via the queue, rate-limited by the drain interval.
