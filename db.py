# STRATA - Self-Hosted AI Memory Server
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under PolyForm Noncommercial 1.0.0 - see LICENSE file
#
# Database Layer - SQLite + numpy cosine similarity + FTS5 (full-text search)
#
# Why numpy instead of sqlite-vec?
# sqlite-vec's aarch64 wheel has a 32-bit binary bug on Pi OS 64-bit.
# For a personal brain (<10k entries), numpy cosine similarity is plenty fast -
# searching 10,000 embeddings takes ~5ms. No extension needed.

import sqlite3
import json
import os
import struct
import numpy as np
from datetime import datetime, timedelta
from config import DB_PATH, DATA_DIR, BACKUP_DIR, EMBEDDING_DIM, DEDUP_THRESHOLD, VAULT_DIR


def _serialize_embedding(embedding):
    """Convert a list of floats to binary blob for storage.
    Stores as raw little-endian float32 bytes - compact and fast to load.
    Marshalling integers to compact heap entries leverages low-level
    encoding to deliver tight, compressed, high-efficiency representations."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _deserialize_embedding(blob):
    """Convert binary blob back to numpy array for similarity computation."""
    n = len(blob) // 4  # 4 bytes per float32
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _safe_json_list(raw):
    """Safely parse a JSON array from a DB row. Returns empty list on failure.

    Protects against corrupt rows crashing all searches - one bad row
    with malformed JSON would otherwise take down every query that
    iterates over results. This returns [] instead of raising."""
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _escape_like(value):
    """Escape LIKE wildcards (% and _) so they're treated as literal characters.
    Without this, searching person="%" returns ALL thoughts."""
    return value.replace("%", "\\%").replace("_", "\\_")


def get_db():
    """Get a database connection.
    Each call gets a fresh connection - SQLite handles concurrency via file locks.

    QUALITY NOTE: This function is named get_db() but callers use 'conn' for the
    local variable to avoid shadowing the 'db' module name in server.py."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=10)  # Wait up to 10s for locks instead of failing
    conn.row_factory = sqlite3.Row  # Return rows as dicts
    # WAL mode: allows concurrent readers + 1 writer (no more "database is locked" errors)
    # busy_timeout: wait 5s for a lock instead of failing immediately
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")  # Enforce FK constraints (orphan prevention)
    return conn


def init_db():
    """Create all tables if they don't exist.
    Called once at server startup. Safe to call multiple times."""
    conn = get_db()
    try:

        # Main thoughts table - stores the actual content and metadata
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thoughts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                type TEXT DEFAULT 'thought',
                tags TEXT DEFAULT '[]',
                people TEXT DEFAULT '[]',
                source TEXT DEFAULT 'manual',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Embeddings table - stores vector as binary blob
        # We do similarity search in Python with numpy (fast enough for <10k entries)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thought_embeddings (
                thought_id INTEGER PRIMARY KEY,
                embedding BLOB NOT NULL,
                FOREIGN KEY (thought_id) REFERENCES thoughts(id)
            )
        """)

        # Full-text search index - FTS5 for keyword/phrase search as a fallback
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS thoughts_fts USING fts5(
                content,
                tags,
                people,
                content=''
            )
        """)

        # --- STRATA watermark ---
        # Origin tracking table. Records who created this database and when.
        # This is how we trace copies back to the source project if someone
        # forks, renames, and tries to pass it off as their own work.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _strata_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Insert watermark values (INSERT OR IGNORE so we don't overwrite on restart)
        conn.execute("INSERT OR IGNORE INTO _strata_meta (key, value) VALUES ('project', 'STRATA')")
        conn.execute("INSERT OR IGNORE INTO _strata_meta (key, value) VALUES ('author', 'Chris Mitchell / A Generation Forwordz Foundation')")
        conn.execute("INSERT OR IGNORE INTO _strata_meta (key, value) VALUES ('license', 'PolyForm Noncommercial 1.0.0')")
        conn.execute("INSERT OR IGNORE INTO _strata_meta (key, value) VALUES ('repo', 'https://github.com/agenerationforwordz-tech/strata')")
        conn.execute("INSERT OR IGNORE INTO _strata_meta (key, value) VALUES ('created', ?)", (datetime.now().isoformat(),))

        # Internal schema version tracking - used by migration scripts
        # to detect which version of the DB schema is installed
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _schema_info (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("INSERT OR IGNORE INTO _schema_info (key, value) VALUES ('schema_version', '2.0')")
        conn.execute("INSERT OR IGNORE INTO _schema_info (key, value) VALUES ('engine', 'strata-core')")
        conn.execute("INSERT OR IGNORE INTO _schema_info (key, value) VALUES ('origin', 'cm-agfw-2026')")
        conn.execute("INSERT OR IGNORE INTO _schema_info (key, value) VALUES ('sig', '636872697374696e616e206d69746368656c6c')")

        # --- Schema migrations ---
        # Add access tracking columns if they don't exist yet.
        # SQLite doesn't have IF NOT EXISTS for ALTER TABLE, so we check
        # the column list first. These columns track how often each thought
        # gets retrieved - "hot" thoughts that get accessed a lot are more
        # valuable, while untouched ones can be flagged as stale.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(thoughts)").fetchall()}

        if "last_accessed" not in existing_cols:
            conn.execute("ALTER TABLE thoughts ADD COLUMN last_accessed TIMESTAMP DEFAULT NULL")
            print("[db] Added last_accessed column to thoughts table")

        if "access_count" not in existing_cols:
            conn.execute("ALTER TABLE thoughts ADD COLUMN access_count INTEGER DEFAULT 0")
            print("[db] Added access_count column to thoughts table")

        # Machine origin - which device uploaded this thought (surface, helios, telegram, etc.)
        if "machine" not in existing_cols:
            conn.execute("ALTER TABLE thoughts ADD COLUMN machine TEXT DEFAULT 'unknown'")
            print("[db] Added machine column to thoughts table")

        # Trigger - how the capture was initiated:
        #   "auto"      = AI auto-captured during a session
        #   "requested" = User explicitly asked the AI to save it
        #   "manual"    = Typed in directly (bot, API, dashboard)
        if "trigger" not in existing_cols:
            conn.execute("ALTER TABLE thoughts ADD COLUMN trigger TEXT DEFAULT 'unknown'")
            print("[db] Added trigger column to thoughts table")

        # --- File Vault attachments ---
        # Links files stored in the vault to their parent thoughts.
        # Each thought can have 0-500 attached files (code, docs, archives, etc.)
        # The vault_path is relative to VAULT_DIR so it's portable across machines.
        # checksum enables dedup - same file attached to two thoughts won't be
        # flagged as a problem, but you can detect it.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thought_id INTEGER NOT NULL,
                vault_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                mime_type TEXT DEFAULT 'application/octet-stream',
                checksum TEXT,
                device TEXT DEFAULT 'unknown',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (thought_id) REFERENCES thoughts(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_thought_id ON attachments(thought_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_checksum ON attachments(checksum)")

        # --- Performance indexes ---
        # Without these, queries filtering by created_at or type do full table scans.
        # At 10K+ rows, these indexes make list_recent, search_advanced, and
        # generate_report significantly faster.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_created_at ON thoughts(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_type ON thoughts(type)")

        conn.commit()
    finally:
        conn.close()
    print(f"[db] Database initialized at {DB_PATH}")


def store_thought(content, embedding, thought_type="thought", tags=None, people=None, source="manual", machine="unknown", trigger="unknown"):
    """Store a new thought with its embedding.

    Args:
        content: The actual text of the thought
        embedding: 768-dim float list from the embedding model
        thought_type: Category (thought, decision, session, person, insight, project, etc.)
        tags: List of string tags for filtering
        people: List of people names mentioned
        source: Where this came from (manual, claude-code, codex, telegram, migration)
        machine: Which device uploaded this (surface, helios, telegram, pi-nas, etc.)
        trigger: How capture was initiated (auto, requested, manual)

    Returns:
        The new thought's ID
    """
    tags = tags or []
    people = people or []
    conn = get_db()
    try:

        # Insert the thought itself
        cursor = conn.execute(
            "INSERT INTO thoughts (content, type, tags, people, source, machine, trigger) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (content, thought_type, json.dumps(tags), json.dumps(people), source, machine, trigger)
        )
        thought_id = cursor.lastrowid

        # Validate embedding dimensions before storing - if the model changes,
        # mismatched embeddings become invisible to search (skipped at read time).
        # Better to fail loudly at write time than silently lose data at read time.
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(f"Embedding dimension mismatch: got {len(embedding)}, expected {EMBEDDING_DIM}")

        # Insert the vector embedding as a binary blob
        conn.execute(
            "INSERT INTO thought_embeddings (thought_id, embedding) VALUES (?, ?)",
            (thought_id, _serialize_embedding(embedding))
        )

        # Insert into FTS index for keyword search
        conn.execute(
            "INSERT INTO thoughts_fts (rowid, content, tags, people) VALUES (?, ?, ?, ?)",
            (thought_id, content, " ".join(tags), " ".join(people))
        )

        conn.commit()
    finally:
        conn.close()
    return thought_id


def find_duplicates(embedding, threshold=None):
    """Check if any existing thoughts are too similar to a new one.

    Used before capture to prevent near-duplicates from piling up.
    Compares the proposed embedding against all existing embeddings
    using cosine similarity. Returns any matches above the threshold.

    Args:
        embedding: 768-dim float list of the proposed new thought
        threshold: Similarity threshold (default from config: 0.85)

    Returns:
        List of dicts with id, content preview, and similarity score
        for any existing thoughts above the threshold. Empty list = no dupes.
    """
    if threshold is None:
        threshold = DEDUP_THRESHOLD

    conn = get_db()
    try:

        # Load all existing embeddings (same pattern as search_similar)
        rows = conn.execute("""
            SELECT e.thought_id, e.embedding, t.content
            FROM thought_embeddings e
            JOIN thoughts t ON t.id = e.thought_id
        """).fetchall()

    finally:
        conn.close()

    if not rows:
        return []

    # Compare against all stored embeddings
    query_vec = np.array(embedding, dtype=np.float32)
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)

    duplicates = []
    for row in rows:
        stored_vec = _deserialize_embedding(row["embedding"])
        # Skip embeddings with wrong dimensions (from model switch or corruption)
        if len(stored_vec) != EMBEDDING_DIM:
            continue
        stored_norm = stored_vec / (np.linalg.norm(stored_vec) + 1e-10)
        similarity = float(np.dot(query_norm, stored_norm))

        if similarity >= threshold:
            # Return a preview - first 150 chars of the existing content
            content = row["content"]
            preview = content[:150] + "..." if len(content) > 150 else content
            duplicates.append({
                "id": row["thought_id"],
                "preview": preview,
                "similarity": round(similarity, 4),
            })

    # Sort by similarity (highest first) so the most obvious dupe is on top
    duplicates.sort(key=lambda x: x["similarity"], reverse=True)
    return duplicates


def search_similar(query_embedding, limit=10, threshold=0.0):
    """Find thoughts most similar to the query by cosine similarity.

    This is the core semantic search - finds thoughts by MEANING, not keywords.
    "job change" will find "thinking about switching careers" because the
    embeddings land near each other in meaning-space.

    How it works:
    1. Load all embeddings from the DB (they're small - 10k entries = ~30MB)
    2. Compute cosine similarity between query and all stored embeddings
    3. Filter by minimum threshold (if set)
    4. Return the top N most similar

    For <10k entries this takes ~5ms. Plenty fast for a personal brain.

    Args:
        query_embedding: 768-dim float list
        limit: Max results to return
        threshold: Minimum cosine similarity score (0.0 = return everything,
                   0.5 = moderate match, 0.7 = strong match). Default 0.0.

    Returns list of dicts with id, content, type, tags, people, source, created_at, similarity
    """
    conn = get_db()
    try:

        # Get all embeddings
        rows = conn.execute("""
            SELECT e.thought_id, e.embedding, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger
            FROM thought_embeddings e
            JOIN thoughts t ON t.id = e.thought_id
        """).fetchall()

        if not rows:
            return []

        # Build numpy matrix of all stored embeddings for fast batch comparison
        query_vec = np.array(query_embedding, dtype=np.float32)
        thought_ids = []
        embeddings = []
        metadata = []

        for row in rows:
            emb = _deserialize_embedding(row["embedding"])
            # Skip embeddings with wrong dimensions (from model switch or corruption)
            if len(emb) != EMBEDDING_DIM:
                continue
            thought_ids.append(row["thought_id"])
            embeddings.append(emb)
            metadata.append({
                "id": row["thought_id"],
                "content": row["content"],
                "type": row["type"],
                "tags": _safe_json_list(row["tags"]),
                "people": _safe_json_list(row["people"]),
                "source": row["source"],
                "created_at": row["created_at"],
                "machine": row["machine"] or "unknown",
                "trigger": row["trigger"] or "unknown",
            })

        if not embeddings:
            return []

        # Stack into matrix and compute cosine similarity in one shot
        emb_matrix = np.stack(embeddings)  # Shape: (N, 768)
        # Cosine similarity: dot product of normalized vectors
        # fastembed already normalizes, but let's be safe
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
        emb_norms = emb_matrix / (np.linalg.norm(emb_matrix, axis=1, keepdims=True) + 1e-10)
        similarities = emb_norms @ query_norm  # Shape: (N,)

        # Sort by similarity (highest first) and take top N
        top_indices = np.argsort(similarities)[::-1]

        results = []
        for idx in top_indices:
            sim = float(similarities[idx])
            # Skip results below the minimum threshold - filters out noise
            if sim < threshold:
                continue
            result = metadata[idx].copy()
            result["similarity"] = round(sim, 4)
            results.append(result)
            if len(results) >= limit:
                break

    finally:
        conn.close()
    return results


def find_related_by_id(thought_id, limit=5):
    """Find thoughts most similar to an existing thought by its stored embedding.

    Instead of searching by text query, this says "find more like THIS one"
    using the thought's already-computed embedding. Skips embedding generation
    entirely - just a vector lookup + cosine comparison.

    Args:
        thought_id: ID of the thought to find relatives for
        limit: Max results to return (default 5)

    Returns:
        List of similar thoughts (excluding the source thought itself), or None if not found
    """
    conn = get_db()
    try:

        # Get the source thought's embedding
        source_row = conn.execute(
            "SELECT embedding FROM thought_embeddings WHERE thought_id = ?", (thought_id,)
        ).fetchone()

        if not source_row:
            return None

        source_embedding = _deserialize_embedding(source_row["embedding"])

        # Get all other embeddings
        rows = conn.execute("""
            SELECT e.thought_id, e.embedding, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger
            FROM thought_embeddings e
            JOIN thoughts t ON t.id = e.thought_id
            WHERE e.thought_id != ?
        """, (thought_id,)).fetchall()

    finally:
        conn.close()

    if not rows:
        return []

    # Same cosine similarity pattern as search_similar, but using stored embedding
    query_vec = np.array(source_embedding, dtype=np.float32)
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)

    embeddings = []
    metadata = []
    for row in rows:
        emb = _deserialize_embedding(row["embedding"])
        # Skip embeddings with wrong dimensions
        if len(emb) != EMBEDDING_DIM:
            continue
        embeddings.append(emb)
        metadata.append({
            "id": row["thought_id"],
            "content": row["content"],
            "type": row["type"],
            "tags": _safe_json_list(row["tags"]),
            "people": _safe_json_list(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
        })

    if not embeddings:
        return []

    emb_matrix = np.stack(embeddings)
    emb_norms = emb_matrix / (np.linalg.norm(emb_matrix, axis=1, keepdims=True) + 1e-10)
    similarities = emb_norms @ query_norm

    top_indices = np.argsort(similarities)[::-1][:limit]

    results = []
    for idx in top_indices:
        result = metadata[idx].copy()
        result["similarity"] = round(float(similarities[idx]), 4)
        results.append(result)

    return results


def hybrid_search(query_text, query_embedding, limit=10, keyword_weight=0.3, threshold=0.0):
    """Blended search: FTS5 keyword (BM25) + vector (cosine) similarity.

    Combines the precision of keyword matching with the flexibility of semantic
    search. A query for "CarPi HUD" will boost results that literally contain
    those words AND find semantically related thoughts about the car dashboard.

    Score formula: (keyword_weight * bm25_normalized) + ((1 - keyword_weight) * cosine_similarity)

    Args:
        query_text: The raw search query string (for FTS5 BM25)
        query_embedding: 768-dim float list (for cosine similarity)
        limit: Max results to return
        keyword_weight: How much to weight keyword matches (0.0-1.0, default 0.3)
        threshold: Minimum final blended score to include (default 0.0)

    Returns:
        List of result dicts with id, content, similarity (blended score), match_type
    """
    conn = get_db()
    try:

        # --- STEP 1: Get BM25 keyword scores from FTS5 ---
        # FTS5 rank() returns negative values (more negative = better match)
        # We need to normalize these to 0-1 range
        fts_scores = {}
        try:
            # Wrap entire query in double quotes to treat as literal phrase.
            # FTS5 has operators (AND, OR, NOT, NEAR, *, column:) that could be exploited.
            # Quoting makes it a literal string search instead of an operator query.
            safe_query = '"' + query_text.replace('"', '""') + '"'
            fts_rows = conn.execute("""
                SELECT rowid, rank
                FROM thoughts_fts
                WHERE thoughts_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (safe_query, limit * 3)).fetchall()

            if fts_rows:
                # Normalize BM25 scores to 0-1 range (rank is negative, more negative = better)
                ranks = [abs(row["rank"]) for row in fts_rows]
                max_rank = max(ranks) if ranks else 1
                for row in fts_rows:
                    # Flip: higher is better, normalize to 0-1
                    fts_scores[row["rowid"]] = abs(row["rank"]) / max_rank if max_rank > 0 else 0
        except Exception:
            # FTS5 query might fail on special characters - fall back to vector-only
            pass

        # --- STEP 2: Get cosine similarity scores ---
        rows = conn.execute("""
            SELECT e.thought_id, e.embedding, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger
            FROM thought_embeddings e
            JOIN thoughts t ON t.id = e.thought_id
        """).fetchall()

    finally:
        conn.close()

    if not rows:
        return []

    query_vec = np.array(query_embedding, dtype=np.float32)
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)

    embeddings = []
    metadata = {}
    for row in rows:
        tid = row["thought_id"]
        emb = _deserialize_embedding(row["embedding"])
        # Skip embeddings with wrong dimensions
        if len(emb) != EMBEDDING_DIM:
            continue
        embeddings.append((tid, emb))
        metadata[tid] = {
            "id": tid,
            "content": row["content"],
            "type": row["type"],
            "tags": _safe_json_list(row["tags"]),
            "people": _safe_json_list(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
        }

    # Compute cosine similarities - vectorized with numpy (same approach as search_similar)
    # This replaces the scalar Python loop for ~10x speedup at scale
    cosine_scores = {}
    if embeddings:
        tids = [tid for tid, _ in embeddings]
        emb_matrix = np.stack([emb for _, emb in embeddings])
        emb_norms = emb_matrix / (np.linalg.norm(emb_matrix, axis=1, keepdims=True) + 1e-10)
        similarities = emb_norms @ query_norm
        cosine_scores = {tid: float(sim) for tid, sim in zip(tids, similarities)}

    # --- STEP 3: Blend scores ---
    all_ids = set(cosine_scores.keys())
    blended = []
    for tid in all_ids:
        cos = cosine_scores.get(tid, 0)
        kw = fts_scores.get(tid, 0)
        score = (keyword_weight * kw) + ((1 - keyword_weight) * cos)

        if score < threshold:
            continue

        result = metadata[tid].copy()
        result["similarity"] = round(score, 4)
        # Tell the caller what matched - useful for debugging search quality
        result["match_type"] = "both" if kw > 0 and cos > 0 else ("keyword" if kw > 0 else "semantic")
        blended.append(result)

    # Sort by blended score (highest first) and trim
    blended.sort(key=lambda r: r["similarity"], reverse=True)
    return blended[:limit]


def search_advanced(filters, limit=20):
    """Multi-filter search with combined conditions.

    Build a SQL query dynamically from whatever filters are provided.
    Supports filtering by: type, tag, person, source, machine, date range.

    Args:
        filters: Dict with optional keys:
           - type: thought type to filter by
           - tag: tag to filter by (case-insensitive)
           - person: person to filter by (partial, case-insensitive)
           - source: source to filter by
           - machine: machine to filter by
           - date_from: ISO date string (inclusive)
           - date_to: ISO date string (inclusive)
        limit: Max results (default 20)

    Returns:
        List of matching thought dicts, newest first
    """
    conn = get_db()
    try:

        # Start building the query - use JOINs only when filtering by tag or person
        # since those require json_each() on the JSON arrays
        needs_tag_join = "tag" in filters and filters["tag"]
        needs_person_join = "person" in filters and filters["person"]

        query = "SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.access_count, t.last_accessed FROM thoughts t"
        conditions = []
        params = []

        if needs_tag_join:
            query += ", json_each(t.tags) jt"
            conditions.append("LOWER(jt.value) = LOWER(?)")
            params.append(filters["tag"])

        if needs_person_join:
            query += ", json_each(t.people) jp"
            conditions.append("LOWER(jp.value) LIKE LOWER(?) ESCAPE '\\'")
            params.append(f"%{_escape_like(filters['person'])}%")

        if "type" in filters and filters["type"]:
            conditions.append("t.type = ?")
            params.append(filters["type"])

        if "source" in filters and filters["source"]:
            conditions.append("t.source = ?")
            params.append(filters["source"])

        if "machine" in filters and filters["machine"]:
            conditions.append("t.machine = ?")
            params.append(filters["machine"])

        if "date_from" in filters and filters["date_from"]:
            conditions.append("t.created_at >= ?")
            params.append(filters["date_from"])

        if "date_to" in filters and filters["date_to"]:
            conditions.append("t.created_at <= ?")
            params.append(filters["date_to"])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY t.created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": _safe_json_list(row["tags"]),
            "people": _safe_json_list(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "access_count": row["access_count"] or 0,
            "last_accessed": row["last_accessed"],
        })

    return results


def generate_report(days=7):
    """Generate a trend report comparing current period vs previous period.

    Looks at the last N days vs the N days before that. Shows:
   - Total thoughts captured in each period
   - Rising tags (>50% increase) and declining tags (>30% decrease)
   - Activity by machine and source
   - Most accessed thoughts (hottest memories)

    NO consolidation. NO decay. Every memory is permanent and valuable.
    This is purely analytical - nothing gets archived or deleted.

    Args:
        days: Number of days for the current period (default 7)

    Returns:
        Dict with report data
    """
    conn = get_db()
    try:
        now = datetime.now()
        current_start = (now - timedelta(days=days)).isoformat()
        previous_start = (now - timedelta(days=days * 2)).isoformat()

        # Current period stats
        current_count = conn.execute(
            "SELECT COUNT(*) FROM thoughts WHERE created_at >= ?", (current_start,)
        ).fetchone()[0]

        previous_count = conn.execute(
            "SELECT COUNT(*) FROM thoughts WHERE created_at >= ? AND created_at < ?",
            (previous_start, current_start)
        ).fetchone()[0]

        # Tag trends: current period
        current_tags = {}
        rows = conn.execute("""
            SELECT LOWER(j.value) as tag, COUNT(*) as cnt
            FROM thoughts t, json_each(t.tags) j
            WHERE t.created_at >= ?
            GROUP BY LOWER(j.value) ORDER BY cnt DESC LIMIT 20
        """, (current_start,)).fetchall()
        for row in rows:
            current_tags[row["tag"]] = row["cnt"]

        # Tag trends: previous period
        previous_tags = {}
        rows = conn.execute("""
            SELECT LOWER(j.value) as tag, COUNT(*) as cnt
            FROM thoughts t, json_each(t.tags) j
            WHERE t.created_at >= ? AND t.created_at < ?
            GROUP BY LOWER(j.value) ORDER BY cnt DESC LIMIT 20
        """, (previous_start, current_start)).fetchall()
        for row in rows:
            previous_tags[row["tag"]] = row["cnt"]

        # Compute rising and declining tags
        rising = []
        declining = []
        all_tags = set(list(current_tags.keys()) + list(previous_tags.keys()))
        for tag in all_tags:
            cur = current_tags.get(tag, 0)
            prev = previous_tags.get(tag, 0)
            if prev == 0 and cur > 0:
                rising.append({"tag": tag, "current": cur, "previous": 0, "change": "new"})
            elif prev > 0:
                pct = ((cur - prev) / prev) * 100
                if pct > 50:
                    rising.append({"tag": tag, "current": cur, "previous": prev, "change": f"+{pct:.0f}%"})
                elif pct < -30:
                    declining.append({"tag": tag, "current": cur, "previous": prev, "change": f"{pct:.0f}%"})

        # Activity by machine (current period)
        machine_rows = conn.execute("""
            SELECT COALESCE(machine, 'unknown') as machine, COUNT(*) as cnt
            FROM thoughts WHERE created_at >= ?
            GROUP BY machine ORDER BY cnt DESC
        """, (current_start,)).fetchall()
        by_machine = {row["machine"]: row["cnt"] for row in machine_rows}

        # Activity by source (current period)
        source_rows = conn.execute("""
            SELECT source, COUNT(*) as cnt
            FROM thoughts WHERE created_at >= ?
            GROUP BY source ORDER BY cnt DESC
        """, (current_start,)).fetchall()
        by_source = {row["source"]: row["cnt"] for row in source_rows}

        # Hottest memories (most accessed overall)
        hot_rows = conn.execute("""
            SELECT id, content, type, access_count, last_accessed
            FROM thoughts
            WHERE access_count > 0
            ORDER BY access_count DESC
            LIMIT 10
        """).fetchall()
        hottest = []
        for row in hot_rows:
            preview = row["content"][:100] + "..." if len(row["content"]) > 100 else row["content"]
            hottest.append({
                "id": row["id"],
                "preview": preview,
                "type": row["type"],
                "access_count": row["access_count"],
                "last_accessed": row["last_accessed"],
            })

        # Type breakdown for current period
        type_rows = conn.execute("""
            SELECT type, COUNT(*) as cnt
            FROM thoughts WHERE created_at >= ?
            GROUP BY type ORDER BY cnt DESC
        """, (current_start,)).fetchall()
        by_type = {row["type"]: row["cnt"] for row in type_rows}

    finally:
        conn.close()

    return {
        "period_days": days,
        "current_period": {
            "thoughts_captured": current_count,
            "by_type": by_type,
            "by_machine": by_machine,
            "by_source": by_source,
        },
        "previous_period": {
            "thoughts_captured": previous_count,
        },
        "change": f"{((current_count - previous_count) / max(previous_count, 1)) * 100:+.0f}%" if previous_count else "no previous data",
        "trending": {
            "rising": rising,
            "declining": declining,
        },
        "hottest_memories": hottest,
    }


def record_access(thought_ids):
    """Bump access_count and last_accessed for the given thought IDs.

    Called by search tools AFTER returning results - tracks which thoughts
    are actively being retrieved. Over time this reveals which memories are
    "hot" (frequently accessed) vs "cold" (never looked at since creation).

    Uses single batch UPDATE with WHERE id IN (...) instead
    of N individual updates. Fewer write locks, faster execution.

    Args:
        thought_ids: List of thought IDs that were just returned to a caller
    """
    if not thought_ids:
        return

    conn = get_db()
    try:
        now = datetime.now().isoformat()

        # Batch update - single query for all IDs (much more efficient than N separate updates)
        placeholders = ",".join("?" * len(thought_ids))
        conn.execute(
            f"UPDATE thoughts SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({placeholders})",
            [now] + list(thought_ids)
        )

        conn.commit()
    finally:
        conn.close()


def list_recent(limit=20, hours=168, offset=0):
    """Get the most recent thoughts within a time window.
    Default: last 7 days (168 hours). Good for "what was I thinking about this week?"
    Supports offset for pagination (infinite scroll).
    """
    conn = get_db()
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

        rows = conn.execute("""
            SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                   t.created_at, t.machine, t.trigger,
                   (SELECT COUNT(*) FROM attachments a WHERE a.thought_id = t.id) AS file_count
            FROM thoughts t
            WHERE t.created_at >= ?
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
        """, (cutoff, limit, offset)).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "content": row["content"],
                "type": row["type"],
                "tags": _safe_json_list(row["tags"]),
                "people": _safe_json_list(row["people"]),
                "source": row["source"],
                "created_at": row["created_at"],
                "machine": row["machine"] or "unknown",
                "trigger": row["trigger"] or "unknown",
                "has_files": row["file_count"] > 0,
            })

    finally:
        conn.close()
    return results


def search_by_tag(tag, limit=20):
    """Find all thoughts with a specific tag.
    Tags are stored as JSON arrays, so we use JSON contains check."""
    conn = get_db()
    try:

        rows = conn.execute("""
            SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source,
                   t.created_at, t.machine, t.trigger,
                   (SELECT COUNT(*) FROM attachments a WHERE a.thought_id = t.id) AS file_count
            FROM thoughts t, json_each(t.tags) j
            WHERE LOWER(j.value) = LOWER(?)
            ORDER BY t.created_at DESC
            LIMIT ?
        """, (tag, limit)).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "content": row["content"],
                "type": row["type"],
                "tags": _safe_json_list(row["tags"]),
                "people": _safe_json_list(row["people"]),
                "source": row["source"],
                "created_at": row["created_at"],
                "machine": row["machine"] or "unknown",
                "trigger": row["trigger"] or "unknown",
                "has_files": row["file_count"] > 0,
            })

    finally:
        conn.close()
    return results


def search_by_person(person, limit=20):
    """Find all thoughts that mention a specific person.
    Case-insensitive partial match - "chris" matches "Chris Mitchell"."""
    conn = get_db()
    try:

        rows = conn.execute("""
            SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger
            FROM thoughts t, json_each(t.people) j
            WHERE LOWER(j.value) LIKE LOWER(?) ESCAPE '\'
            ORDER BY t.created_at DESC
            LIMIT ?
        """, (f"%{_escape_like(person)}%", limit)).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "content": row["content"],
                "type": row["type"],
                "tags": _safe_json_list(row["tags"]),
                "people": _safe_json_list(row["people"]),
                "source": row["source"],
                "created_at": row["created_at"],
                "machine": row["machine"] or "unknown",
                "trigger": row["trigger"] or "unknown",
            })

    finally:
        conn.close()
    return results


def update_thought(thought_id, content=None, thought_type=None, tags=None, people=None, new_embedding=None):
    """Update an existing thought's fields.

    Only the fields you pass get updated - everything else stays the same.
    If content changes, the caller should also pass new_embedding (re-embedded text).
    This keeps the embedding in sync with the content for accurate semantic search.

    Args:
        thought_id: The ID of the thought to update
        content: New text content (triggers re-embedding if new_embedding also provided)
        thought_type: New type category
        tags: New tags list (replaces existing tags entirely)
        people: New people list (replaces existing people entirely)
        new_embedding: New 768-dim embedding (required when content changes)

    Returns:
        True if the thought was found and updated, False if not found
    """
    conn = get_db()
    try:

        # Check the thought exists AND fetch old values for FTS5 contentless delete.
        # FTS5 content='' tables need the OLD values passed to the delete command.
        existing = conn.execute(
            "SELECT id, content, tags, people FROM thoughts WHERE id = ?", (thought_id,)
        ).fetchone()
        if not existing:
            return False

        # Save old values - needed for FTS5 contentless delete syntax below
        old_content = existing["content"]
        old_tags = _safe_json_list(existing["tags"])
        old_people = _safe_json_list(existing["people"])

        # Build the UPDATE query dynamically - only set fields that were provided
        updates = []
        params = []

        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if thought_type is not None:
            updates.append("type = ?")
            params.append(thought_type)
        if tags is not None:
            updates.append("tags = ?")
            params.append(json.dumps(tags))
        if people is not None:
            updates.append("people = ?")
            params.append(json.dumps(people))

        # Apply the updates to the thoughts table
        if updates:
            params.append(thought_id)
            conn.execute(f"UPDATE thoughts SET {', '.join(updates)} WHERE id = ?", params)

        # If content changed and we got a new embedding, update the vector too
        if new_embedding is not None:
            conn.execute(
                "UPDATE thought_embeddings SET embedding = ? WHERE thought_id = ?",
                (_serialize_embedding(new_embedding), thought_id)
            )

        # Rebuild the FTS index entry for this thought.
        # FTS5 contentless tables (content='') CANNOT use plain DELETE.
        # Instead, use the special INSERT('delete',...) command with the OLD values,
        # then INSERT the new values. This is a SQLite FTS5 requirement:
        # https://www.sqlite.org/fts5.html#contentless_tables
        row = conn.execute(
            "SELECT content, tags, people FROM thoughts WHERE id = ?", (thought_id,)
        ).fetchone()
        if row:
            new_tags = _safe_json_list(row["tags"])
            new_people = _safe_json_list(row["people"])
            # Remove old FTS entry using contentless delete syntax
            conn.execute(
                "INSERT INTO thoughts_fts(thoughts_fts, rowid, content, tags, people) VALUES('delete', ?, ?, ?, ?)",
                (thought_id, old_content, " ".join(old_tags), " ".join(old_people))
            )
            # Insert updated FTS entry
            conn.execute(
                "INSERT INTO thoughts_fts (rowid, content, tags, people) VALUES (?, ?, ?, ?)",
                (thought_id, row["content"], " ".join(new_tags), " ".join(new_people))
            )

        conn.commit()
    finally:
        conn.close()
    return True


def delete_thought(thought_id):
    """Permanently remove a thought from the database.

    Deletes from all three tables: thoughts, thought_embeddings, and thoughts_fts.
    This is irreversible - use with care.

    Args:
        thought_id: The ID of the thought to delete

    Returns:
        True if the thought was found and deleted, False if not found
    """
    conn = get_db()
    try:

        # Check the thought exists before trying to delete
        existing = conn.execute("SELECT id FROM thoughts WHERE id = ?", (thought_id,)).fetchone()
        if not existing:
            return False

        # Fetch old values for FTS5 contentless delete (must pass original values)
        row = conn.execute(
            "SELECT content, tags, people FROM thoughts WHERE id = ?", (thought_id,)
        ).fetchone()

        # Delete from all three tables
        conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (thought_id,))
        # FTS5 contentless tables: use special INSERT('delete',...) instead of plain DELETE.
        # Plain DELETE raises "cannot DELETE from contentless fts5 table".
        if row:
            old_tags = _safe_json_list(row["tags"])
            old_people = _safe_json_list(row["people"])
            conn.execute(
                "INSERT INTO thoughts_fts(thoughts_fts, rowid, content, tags, people) VALUES('delete', ?, ?, ?, ?)",
                (thought_id, row["content"], " ".join(old_tags), " ".join(old_people))
            )
        conn.execute("DELETE FROM thoughts WHERE id = ?", (thought_id,))

        conn.commit()
    finally:
        conn.close()
    return True


def delete_thought_full(thought_id):
    """Delete a thought AND its attachment records in a single transaction.

    This is the atomic version - if the server crashes mid-operation,
    either everything is deleted or nothing is. No orphaned state.

    Returns:
        Tuple of (success: bool, vault_paths: list) so caller can clean up files.
        Vault file deletion happens AFTER the DB transaction succeeds.
    """
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM thoughts WHERE id = ?", (thought_id,)).fetchone()
        if not existing:
            return False, []

        # Collect vault paths before deleting records (need them for file cleanup)
        att_rows = conn.execute("SELECT vault_path FROM attachments WHERE thought_id = ?", (thought_id,)).fetchall()
        vault_paths = [row["vault_path"] for row in att_rows]

        # Delete attachments
        conn.execute("DELETE FROM attachments WHERE thought_id = ?", (thought_id,))

        # Delete embedding
        conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (thought_id,))

        # FTS5 contentless delete
        row = conn.execute("SELECT content, tags, people FROM thoughts WHERE id = ?", (thought_id,)).fetchone()
        if row:
            old_tags = _safe_json_list(row["tags"])
            old_people = _safe_json_list(row["people"])
            conn.execute(
                "INSERT INTO thoughts_fts(thoughts_fts, rowid, content, tags, people) VALUES('delete', ?, ?, ?, ?)",
                (thought_id, row["content"], " ".join(old_tags), " ".join(old_people))
            )

        # Delete the thought itself
        conn.execute("DELETE FROM thoughts WHERE id = ?", (thought_id,))

        conn.commit()
        return True, vault_paths
    finally:
        conn.close()


def get_thought_by_id(thought_id):
    """Fetch a single thought by its ID. Returns dict or None if not found.

    Useful for confirming a thought exists before updating/deleting,
    or for showing the user what they're about to modify.
    Includes access tracking fields and attached file list.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, content, type, tags, people, source, created_at, last_accessed, access_count, machine, trigger FROM thoughts WHERE id = ?",
            (thought_id,)
        ).fetchone()

        if not row:
            return None

        # Fetch attachments for this thought - shows what files are linked
        attachment_rows = conn.execute(
            """SELECT id, vault_path, filename, file_size, mime_type, checksum, device, created_at
               FROM attachments WHERE thought_id = ? ORDER BY created_at ASC""",
            (thought_id,)
        ).fetchall()
    finally:
        conn.close()

    attachments = [
        {
            "id": r["id"],
            "vault_path": r["vault_path"],
            "filename": r["filename"],
            "file_size": r["file_size"],
            "mime_type": r["mime_type"],
            "device": r["device"],
        }
        for r in attachment_rows
    ]

    return {
        "id": row["id"],
        "content": row["content"],
        "type": row["type"],
        "tags": _safe_json_list(row["tags"]),
        "people": _safe_json_list(row["people"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "last_accessed": row["last_accessed"],
        "access_count": row["access_count"] or 0,
        "machine": row["machine"] or "unknown",
        "trigger": row["trigger"] or "unknown",
        "attachments": attachments,
        "has_files": len(attachments) > 0,
    }


def get_stats():
    """Get database statistics - total thoughts, type breakdown, top tags, top people, db size."""
    conn = get_db()
    try:

        total = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]

        # Count by type
        type_rows = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM thoughts GROUP BY type ORDER BY cnt DESC"
        ).fetchall()
        types = {row["type"]: row["cnt"] for row in type_rows}

        # Count by source
        source_rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM thoughts GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        sources = {row["source"]: row["cnt"] for row in source_rows}

        # Count by machine - shows which devices are contributing thoughts
        machine_rows = conn.execute(
            "SELECT COALESCE(machine, 'unknown') as machine, COUNT(*) as cnt FROM thoughts GROUP BY machine ORDER BY cnt DESC"
        ).fetchall()
        machines = {row["machine"]: row["cnt"] for row in machine_rows}

        # Count by trigger - shows auto vs requested vs manual breakdown
        trigger_rows = conn.execute(
            "SELECT COALESCE(trigger, 'unknown') as trigger, COUNT(*) as cnt FROM thoughts GROUP BY trigger ORDER BY cnt DESC"
        ).fetchall()
        triggers = {row["trigger"]: row["cnt"] for row in trigger_rows}

        # Top 10 tags
        tag_rows = conn.execute("""
            SELECT j.value as tag, COUNT(*) as cnt
            FROM thoughts t, json_each(t.tags) j
            GROUP BY LOWER(j.value)
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()
        top_tags = {row["tag"]: row["cnt"] for row in tag_rows}

        # Top 10 people
        people_rows = conn.execute("""
            SELECT j.value as person, COUNT(*) as cnt
            FROM thoughts t, json_each(t.people) j
            GROUP BY LOWER(j.value)
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()
        top_people = {row["person"]: row["cnt"] for row in people_rows}

        # Database file size
        db_size_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        db_size_mb = round(db_size_bytes / (1024 * 1024), 2)

    finally:
        conn.close()

    # Vault stats - how many files, total size on disk
    vault_stats = None
    try:
        import vault as vault_module
        vault_stats = vault_module.get_vault_stats()
    except Exception:
        pass  # vault module might not be available yet

    result = {
        "total_thoughts": total,
        "by_type": types,
        "by_source": sources,
        "by_machine": machines,
        "by_trigger": triggers,
        "top_tags": top_tags,
        "top_people": top_people,
        "db_size_mb": db_size_mb,
    }
    if vault_stats:
        result["vault"] = vault_stats

    return result


# ============================================================
# FILE VAULT ATTACHMENTS
# ============================================================
# These functions manage the attachments table - the link between
# thoughts (semantic index) and actual files (stored in vault/).
# A thought can have 0-500 attachments. When an AI retrieves a
# thought, it sees the attachment list and can request any file.

def store_attachment(thought_id, vault_path, filename, file_size=0,
                     mime_type="application/octet-stream", checksum=None, device="unknown"):
    """Record a file attachment in the database.

    This is the DB side - the actual file should already be stored in the vault
    by vault.store_file() before calling this. We just record the metadata.

    Args:
        thought_id: Which thought this file belongs to
        vault_path: Path relative to VAULT_DIR (from vault.store_file())
        filename: Display filename
        file_size: Size in bytes
        mime_type: MIME type string
        checksum: SHA-256 hex digest
        device: Which device uploaded this

    Returns:
        The new attachment's ID
    """
    conn = get_db()
    try:
        cursor = conn.execute(
            """INSERT INTO attachments
               (thought_id, vault_path, filename, file_size, mime_type, checksum, device)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (thought_id, vault_path, filename, file_size, mime_type, checksum, device)
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_attachments(thought_id):
    """Get all file attachments for a thought.

    Returns a list of attachment metadata dicts. Does NOT return file
    content - use vault.read_file(vault_path) for that.

    Args:
        thought_id: The thought to get attachments for

    Returns:
        List of dicts with: id, vault_path, filename, file_size, mime_type, checksum, device, created_at
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, vault_path, filename, file_size, mime_type, checksum, device, created_at
               FROM attachments WHERE thought_id = ? ORDER BY created_at ASC""",
            (thought_id,)
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "id": row["id"],
            "vault_path": row["vault_path"],
            "filename": row["filename"],
            "file_size": row["file_size"],
            "mime_type": row["mime_type"],
            "checksum": row["checksum"],
            "device": row["device"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_attachment_by_id(attachment_id):
    """Get a single attachment's metadata by its ID.

    Args:
        attachment_id: The attachment record ID

    Returns:
        Dict with attachment metadata, or None if not found
    """
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, thought_id, vault_path, filename, file_size, mime_type, checksum, device, created_at
               FROM attachments WHERE id = ?""",
            (attachment_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None

    return {
        "id": row["id"],
        "thought_id": row["thought_id"],
        "vault_path": row["vault_path"],
        "filename": row["filename"],
        "file_size": row["file_size"],
        "mime_type": row["mime_type"],
        "checksum": row["checksum"],
        "device": row["device"],
        "created_at": row["created_at"],
    }


def delete_attachment(attachment_id):
    """Remove a single attachment record from the database.

    NOTE: This only removes the DB record. The caller should also
    delete the actual file from the vault using vault.delete_file().

    Args:
        attachment_id: The attachment record to delete

    Returns:
        The vault_path of the deleted attachment (so caller can delete the file), or None
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT vault_path FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()

        if not row:
            return None

        vault_path = row["vault_path"]
        conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        conn.commit()
        return vault_path
    finally:
        conn.close()


def delete_attachments_for_thought(thought_id):
    """Remove ALL attachment records for a thought.

    Called when a thought is deleted. Returns the vault_paths
    so the caller can clean up the actual files.

    Args:
        thought_id: The thought whose attachments should be removed

    Returns:
        List of vault_path strings for file cleanup
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT vault_path FROM attachments WHERE thought_id = ?", (thought_id,)
        ).fetchall()

        paths = [row["vault_path"] for row in rows]

        if paths:
            conn.execute("DELETE FROM attachments WHERE thought_id = ?", (thought_id,))
            conn.commit()

        return paths
    finally:
        conn.close()


def count_attachments(thought_id):
    """Count how many attachments a thought has.

    Used to enforce MAX_ATTACHMENTS_PER_THOUGHT before adding more.

    Args:
        thought_id: The thought to count attachments for

    Returns:
        Integer count
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE thought_id = ?", (thought_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def find_attachment_by_checksum(checksum):
    """Find an existing attachment with the same checksum (same file content).

    Used for dedup - if you try to attach a file that's already in the vault
    (attached to any thought), we can warn you or just link to the existing copy.

    Args:
        checksum: SHA-256 hex digest to search for

    Returns:
        Dict with attachment metadata, or None if no match
    """
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, thought_id, vault_path, filename, file_size
               FROM attachments WHERE checksum = ? LIMIT 1""",
            (checksum,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None

    return {
        "id": row["id"],
        "thought_id": row["thought_id"],
        "vault_path": row["vault_path"],
        "filename": row["filename"],
        "file_size": row["file_size"],
    }
