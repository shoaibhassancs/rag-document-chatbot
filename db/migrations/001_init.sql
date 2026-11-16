-- Initial schema. {{EMBEDDING_DIMENSIONS}} is replaced by app/migrations.py from the
-- EMBEDDING_DIMENSIONS setting (validated as an integer), so the size lives only in config.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL UNIQUE,
    source_type  text NOT NULL CHECK (source_type IN ('pdf', 'markdown', 'text', 'html', 'web')),
    source_url   text,
    content_hash text NOT NULL,
    status       text NOT NULL CHECK (status IN ('ingesting', 'ready', 'failed')),
    error        text,
    page_count   integer,
    chunk_total  integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX documents_content_hash_idx ON documents (content_hash);

CREATE TABLE chunks (
    id              bigserial PRIMARY KEY,
    document_id     uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_index     integer NOT NULL,
    text            text NOT NULL,
    chunk_hash      text NOT NULL,
    page            integer,
    section         text,
    token_estimate  integer NOT NULL,
    -- NULL until embedded, so ingestion and re-embedding can resume after a crash.
    embedding       vector({{EMBEDDING_DIMENSIONS}}),
    embedding_model text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX chunks_chunk_hash_idx ON chunks (chunk_hash);
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);

-- Key/value facts about how the vectors were built (embedding model, dimension, re-embed state).
CREATE TABLE app_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);
