# Persistent User Memory — Foundation

> **PR scope:** specification, final database schema, thin database access
> layer, and configuration contracts only.
>
> Runtime extraction, queueing, backend adapters, read injection, and channel
> wiring are intentionally deferred to the next PR.

## Split and follow-up

This PR establishes the stable foundation that the runtime implementation will
consume. The already-built hardened runtime is preserved on
`feat/memory-runtime-hardening` and will be rebased onto this foundation for the
next PR. That follow-up will reuse the contracts here rather than duplicate or
replace them.

The next PR owns:

1. Customer identity resolution and phone-to-customer merge orchestration.
2. The Redis extraction queue, retry/lease behavior, and drain worker.
3. Curator extraction and pluggable pgvector/Supermemory adapters.
4. Voice/chat enqueue points and safe user-role memory injection.
5. Scheduler registration, purge wiring, metrics, and operational alerts.

Nothing in this PR reads, writes, extracts, or injects memory at runtime.

## Product contract

- Memory is shared across a merchant's opted-in voice and chat templates.
- Every fact is scoped by `(reseller_id, merchant_id, customer_key)`.
- Templates control participation only through `memory.enabled`.
- Backend, identity policy, retention, limits, and embedding selection are
  global engine settings.
- The global `BUDDY_MEMORY_ENABLED` flag remains an incident-response kill
  switch in addition to template opt-in.
- Raw transcripts stay in their source lead/chat tables. Only curated facts
  may enter a memory backend.
- Retrieved facts are untrusted user data and must be appended as user-role
  context, never as system instructions.

Template configuration:

```json
{
  "memory": {
    "enabled": true
  }
}
```

Unknown fields in the template memory object are rejected so backend or policy
overrides cannot silently become template-specific.

## Global configuration

The following accessors use the standard Redis/DevCycle, environment, and
default cascade:

```text
BUDDY_MEMORY_ENABLED=false
BUDDY_MEMORY_BACKEND=pgvector
BUDDY_MEMORY_IDENTITY_FIELD=customer_id
BUDDY_MEMORY_PHONE_FIELD=customer_mobile_number
BUDDY_MEMORY_PHONE_DEFAULT_REGION=
BUDDY_MEMORY_ALLOW_PHONE_FALLBACK=true
BUDDY_MEMORY_RETENTION_DAYS=180
BUDDY_MEMORY_EMBEDDING_PROVIDER=azure_openai
BUDDY_MEMORY_EMBEDDING_MODEL=text-embedding-3-large
MEMORY_MAX_FACTS_PER_USER=100
```

`MemoryEngineConfig` validates the combined global policy. Embedding dimensions
are fixed at 768 to match the shared knowledge-base embedding shape.

The default phone region is empty, which means E.164-only. The runtime PR may
accept local-format numbers only when operators explicitly configure a global
ISO-3166 alpha-2 region.

## Database schema

Migration `042_create_memory_tables.sql` is the only memory migration. The
pgvector extension must be enabled out-of-band by a privileged database role
before it runs.

### `user_memory`

Stores curated facts with:

- tenant/customer scope and canonical/provisional key type;
- category, structured data, confidence, source channel, and audit timestamps;
- `halfvec(768)` embedding for indexed cosine retrieval;
- deterministic operation key for idempotent inserts;
- expiry and supersession timestamps.

Constraints reject empty tenant/customer/fact values, invalid enum-like values,
and confidence outside `[0, 1]`. Partial indexes support active identity reads,
operation idempotency, expiry cleanup, and HNSW cosine search.

### `customer_identity`

Stores the tenant-scoped phone-to-customer alias. Reusing one phone with a
different customer ID marks the alias `CONFLICTED` without overwriting its
original customer ID. The runtime must fail closed for conflicted aliases.

## Database layer

The standard query → accessor → decoder pattern provides:

- tenant-scoped insert, profile listing, pgvector search, supersession, and
  provisional-key repointing;
- bounded expiry purge;
- conflict-safe alias upsert and lookup;
- typed `UserMemory`, `CustomerIdentity`, and `MemoryKey` records.

All SQL values are parameterized. Embeddings use text parameters explicitly
cast to `halfvec(768)`, so this foundation does not register a process-wide
asyncpg pgvector codec.

## Acceptance boundary

This PR is complete when:

- migration `042` creates the final schema without corrective `ALTER`/`DROP`
  steps or privileged `CREATE EXTENSION`;
- every memory query includes the required tenant/customer scope;
- similarity search executes in Postgres with `ORDER BY embedding <=> ...`;
- alias conflict SQL never overwrites the original customer mapping;
- templates expose only `memory.enabled`;
- global configuration and field-reference coverage are documented and tested.

Runtime behavior is not an acceptance criterion for this PR; it belongs to the
follow-up implementation described above.
