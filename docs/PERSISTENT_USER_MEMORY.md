# Persistent User Memory — Runtime Core

> **Status:** This follow-up builds the runtime core on the foundation from PR
> #800. Channel wiring and scheduler registration remain intentionally deferred
> to the integration PR.
>
> **Scope:** Breeze Buddy voice and chat. The Automatic agent is unaffected.

## Guarantees

- Memory is opt-in twice: the dynamic `BUDDY_MEMORY_ENABLED` kill switch and
  `ConfigurationModel.memory.enabled` must both be true.
- Every fact is scoped by `(reseller_id, merchant_id, customer_key)`.
- The worker sends only curator-produced facts to storage backends. Raw
  transcripts remain in their source lead/chat tables.
- Reads fail open. Failed writes raise into a crash-safe Redis retry path.
- Conversational facts are untrusted user data. `render_memory_user_tail()`
  produces a JSON-escaped user-role block; memory must never be injected as a
  system instruction.

## Runtime resolution

`resolve_memory_runtime()` is the only runtime entry point. It applies:

1. Template opt-in.
2. Dynamic global kill switch.
3. Non-empty reseller and merchant scope.
4. Strict global engine and credential validation.
5. Customer identity resolution using the global identity policy.

The resulting `ResolvedMemoryRuntime` contains the validated global
`MemoryEngineConfig` and resolved customer identity used by `MemoryService`.

Template memory configuration:

```json
{
  "memory": {
    "enabled": true
  }
}
```

Templates cannot override engine policy. The global engine is resolved
dynamically through the standard Redis/DevCycle, environment, and default
cascade:

```text
BUDDY_MEMORY_BACKEND
BUDDY_MEMORY_IDENTITY_FIELD
BUDDY_MEMORY_PHONE_FIELD
BUDDY_MEMORY_PHONE_DEFAULT_REGION
BUDDY_MEMORY_ALLOW_PHONE_FALLBACK
BUDDY_MEMORY_RETENTION_DAYS
BUDDY_MEMORY_EMBEDDING_PROVIDER
BUDDY_MEMORY_EMBEDDING_MODEL
MEMORY_MAX_FACTS_PER_USER
```

Embedding dimensions remain fixed at 768. Unknown backends/providers, empty
field names, invalid regions, and out-of-range retention or fact limits disable
memory rather than silently falling back. The curator uses the existing global
Breeze Buddy Azure LLM configuration and has no template override.

## Identity

`resolve_memory_identity()` returns the full observed identity:

- A non-empty configured customer ID is canonical.
- A phone must parse as a valid E.164 number. International input begins with
  `+`; local-format input requires the global
  `BUDDY_MEMORY_PHONE_DEFAULT_REGION`. Its default is empty, so memory is
  E.164-only unless operators explicitly configure a region.
- A known active phone alias resolves to its canonical customer ID.
- An unknown valid phone may use provisional key `phone:+<country><number>`.
- Alias database errors, invalid/ambiguous phones, missing tenant scope, and
  conflicted aliases disable memory for that conversation.

When a conversation observes both a phone and explicit customer ID, the worker
merges regardless of which identifier was selected initially. The alias upsert,
provisional-row repoint, and fact deduplication are one transaction. Reusing a
phone with another customer ID marks the alias `CONFLICTED`; the original
mapping is not overwritten.

Logs use a short scope digest and never include phones, customer IDs, facts, or
transcripts.

## Extraction queue

`MemoryService.enqueue_extraction()` stores a validated
`MemoryExtractionJob`. It contains a source-record reference and runtime
snapshot, not transcript content. The worker re-reads the transcript from
Postgres.

The dedicated Redis queue uses one Cluster hash tag:

```text
memory:{memory-extraction}:payloads
memory:{memory-extraction}:scheduled
memory:{memory-extraction}:processing
memory:{memory-extraction}:leases
memory:{memory-extraction}:completed
```

Lua scripts make enqueue/dedup, claim, ack, retry, and poison transitions
atomic:

- The scheduled ZSET holds due jobs.
- Claim moves jobs to a processing ZSET with a visibility deadline.
- Claim tokens prevent an expired worker from acknowledging a newer lease.
- A later claim recovers expired processing leases after worker/pod crashes.
- Transient failures retry with bounded exponential backoff.
- Success removes the payload and records bounded completion deduplication.
- Exhausted, permanent, or malformed jobs discard the payload and retain only
  TTL-bounded sanitized failure metadata.

`drain_memory_queue()` exists but is not registered with
`BackgroundTaskScheduler` in this PR. Channel end handlers also do not enqueue
yet. Both changes belong to the integration PR.

## Curator and backend contract

One structured tool call produces validated discriminated operations:

```text
ADD(fact, category?, structured?, confidence?)
UPDATE(fact, supersedes_fact, category?, structured?, confidence?)
DELETE(fact)
```

The transcript and known facts are explicitly marked untrusted in the curator
prompt. An empty `operations` array is a valid no-op. Missing tool calls,
provider failures, and invalid operation shapes raise for queue retry.

Extraction is backend-neutral. `MemoryBackend` stores already-extracted facts:

```python
list_facts(identity, limit) -> list[MemoryFact]
apply_operations(identity, operations, source_channel, operation_key,
                 retention_days, max_facts, embedding_config)
search(identity, query, embedding_config, k) -> list[MemoryFact]
merge_identity(identity) -> MemoryIdentity
```

## Pgvector backend

Pgvector reuses the shared embedding provider registry:

- Dynamic `KB_AZURE_OPENAI_ENDPOINT` and `KB_AZURE_OPENAI_API_KEY`.
- Proxy-aware shared HTTP session.
- `EmbeddingConfig` provider/model snapshot.
- Matryoshka truncation and normalization to `halfvec(768)`.
- Text vector parameters (`$N::halfvec(768)`), so no process-wide asyncpg
  pgvector codec or Python `pgvector` package is needed.

Each operation batch is one database transaction. It:

1. Locks/supersedes exact update/delete targets.
2. Deduplicates inserts by normalized text or cosine distance.
3. Inserts with a deterministic operation key.
4. Applies the active-fact cap after the complete batch.

Reads filter superseded and expired rows and order profile facts by confidence
then recency. Identity merges perform exact/semantic deduplication in the same
transaction.

Migration `042` creates the complete final-state schema, including
`halfvec(768)`, operation idempotency, expiry, validation constraints,
HNSW/expiry indexes, and alias-conflict state. It contains no corrective
`ALTER` or `DROP` steps because the memory tables are new and have not been
deployed.

`purge_expired_user_memories()` hard-deletes a bounded batch. Reads enforce
expiry immediately, but periodic hard deletion begins only when the integration
PR registers the purge task.

## Supermemory backend

Supermemory uses the provider's direct extracted-memory APIs instead of raw
document/conversation ingestion:

- `POST /v4/memories` for extracted facts.
- `PATCH /v4/memories` for versioned updates and `forgetAfter`.
- `DELETE /v4/memories` for curator deletes.
- `POST /v4/search` for scoped fact recall.
- `POST /v3/container-tags/merge` for phone-to-customer merges.

The adapter uses the shared proxy-aware aiohttp transport and resolves
`SUPERMEMORY_API_KEY` dynamically for rotation. Container tags are opaque
versioned SHA-256 values; hosted metadata contains operation/source metadata,
never raw tenant/customer identifiers.

Remote retries are idempotent through deterministic operation metadata and
exact-result reconciliation. HTTP 429/5xx/transport errors retry; permanent
4xx configuration/request errors go to the sanitized poison path.

Supermemory documents `forgetAfter` and per-memory delete as **soft forget**:
expired facts leave search results but may remain in the provider database.
This limitation is accepted for tenants choosing this backend. Deployments
requiring hard deletion must select pgvector.

## Deferred integration PR

The next PR must:

1. Resolve runtime state and enqueue from both voice and chat end paths using
   the same deterministic end-event key.
2. Register the extraction drain and pgvector purge entrypoints with the
   background scheduler.
3. Fetch facts for voice/chat and append `render_memory_user_tail()` output as
   user-role data.
4. Add the optional scoped recall tool.
5. Add rollout metrics, queue-depth/poison alerts, and an enablement runbook.

Until those changes land, this foundation is inert in production even when a
template opts into memory.
