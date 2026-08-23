-- Migration 034: Knowledge Base (RAG) storage.
--
-- Merchants upload documents (files now; Google Sheets / text in follow-ups)
-- into merchant-scoped knowledge bases. Documents are parsed, chunked and
-- embedded by a background ingestion task; agents retrieve at runtime via
-- hybrid search (pgvector HNSW + full-text + trigram, RRF-fused) with a
-- strict latency budget on the voice path.
--
-- Design notes:
-- * ``halfvec(768)``: all embedding providers are normalized to 768 dims via
--   Matryoshka truncation + L2 renorm, so one column/index serves every
--   provider and fp16 halves storage vs ``vector``.
-- * ``tsv`` uses the 'simple' config (no stemming): merchant content mixes
--   English/Hinglish/SKUs where stemming hurts exact matches; the trigram
--   index adds typo tolerance for the keyword leg.
-- * Template <-> KB attachment lives in template ``configurations`` JSONB
--   (same precedent as MCP config) -- no join table. KB deletion does an
--   in-use scan over template configurations at the API layer.
-- * DEPLOYMENT PREREQUISITE: the Postgres instance must allow
--   ``CREATE EXTENSION vector`` (pgvector) and ``pg_trgm`` (RDS/Cloud SQL do).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS knowledge_base (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id         varchar(255) NOT NULL,
    merchant_identifier varchar(255),
    name                varchar(255) NOT NULL,
    description         text,
    -- {"provider": "openai", "model": "text-embedding-3-small", "dimensions": 768}
    embedding_config    jsonb NOT NULL DEFAULT '{}'::jsonb,
    settings            jsonb NOT NULL DEFAULT '{}'::jsonb,
    status              varchar(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Name uniqueness must also hold for reseller-level KBs (merchant NULL):
-- a plain UNIQUE constraint treats NULLs as distinct, so mirror the
-- template table's split partial-index pattern (migration 018).
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_base_reseller_merchant_name
    ON knowledge_base (reseller_id, merchant_identifier, name)
    WHERE merchant_identifier IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_base_reseller_name_no_merchant
    ON knowledge_base (reseller_id, name)
    WHERE merchant_identifier IS NULL;

CREATE INDEX IF NOT EXISTS idx_knowledge_base_reseller
    ON knowledge_base (reseller_id);

CREATE TABLE IF NOT EXISTS kb_document (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id         uuid NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    -- 'text' is reserved for a future inline-snippet source; only 'file' and
    -- 'google_sheet' get API surface in v1.
    source_type   varchar(20) NOT NULL
        CHECK (source_type IN ('file', 'google_sheet', 'text')),
    name          varchar(512) NOT NULL,
    -- file: {"gcs_path": "..."} | google_sheet: {"spreadsheet_id": "...", "ranges": [...]}
    source_ref    jsonb NOT NULL DEFAULT '{}'::jsonb,
    mime_type     varchar(255),
    size_bytes    bigint,
    status        varchar(20) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'ERROR')),
    error_message text,
    content_hash  varchar(64),
    chunk_count   integer NOT NULL DEFAULT 0,
    token_count   integer NOT NULL DEFAULT 0,
    synced_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_kb_document_kb
    ON kb_document (kb_id);

-- The ingestion sweeper polls for claimable work; keep that scan O(pending).
CREATE INDEX IF NOT EXISTS idx_kb_document_pending
    ON kb_document (created_at)
    WHERE status IN ('PENDING', 'PROCESSING');

CREATE TABLE IF NOT EXISTS kb_chunk (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id  uuid NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    kb_id        uuid NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    chunk_index  integer NOT NULL,
    text         text NOT NULL,
    -- sha256 of the chunk text; ingestion re-embeds only chunks whose hash
    -- changed (incremental sync: edited sheet rows, appended doc sections).
    content_hash varchar(64) NOT NULL,
    embedding    halfvec(768),
    tsv          tsvector GENERATED ALWAYS AS
        (to_tsvector('simple', left(text, 100000))) STORED,
    -- headings breadcrumb, row_key/row_hash for tabular rows, lang, ...
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    token_count  integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),

    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_kb
    ON kb_chunk (kb_id);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_embedding_hnsw
    ON kb_chunk USING hnsw (embedding halfvec_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_tsv
    ON kb_chunk USING gin (tsv);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_text_trgm
    ON kb_chunk USING gin (text gin_trgm_ops);
