-- Migration 023: Enable pgvector and create RAG embeddings table
--
-- pgvector is pre-installed on Cloud SQL for PostgreSQL.
-- No OS-level install needed — just CREATE EXTENSION.
--
-- Table design:
--   One row per document chunk per knowledge base (merchant + template).
--   Embeddings stored as vector(1536) — matches embed-v4.0 output dimension.
--   HNSW index for approximate nearest-neighbour search:
--     m=16, ef_construction=64 — good balance of speed vs accuracy for
--     knowledge bases up to ~50 000 chunks.
--
-- Search operator: <=> (cosine distance). Lower = more similar.
-- To get cosine similarity: 1 - (embedding <=> query_vector)
--
-- Chunks are keyed by (merchant_id, template_id, chunk_index) so a
-- re-index (upsert) replaces old chunks atomically without leaving orphans.

BEGIN;

-- Enable the pgvector extension (Cloud SQL: already installed, just needs enabling)
CREATE EXTENSION IF NOT EXISTS vector;

-- RAG embeddings table
CREATE TABLE IF NOT EXISTS rag_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    merchant_id     TEXT        NOT NULL,
    template_id     TEXT        NOT NULL,
    chunk_index     INTEGER     NOT NULL,
    source_file     TEXT        NOT NULL DEFAULT '',
    chunk_text      TEXT        NOT NULL,
    embedding       vector(1536) NOT NULL,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Unique key for upsert: one chunk per position per knowledge base
    CONSTRAINT rag_embeddings_kb_chunk_key
        UNIQUE (merchant_id, template_id, chunk_index)
);

-- HNSW index for fast cosine similarity search
-- Filters by (merchant_id, template_id) first, then ranks by vector distance.
-- Partial indexes per tenant are not needed at expected scale (<50k total chunks).
CREATE INDEX IF NOT EXISTS idx_rag_embeddings_hnsw
    ON rag_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- B-tree index to make the WHERE merchant_id=? AND template_id=? filter fast
CREATE INDEX IF NOT EXISTS idx_rag_embeddings_kb
    ON rag_embeddings (merchant_id, template_id);

-- Index on indexed_at for TTL/freshness queries from the status endpoint
CREATE INDEX IF NOT EXISTS idx_rag_embeddings_indexed_at
    ON rag_embeddings (merchant_id, template_id, indexed_at);

COMMIT;
