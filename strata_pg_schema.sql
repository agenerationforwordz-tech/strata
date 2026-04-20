-- STRATA — PostgreSQL Schema
-- Migrated from SQLite to PostgreSQL + pgvector for concurrent multi-agent access.
-- pgvector handles similarity search IN the database (no more loading all embeddings into Python).
-- tsvector handles full-text search (replaces SQLite FTS5).
-- JSONB replaces JSON TEXT columns for tags/people (native operators, GIN indexable).
--
-- Run this ONCE on the strata_db database as the strata user.

-- Enable pgvector extension (installed via postgresql-17-pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- MAIN TABLES
-- ============================================================

-- Thoughts table — the core of Strata. Every memory lives here.
CREATE TABLE IF NOT EXISTS thoughts (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    type TEXT DEFAULT 'thought',
    tags JSONB DEFAULT '[]'::jsonb,
    people JSONB DEFAULT '[]'::jsonb,
    source TEXT DEFAULT 'manual',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_accessed TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    machine TEXT DEFAULT 'unknown',
    trigger TEXT DEFAULT 'unknown',
    status TEXT DEFAULT 'none',
    priority INTEGER DEFAULT 0,
    original_date TIMESTAMPTZ,
    -- Full-text search vector, auto-maintained by trigger below
    search_vector TSVECTOR
);

-- Embeddings table — stores 768-dim vectors via pgvector.
-- Separate from thoughts for clean separation of concerns.
-- ON DELETE CASCADE means deleting a thought auto-deletes its embedding.
CREATE TABLE IF NOT EXISTS thought_embeddings (
    thought_id INTEGER PRIMARY KEY REFERENCES thoughts(id) ON DELETE CASCADE,
    embedding vector(768) NOT NULL
);

-- Agent profiles — per-agent startup config and instructions.
CREATE TABLE IF NOT EXISTS agent_profiles (
    agent_name TEXT PRIMARY KEY,
    startup_mode TEXT DEFAULT 'standard',
    instructions TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Audit trail — every create/update/delete to thoughts is logged here.
-- This is write-only; entries are never modified or deleted.
CREATE TABLE IF NOT EXISTS thought_history (
    id SERIAL PRIMARY KEY,
    thought_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    old_content TEXT,
    new_content TEXT,
    changed_fields JSONB DEFAULT '[]'::jsonb,
    source TEXT DEFAULT 'unknown',
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

-- Per-agent API keys with granular permissions.
CREATE TABLE IF NOT EXISTS agent_keys (
    id SERIAL PRIMARY KEY,
    agent_name TEXT NOT NULL,
    api_key TEXT UNIQUE NOT NULL,
    enabled INTEGER DEFAULT 1,
    can_read INTEGER DEFAULT 1,
    can_write INTEGER DEFAULT 1,
    can_delete INTEGER DEFAULT 0,
    can_admin INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used TIMESTAMPTZ,
    notes TEXT DEFAULT ''
);

-- Dashboard auth tables (for human login, not AI clients)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    seed_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    ip_address TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS login_history (
    id SERIAL PRIMARY KEY,
    username TEXT,
    success BOOLEAN DEFAULT FALSE,
    ip_address TEXT,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

-- Vault file metadata (actual files live on disk at vault/{device}/{YYYY-MM}/{thought_id}/)
CREATE TABLE IF NOT EXISTS thought_files (
    id SERIAL PRIMARY KEY,
    thought_id INTEGER REFERENCES thoughts(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    file_hash TEXT,
    mime_type TEXT DEFAULT 'application/octet-stream',
    uploaded_at TIMESTAMPTZ DEFAULT NOW(),
    device TEXT DEFAULT 'unknown'
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Thoughts table indexes
CREATE INDEX IF NOT EXISTS idx_thoughts_created_at ON thoughts(created_at);
CREATE INDEX IF NOT EXISTS idx_thoughts_type ON thoughts(type);
CREATE INDEX IF NOT EXISTS idx_thoughts_status_priority ON thoughts(status, priority);
CREATE INDEX IF NOT EXISTS idx_thoughts_machine_created ON thoughts(machine, created_at);
CREATE INDEX IF NOT EXISTS idx_thoughts_search_vector ON thoughts USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS idx_thoughts_tags ON thoughts USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_thoughts_people ON thoughts USING GIN(people);

-- pgvector HNSW index for fast similarity search.
-- HNSW = Hierarchical Navigable Small World graph. Better than IVFFlat for < 10K vectors
-- because it doesn't need training data and has higher recall.
-- vector_cosine_ops = cosine distance operator class.
CREATE INDEX IF NOT EXISTS idx_embeddings_vector ON thought_embeddings
    USING hnsw (embedding vector_cosine_ops);

-- Audit trail indexes
CREATE INDEX IF NOT EXISTS idx_history_thought_id ON thought_history(thought_id);
CREATE INDEX IF NOT EXISTS idx_history_timestamp ON thought_history(timestamp);

-- Agent keys index (fast lookup on every API request)
CREATE INDEX IF NOT EXISTS idx_agent_keys_key ON agent_keys(api_key);

-- ============================================================
-- TRIGGERS
-- ============================================================

-- Auto-update search_vector whenever a thought is inserted or updated.
-- Content gets 'A' weight (highest), tags get 'B', people get 'C'.
-- Uses 'english' dictionary for content (stemming) and 'simple' for tags/people (exact).
CREATE OR REPLACE FUNCTION update_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(
            (SELECT string_agg(elem, ' ') FROM jsonb_array_elements_text(COALESCE(NEW.tags, '[]'::jsonb)) AS elem),
            ''
        )), 'B') ||
        setweight(to_tsvector('simple', COALESCE(
            (SELECT string_agg(elem, ' ') FROM jsonb_array_elements_text(COALESCE(NEW.people, '[]'::jsonb)) AS elem),
            ''
        )), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop existing trigger first (idempotent)
DROP TRIGGER IF EXISTS thoughts_search_vector_update ON thoughts;
CREATE TRIGGER thoughts_search_vector_update
    BEFORE INSERT OR UPDATE ON thoughts
    FOR EACH ROW
    EXECUTE FUNCTION update_search_vector();

-- ============================================================
-- PERMISSIONS
-- ============================================================

-- Grant full access to the strata user on all tables and sequences
GRANT ALL ON ALL TABLES IN SCHEMA public TO strata;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO strata;
