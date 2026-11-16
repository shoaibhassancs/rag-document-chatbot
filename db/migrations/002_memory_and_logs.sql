-- Milestone 4: short conversation memory and the exchange log. Additive only: no change to the
-- Milestone 1 and 2 tables.

-- One row per chat session. `last_status` is the status of the previous exchange, so the service can
-- enforce "never two clarifying questions in a row" in code.
CREATE TABLE chat_sessions (
    session_id     text PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_active_at timestamptz NOT NULL DEFAULT now(),
    last_status    text
);

CREATE INDEX chat_sessions_last_active_idx ON chat_sessions (last_active_at);

-- One row per exchange. The text is stored with emails and phone numbers masked.
-- Deleting a session (expiry) deletes its turns.
CREATE TABLE chat_turns (
    id                bigserial PRIMARY KEY,
    session_id        text NOT NULL REFERENCES chat_sessions (session_id) ON DELETE CASCADE,
    created_at        timestamptz NOT NULL DEFAULT now(),
    user_message      text NOT NULL,
    assistant_message text NOT NULL,
    status            text NOT NULL
);

CREATE INDEX chat_turns_session_idx ON chat_turns (session_id, id);

-- One row per /chat exchange, for tracing which chunks produced each answer. Text columns and the
-- jsonb columns are masked before they are written. No foreign keys to chunks: chunks can be
-- replaced when a document is re-ingested. `session_id` is NULL when the caller sent none.
CREATE TABLE chat_logs (
    id                    bigserial PRIMARY KEY,
    created_at            timestamptz NOT NULL DEFAULT now(),
    session_id            text,
    question              text NOT NULL,
    standalone_query      text,
    clarify_action        text,
    status                text NOT NULL,
    answer                text NOT NULL,
    retrieved_chunk_ids   integer[] NOT NULL DEFAULT '{}',
    used_chunk_ids        integer[] NOT NULL DEFAULT '{}',
    similarities          jsonb,
    latency_ms            integer,
    timings_ms            jsonb,
    llm_usage             jsonb,
    prompt_version        text,
    clarify_prompt_version text,
    error                 text
);

CREATE INDEX chat_logs_created_idx ON chat_logs (created_at);
CREATE INDEX chat_logs_session_idx ON chat_logs (session_id);
