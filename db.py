# STRATA — Database Layer
# SQLite + numpy cosine similarity + FTS5 (full-text search)
# Default database file is data/strata.db. config.py handles filename
# resolution so this module just imports DB_PATH and uses whatever it gets.
#
# Why numpy instead of sqlite-vec?
# sqlite-vec's aarch64 wheel has a 32-bit binary bug on Pi OS 64-bit.
# For a personal brain (<10k entries), numpy cosine similarity is plenty fast —
# searching 10,000 embeddings takes ~5ms. No extension needed.

import sqlite3
import json
import os
import struct
import numpy as np
from datetime import datetime, timedelta
from config import DB_PATH, DATA_DIR, BACKUP_DIR, EMBEDDING_DIM, DEDUP_THRESHOLD

# --- Encryption at rest (Option 2: SQLCipher) ---
# If STRATA_DB_ENCRYPT=true, uses pysqlcipher3 instead of sqlite3.
# The encryption key is read from the file specified in STRATA_DB_KEY_FILE.
# Without these env vars, Strata runs with plain sqlite3 (default).
_DB_ENCRYPT = os.environ.get("STRATA_DB_ENCRYPT", "").lower() == "true"
_DB_KEY = None
if _DB_ENCRYPT:
    _key_file = os.environ.get("STRATA_DB_KEY_FILE", "/etc/strata/db.key")
    try:
        with open(_key_file, "r") as _kf:
            _DB_KEY = _kf.read().strip()
        # Import pysqlcipher3 as our sqlite3 replacement
        from pysqlcipher3 import dbapi2 as sqlcipher
        print(f"[db] Encryption ENABLED — using SQLCipher (key from {_key_file})")
    except ImportError:
        print("[db] WARNING: STRATA_DB_ENCRYPT=true but pysqlcipher3 not installed. Run setup_encryption.sh")
        _DB_ENCRYPT = False
    except FileNotFoundError:
        print(f"[db] WARNING: Key file {_key_file} not found. Encryption disabled.")
        _DB_ENCRYPT = False

VALID_STATUSES = {"none", "open", "in_progress", "done"}


def _normalize_status(status):
    """Normalize status to a safe known value."""
    if status is None:
        return "none"
    status = str(status).strip().lower()
    return status if status in VALID_STATUSES else "none"


def _normalize_priority(priority):
    """Normalize priority to int in 0-5 range."""
    if priority is None:
        return 0
    try:
        value = int(priority)
    except Exception:
        return 0
    return max(0, min(5, value))


def _serialize_embedding(embedding):
    """Convert a list of floats to binary blob for storage.
    Stores as raw little-endian float32 bytes — compact and fast to load."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _deserialize_embedding(blob):
    """Convert binary blob back to numpy array for similarity computation."""
    n = len(blob) // 4  # 4 bytes per float32
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def get_db():
    """Get a database connection.
    Each call gets a fresh connection — SQLite handles concurrency via file locks.

    QUALITY NOTE: This function is named get_db() but callers use 'conn' for the
    local variable to avoid shadowing the 'db' module name in server.py."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Use SQLCipher if encryption is enabled, otherwise plain sqlite3
    if _DB_ENCRYPT and _DB_KEY:
        conn = sqlcipher.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlcipher.Row
        # Unlock the encrypted database with our key
        conn.execute(f"PRAGMA key='{_DB_KEY}'")
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30)  # 30s safety net — write queue handles contention
        conn.row_factory = sqlite3.Row  # Return rows as dicts
    # WAL mode: allows concurrent readers + 1 writer (no more "database is locked" errors)
    # busy_timeout: wait 5s for a lock instead of failing immediately
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    """Create all tables if they don't exist.
    Called once at server startup. Safe to call multiple times."""
    conn = get_db()

    # Main thoughts table — stores the actual content and metadata
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

    # Agent-specific startup defaults/instructions.
    # This gives each model a deterministic profile (codex, surface, helios, etc.).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_profiles (
            agent_name TEXT PRIMARY KEY,
            startup_mode TEXT DEFAULT 'standard',
            instructions TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Embeddings table — stores vector as binary blob
    # We do similarity search in Python with numpy (fast enough for <10k entries)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thought_embeddings (
            thought_id INTEGER PRIMARY KEY,
            embedding BLOB NOT NULL,
            FOREIGN KEY (thought_id) REFERENCES thoughts(id)
        )
    """)

    # Full-text search index — FTS5 for keyword/phrase search as a fallback
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS thoughts_fts USING fts5(
            content,
            tags,
            people,
            content=''
        )
    """)

    # --- Schema migrations ---
    # Add access tracking columns if they don't exist yet.
    # SQLite doesn't have IF NOT EXISTS for ALTER TABLE, so we check
    # the column list first. These columns track how often each thought
    # gets retrieved — "hot" thoughts that get accessed a lot are more
    # valuable, while untouched ones can be flagged as stale.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(thoughts)").fetchall()}

    if "last_accessed" not in existing_cols:
        conn.execute("ALTER TABLE thoughts ADD COLUMN last_accessed TIMESTAMP DEFAULT NULL")
        print("[db] Added last_accessed column to thoughts table")

    if "access_count" not in existing_cols:
        conn.execute("ALTER TABLE thoughts ADD COLUMN access_count INTEGER DEFAULT 0")
        print("[db] Added access_count column to thoughts table")

    # Machine origin — which device uploaded this thought (surface, helios, telegram, etc.)
    if "machine" not in existing_cols:
        conn.execute("ALTER TABLE thoughts ADD COLUMN machine TEXT DEFAULT 'unknown'")
        print("[db] Added machine column to thoughts table")

    # Trigger — how the capture was initiated:
    #   "auto"      = Claude auto-captured during a session
    #   "requested" = Chris explicitly asked Claude to save it
    #   "manual"    = Chris typed it in directly (bot, API, dashboard)
    if "trigger" not in existing_cols:
        conn.execute("ALTER TABLE thoughts ADD COLUMN trigger TEXT DEFAULT 'unknown'")
        print("[db] Added trigger column to thoughts table")

    # Lightweight task state for execution workflows.
    if "status" not in existing_cols:
        conn.execute("ALTER TABLE thoughts ADD COLUMN status TEXT DEFAULT 'none'")
        print("[db] Added status column to thoughts table")

    if "priority" not in existing_cols:
        conn.execute("ALTER TABLE thoughts ADD COLUMN priority INTEGER DEFAULT 0")
        print("[db] Added priority column to thoughts table")

    # Original date — for legacy/pre-Strata imports.
    # Tracks when the thought ACTUALLY happened (e.g. a project from 2024),
    # separate from created_at which tracks when it entered the Strata database.
    # NULL means "not a legacy import" — created_at is the true date.
    if "original_date" not in existing_cols:
        conn.execute("ALTER TABLE thoughts ADD COLUMN original_date TIMESTAMP DEFAULT NULL")
        print("[db] Added original_date column to thoughts table")

    # --- Performance indexes ---
    # Without these, queries filtering by created_at or type do full table scans.
    # At 10K+ rows, these indexes make list_recent, search_advanced, and
    # generate_report significantly faster.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_created_at ON thoughts(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_type ON thoughts(type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_status_priority ON thoughts(status, priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_thoughts_machine_created ON thoughts(machine, created_at)")

    # --- Audit trail table ---
    # Every mutation (create/update/delete) to a thought gets logged here.
    # WHY: Once thoughts are updated or deleted, the original content is lost forever.
    # This table preserves the full history so Chris can see what changed, when, and by whom.
    # It's also essential for debugging — if an agent corrupts a thought, the audit trail
    # shows exactly what happened and lets us recover the original content.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thought_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thought_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            old_content TEXT,
            new_content TEXT,
            changed_fields TEXT DEFAULT '[]',
            source TEXT DEFAULT 'unknown',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Index on thought_id — fast lookup when viewing history for a specific thought.
    # Without this, "show me all changes to thought #42" would scan every row.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_thought_id ON thought_history(thought_id)")

    # Index on timestamp — fast lookup for "what changed recently?" queries.
    # Used by get_thought_history() when no specific thought_id is given.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_timestamp ON thought_history(timestamp)")

    conn.commit()
    conn.close()
    print(f"[db] Database initialized at {DB_PATH}")


def log_history(thought_id, action, old_content=None, new_content=None, changed_fields=None, source="unknown"):
    """Log a mutation to the thought_history audit trail.

    Every create, update, and delete gets recorded here so we have a complete
    paper trail of what happened to every thought. This is a write-only log —
    history entries are never modified or deleted.

    WHY a separate helper instead of inline SQL in each function?
    Because three different functions (store, update, delete) all need to log,
    and centralizing the logic means one place to fix bugs or add fields later.

    Args:
        thought_id: The ID of the thought being mutated
        action: What happened — 'create', 'update', or 'delete'
        old_content: The content BEFORE the change (None for creates)
        new_content: The content AFTER the change (None for deletes)
        changed_fields: List of field names that were modified (e.g. ['content', 'tags'])
        source: Who made the change — matches the thought's source field
                (e.g. 'claude-code', 'telegram', 'manual', 'codex')
    """
    # Default to empty list if no changed_fields provided.
    # We store this as JSON so it's queryable and human-readable in the DB.
    if changed_fields is None:
        changed_fields = []

    conn = get_db()
    conn.execute(
        """INSERT INTO thought_history (thought_id, action, old_content, new_content, changed_fields, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (thought_id, action, old_content, new_content, json.dumps(changed_fields), source)
    )
    conn.commit()
    conn.close()


def store_thought(
    content,
    embedding,
    thought_type="thought",
    tags=None,
    people=None,
    source="manual",
    machine="unknown",
    trigger="unknown",
    status="none",
    priority=0,
):
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
    status = _normalize_status(status)
    priority = _normalize_priority(priority)
    conn = get_db()

    # Insert the thought itself
    cursor = conn.execute(
        "INSERT INTO thoughts (content, type, tags, people, source, machine, trigger, status, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (content, thought_type, json.dumps(tags), json.dumps(people), source, machine, trigger, status, priority)
    )
    thought_id = cursor.lastrowid

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
    conn.close()

    # Log the creation to the audit trail AFTER the main transaction commits.
    # WHY after and not inside the same transaction? Because log_history() opens
    # its own connection, and we don't want a logging failure to roll back
    # the actual thought creation. The thought is the important data — the
    # audit log is supplementary. If logging fails, we still have the thought.
    log_history(thought_id, action="create", new_content=content, source=source)

    return thought_id


def store_legacy_thought(
    content,
    embedding,
    thought_type="thought",
    tags=None,
    people=None,
    source="pre-strata",
    original_date=None,
    machine="unknown",
    trigger="manual",
    status="none",
    priority=0,
):
    """Store a pre-Strata historical thought with a NEGATIVE ID.

    This is the import path for old projects, notes, and files that existed
    before Strata was built. Negative IDs create a clean namespace:
      - Positive IDs (1, 2, 3...) = live Strata thoughts (real-time captures)
      - Negative IDs (-1, -2, -3...) = historical imports (pre-Strata era)

    The next negative ID is calculated as MIN(existing IDs) - 1, so legacy
    thoughts count backward from -1 forever without touching the positive
    auto-increment sequence.

    NOTE: Legacy thoughts are NOT indexed in FTS5 because SQLite's FTS5
    virtual tables don't support negative rowids. They ARE fully searchable
    via semantic/vector search (the primary discovery path). They just won't
    appear in hybrid_search() BM25 keyword results — a minor tradeoff.

    Args:
        content: The thought/memory text to store
        embedding: 768-dim float list from the embedding model
        thought_type: Category (thought, decision, session, project, etc.)
        tags: List of string tags — "legacy" is auto-added
        people: List of people names mentioned
        source: Defaults to "pre-strata" to identify historical imports
        original_date: When this ACTUALLY happened (e.g. "2024-06-15"),
                       separate from created_at which is always "right now"
        machine: Which device this originally came from
        trigger: How capture was initiated (usually "manual" for imports)

    Returns:
        The new thought's negative ID (e.g. -1, -42, -200)
    """
    tags = tags or []
    people = people or []
    status = _normalize_status(status)
    priority = _normalize_priority(priority)
    conn = get_db()

    # Calculate the next negative ID: fill gaps first, then go lower.
    # Scans from -1 downward to find the first unused negative ID.
    # This ensures sequential numbering with no skipped IDs.
    row = conn.execute("SELECT id FROM thoughts WHERE id < 0 ORDER BY id DESC").fetchall()
    existing_neg = {r["id"] for r in row}
    if not existing_neg:
        # No negative IDs exist yet — start at -1
        next_id = -1
    else:
        # Walk from -1 downward, find the first gap
        next_id = -1
        while next_id in existing_neg:
            next_id -= 1

    # Insert the thought with an explicit negative ID.
    # AUTOINCREMENT only tracks the max positive rowid in sqlite_sequence,
    # so this won't affect future positive ID assignment at all.
    conn.execute(
        """INSERT INTO thoughts (id, content, type, tags, people, source, machine, trigger, status, priority, original_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (next_id, content, thought_type, json.dumps(tags), json.dumps(people),
         source, machine, trigger, status, priority, original_date)
    )

    # Store the vector embedding — same as regular thoughts
    conn.execute(
        "INSERT INTO thought_embeddings (thought_id, embedding) VALUES (?, ?)",
        (next_id, _serialize_embedding(embedding))
    )

    # SKIP FTS5 insertion — FTS5 doesn't support negative rowids.
    # Legacy thoughts are still fully discoverable via semantic search
    # (cosine similarity on embeddings), just not via BM25 keyword search.

    conn.commit()
    conn.close()

    # Audit trail — same pattern as store_thought()
    log_history(next_id, action="create", new_content=content, source=source)

    return next_id


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

    # Load all existing embeddings (same pattern as search_similar)
    rows = conn.execute("""
        SELECT e.thought_id, e.embedding, t.content
        FROM thought_embeddings e
        JOIN thoughts t ON t.id = e.thought_id
    """).fetchall()

    conn.close()

    if not rows:
        return []

    # Compare against all stored embeddings
    query_vec = np.array(embedding, dtype=np.float32)
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)

    duplicates = []
    for row in rows:
        stored_vec = _deserialize_embedding(row["embedding"])
        # BUG-06 FIX: Skip embeddings with wrong dimensions (from model switch or corruption)
        if len(stored_vec) != EMBEDDING_DIM:
            continue
        stored_norm = stored_vec / (np.linalg.norm(stored_vec) + 1e-10)
        similarity = float(np.dot(query_norm, stored_norm))

        if similarity >= threshold:
            # Return a preview — first 150 chars of the existing content
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

    This is the core semantic search — finds thoughts by MEANING, not keywords.
    "job change" will find "thinking about switching careers" because the
    embeddings land near each other in meaning-space.

    How it works:
    1. Load all embeddings from the DB (they're small — 10k entries = ~30MB)
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

    # Get all embeddings
    rows = conn.execute("""
        SELECT e.thought_id, e.embedding, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.status, t.priority
        FROM thought_embeddings e
        JOIN thoughts t ON t.id = e.thought_id
    """).fetchall()

    if not rows:
        conn.close()
        return []

    # Build numpy matrix of all stored embeddings for fast batch comparison
    query_vec = np.array(query_embedding, dtype=np.float32)
    thought_ids = []
    embeddings = []
    metadata = []

    for row in rows:
        emb = _deserialize_embedding(row["embedding"])
        # BUG-06 FIX: Skip embeddings with wrong dimensions (from model switch or corruption)
        if len(emb) != EMBEDDING_DIM:
            continue
        thought_ids.append(row["thought_id"])
        embeddings.append(emb)
        metadata.append({
            "id": row["thought_id"],
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    if not embeddings:
        conn.close()
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
        # Skip results below the minimum threshold — filters out noise
        if sim < threshold:
            continue
        result = metadata[idx].copy()
        result["similarity"] = round(sim, 4)
        results.append(result)
        if len(results) >= limit:
            break

    conn.close()
    return results


def find_related_by_id(thought_id, limit=5):
    """Find thoughts most similar to an existing thought by its stored embedding.

    Instead of searching by text query, this says "find more like THIS one"
    using the thought's already-computed embedding. Skips embedding generation
    entirely — just a vector lookup + cosine comparison.

    Args:
        thought_id: ID of the thought to find relatives for
        limit: Max results to return (default 5)

    Returns:
        List of similar thoughts (excluding the source thought itself), or None if not found
    """
    conn = get_db()

    # Get the source thought's embedding
    source_row = conn.execute(
        "SELECT embedding FROM thought_embeddings WHERE thought_id = ?", (thought_id,)
    ).fetchone()

    if not source_row:
        conn.close()
        return None

    source_embedding = _deserialize_embedding(source_row["embedding"])

    # Get all other embeddings
    rows = conn.execute("""
        SELECT e.thought_id, e.embedding, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.status, t.priority
        FROM thought_embeddings e
        JOIN thoughts t ON t.id = e.thought_id
        WHERE e.thought_id != ?
    """, (thought_id,)).fetchall()

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
        # BUG-06 FIX: Skip embeddings with wrong dimensions
        if len(emb) != EMBEDDING_DIM:
            continue
        embeddings.append(emb)
        metadata.append({
            "id": row["thought_id"],
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
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

    # --- STEP 1: Get BM25 keyword scores from FTS5 ---
    # FTS5 rank() returns negative values (more negative = better match)
    # We need to normalize these to 0-1 range
    fts_scores = {}
    try:
        # MEDIUM-01 FIX: Wrap entire query in double quotes to treat as literal phrase.
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
        # FTS5 query might fail on special characters — fall back to vector-only
        pass

    # --- STEP 2: Get cosine similarity scores ---
    rows = conn.execute("""
        SELECT e.thought_id, e.embedding, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.status, t.priority
        FROM thought_embeddings e
        JOIN thoughts t ON t.id = e.thought_id
    """).fetchall()

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
        # BUG-06 FIX: Skip embeddings with wrong dimensions
        if len(emb) != EMBEDDING_DIM:
            continue
        embeddings.append((tid, emb))
        metadata[tid] = {
            "id": tid,
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        }

    # Compute cosine similarities
    cosine_scores = {}
    for tid, emb in embeddings:
        emb_norm = emb / (np.linalg.norm(emb) + 1e-10)
        cosine_scores[tid] = float(np.dot(query_norm, emb_norm))

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
        # Tell the caller what matched — useful for debugging search quality
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

    # Start building the query — use JOINs only when filtering by tag or person
    # since those require json_each() on the JSON arrays
    needs_tag_join = "tag" in filters and filters["tag"]
    needs_person_join = "person" in filters and filters["person"]

    query = "SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.status, t.priority, t.access_count, t.last_accessed FROM thoughts t"
    conditions = []
    params = []

    if needs_tag_join:
        query += ", json_each(t.tags) jt"
        conditions.append("LOWER(jt.value) = LOWER(?)")
        params.append(filters["tag"])

    if needs_person_join:
        query += ", json_each(t.people) jp"
        conditions.append("LOWER(jp.value) LIKE LOWER(?)")
        params.append(f"%{filters['person']}%")

    if "type" in filters and filters["type"]:
        conditions.append("t.type = ?")
        params.append(filters["type"])

    if "source" in filters and filters["source"]:
        conditions.append("t.source = ?")
        params.append(filters["source"])

    if "machine" in filters and filters["machine"]:
        conditions.append("t.machine = ?")
        params.append(filters["machine"])

    if "status" in filters and filters["status"]:
        conditions.append("t.status = ?")
        params.append(_normalize_status(filters["status"]))

    if "priority_min" in filters and filters["priority_min"] is not None:
        conditions.append("t.priority >= ?")
        params.append(_normalize_priority(filters["priority_min"]))

    if "priority_max" in filters and filters["priority_max"] is not None:
        conditions.append("t.priority <= ?")
        params.append(_normalize_priority(filters["priority_max"]))

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
    conn.close()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
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
    This is purely analytical — nothing gets archived or deleted.

    Args:
        days: Number of days for the current period (default 7)

    Returns:
        Dict with report data
    """
    conn = get_db()
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
        SELECT j.value as tag, COUNT(*) as cnt
        FROM thoughts t, json_each(t.tags) j
        WHERE t.created_at >= ?
        GROUP BY LOWER(j.value) ORDER BY cnt DESC LIMIT 20
    """, (current_start,)).fetchall()
    for row in rows:
        current_tags[row["tag"]] = row["cnt"]

    # Tag trends: previous period
    previous_tags = {}
    rows = conn.execute("""
        SELECT j.value as tag, COUNT(*) as cnt
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

    Called by search tools AFTER returning results — tracks which thoughts
    are actively being retrieved. Over time this reveals which memories are
    "hot" (frequently accessed) vs "cold" (never looked at since creation).

    BUG-04 FIX: Uses single batch UPDATE with WHERE id IN (...) instead
    of N individual updates. Fewer write locks, faster execution.

    Args:
        thought_ids: List of thought IDs that were just returned to a caller
    """
    if not thought_ids:
        return

    conn = get_db()
    now = datetime.now().isoformat()

    # Batch update — single query for all IDs (much more efficient than N separate updates)
    placeholders = ",".join("?" * len(thought_ids))
    conn.execute(
        f"UPDATE thoughts SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({placeholders})",
        [now] + list(thought_ids)
    )

    conn.commit()
    conn.close()


def list_recent(limit=20, hours=168, offset=0):
    """Get the most recent thoughts within a time window.
    Default: last 7 days (168 hours). Good for "what was I thinking about this week?"
    """
    conn = get_db()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    rows = conn.execute("""
        SELECT id, content, type, tags, people, source, created_at, machine, trigger, status, priority
        FROM thoughts
        WHERE created_at >= ?
          AND (status IS NULL OR status != 'archived')
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (cutoff, limit, offset)).fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    conn.close()
    return results


def search_by_tag(tag, limit=20):
    """Find all thoughts with a specific tag.
    Tags are stored as JSON arrays, so we use JSON contains check."""
    conn = get_db()

    rows = conn.execute("""
        SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.status, t.priority
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
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    conn.close()
    return results


def search_by_person(person, limit=20):
    """Find all thoughts that mention a specific person.
    Case-insensitive partial match — "chris" matches "Chris Mitchell"."""
    conn = get_db()

    rows = conn.execute("""
        SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.status, t.priority
        FROM thoughts t, json_each(t.people) j
        WHERE LOWER(j.value) LIKE LOWER(?)
        ORDER BY t.created_at DESC
        LIMIT ?
    """, (f"%{person}%", limit)).fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    conn.close()
    return results


def update_thought(
    thought_id,
    content=None,
    thought_type=None,
    tags=None,
    people=None,
    status=None,
    priority=None,
    new_embedding=None,
):
    """Update an existing thought's fields.

    Only the fields you pass get updated — everything else stays the same.
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

    # Check the thought exists AND capture its current state for the audit trail.
    # WHY fetch all fields instead of just id? Because we need the old values to
    # record what changed. Without this snapshot, the audit trail would only say
    # "something changed" but not what the original values were.
    existing = conn.execute(
        "SELECT id, content, type, tags, people, source, status, priority FROM thoughts WHERE id = ?",
        (thought_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return False

    # Snapshot the old content for the audit trail
    old_content = existing["content"]
    old_source = existing["source"]

    # Build the UPDATE query dynamically — only set fields that were provided.
    # Also track which fields actually changed for the audit trail.
    # WHY track changed_fields separately? Because knowing WHICH fields changed
    # is more useful than just knowing "an update happened". An agent debugging
    # a bad tag can filter the audit log for tag-only changes.
    updates = []
    params = []
    changed_fields = []

    if content is not None:
        updates.append("content = ?")
        params.append(content)
        changed_fields.append("content")
    if thought_type is not None:
        updates.append("type = ?")
        params.append(thought_type)
        changed_fields.append("type")
    if tags is not None:
        updates.append("tags = ?")
        params.append(json.dumps(tags))
        changed_fields.append("tags")
    if people is not None:
        updates.append("people = ?")
        params.append(json.dumps(people))
        changed_fields.append("people")
    if status is not None:
        updates.append("status = ?")
        params.append(_normalize_status(status))
        changed_fields.append("status")
    if priority is not None:
        updates.append("priority = ?")
        params.append(_normalize_priority(priority))
        changed_fields.append("priority")

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
        # Track embedding change too — useful to know when vectors were recomputed
        changed_fields.append("embedding")

    # Rebuild the FTS index entry for this thought.
    # SKIP for negative IDs — legacy/pre-Strata thoughts were never in FTS5
    # because FTS5 doesn't support negative rowids. They use semantic search only.
    if thought_id >= 0:
        row = conn.execute(
            "SELECT content, tags, people FROM thoughts WHERE id = ?", (thought_id,)
        ).fetchone()
        if row:
            current_tags = json.loads(row["tags"])
            current_people = json.loads(row["people"])
            # FTS5 contentless tables: use proper contentless delete syntax, then insert new
            conn.execute(
                "INSERT INTO thoughts_fts(thoughts_fts, rowid, content, tags, people) VALUES('delete', ?, ?, ?, ?)",
                (thought_id, row["content"], " ".join(current_tags), " ".join(current_people))
            )
            conn.execute(
                "INSERT INTO thoughts_fts (rowid, content, tags, people) VALUES (?, ?, ?, ?)",
                (thought_id, row["content"], " ".join(current_tags), " ".join(current_people))
            )

    conn.commit()
    conn.close()

    # Log the update to the audit trail — capture both old and new content.
    # The new_content is whatever the content is NOW (either the updated value
    # or the old value if content wasn't changed in this update).
    # We use the old source as the fallback because the source field on the
    # thought itself tells us who originally created it — the audit source
    # tells us who made THIS particular change.
    final_content = content if content is not None else old_content
    log_history(
        thought_id,
        action="update",
        old_content=old_content,
        new_content=final_content,
        changed_fields=changed_fields,
        source=old_source,
    )

    return True


def delete_thought(thought_id):
    """Permanently remove a thought from the database.

    Deletes from all three tables: thoughts, thought_embeddings, and thoughts_fts.
    This is irreversible — BUT the audit trail preserves the old content so it
    can be reviewed or recovered manually if needed.

    Args:
        thought_id: The ID of the thought to delete

    Returns:
        True if the thought was found and deleted, False if not found
    """
    conn = get_db()

    # Fetch the full thought BEFORE deleting — we need the content and source
    # for the audit trail. Once it's deleted from the thoughts table, it's gone.
    # The audit trail becomes the ONLY place the original content survives.
    existing = conn.execute(
        "SELECT id, content, tags, people, source FROM thoughts WHERE id = ?", (thought_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return False

    # Capture old values before they're destroyed
    old_content = existing["content"]
    old_source = existing["source"]

    # Delete from all tables — order doesn't matter since we commit at the end.
    # Skip FTS for negative IDs — legacy thoughts were never in the FTS index
    # because FTS5 doesn't support negative rowids.
    conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (thought_id,))
    if thought_id >= 0:
        # FTS5 contentless delete — must pass the original content to remove the entry
        old_tags = json.loads(existing["tags"]) if existing["tags"] else []
        old_people = json.loads(existing["people"]) if existing["people"] else []
        conn.execute(
            "INSERT INTO thoughts_fts(thoughts_fts, rowid, content, tags, people) VALUES('delete', ?, ?, ?, ?)",
            (thought_id, old_content, " ".join(old_tags), " ".join(old_people))
        )
    conn.execute("DELETE FROM thoughts WHERE id = ?", (thought_id,))

    conn.commit()
    conn.close()

    # Log the deletion AFTER committing — preserves the old content in the audit trail.
    # This is the safety net: even after a permanent delete, the old content lives
    # in thought_history so Chris can see what was deleted and potentially recover it.
    log_history(
        thought_id,
        action="delete",
        old_content=old_content,
        source=old_source,
    )

    return True


def get_thought_by_id(thought_id):
    """Fetch a single thought by its ID. Returns dict or None if not found.

    Useful for confirming a thought exists before updating/deleting,
    or for showing the user what they're about to modify.
    Includes access tracking fields (last_accessed, access_count).
    """
    conn = get_db()
    row = conn.execute(
        "SELECT id, content, type, tags, people, source, created_at, last_accessed, access_count, machine, trigger, status, priority FROM thoughts WHERE id = ?",
        (thought_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "id": row["id"],
        "content": row["content"],
        "type": row["type"],
        "tags": json.loads(row["tags"]),
        "people": json.loads(row["people"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "last_accessed": row["last_accessed"],
        "access_count": row["access_count"] or 0,
        "machine": row["machine"] or "unknown",
        "trigger": row["trigger"] or "unknown",
        "status": row["status"] or "none",
        "priority": row["priority"] or 0,
    }


def get_stats():
    """Get database statistics — total thoughts, type breakdown, top tags, top people, db size."""
    conn = get_db()

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

    # Count by machine — shows which devices are contributing thoughts
    machine_rows = conn.execute(
        "SELECT COALESCE(machine, 'unknown') as machine, COUNT(*) as cnt FROM thoughts GROUP BY machine ORDER BY cnt DESC"
    ).fetchall()
    machines = {row["machine"]: row["cnt"] for row in machine_rows}

    # Count by trigger — shows auto vs requested vs manual breakdown
    trigger_rows = conn.execute(
        "SELECT COALESCE(trigger, 'unknown') as trigger, COUNT(*) as cnt FROM thoughts GROUP BY trigger ORDER BY cnt DESC"
    ).fetchall()
    triggers = {row["trigger"]: row["cnt"] for row in trigger_rows}

    status_rows = conn.execute(
        "SELECT COALESCE(status, 'none') as status, COUNT(*) as cnt FROM thoughts GROUP BY status ORDER BY cnt DESC"
    ).fetchall()
    statuses = {row["status"]: row["cnt"] for row in status_rows}

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

    conn.close()

    return {
        "total_thoughts": total,
        "by_type": types,
        "by_source": sources,
        "by_machine": machines,
        "by_trigger": triggers,
        "by_status": statuses,
        "top_tags": top_tags,
        "top_people": top_people,
        "db_size_mb": db_size_mb,
    }


def get_agent_profile(agent_name):
    """Fetch one agent profile by name."""
    conn = get_db()
    row = conn.execute(
        "SELECT agent_name, startup_mode, instructions, metadata, updated_at FROM agent_profiles WHERE agent_name = ?",
        (agent_name,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "agent_name": row["agent_name"],
        "startup_mode": row["startup_mode"] or "standard",
        "instructions": row["instructions"] or "",
        "metadata": json.loads(row["metadata"] or "{}"),
        "updated_at": row["updated_at"],
    }


def upsert_agent_profile(agent_name, startup_mode="standard", instructions="", metadata=None):
    """Create or update an agent profile."""
    metadata = metadata or {}
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        """
        INSERT INTO agent_profiles (agent_name, startup_mode, instructions, metadata, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(agent_name) DO UPDATE SET
            startup_mode = excluded.startup_mode,
            instructions = excluded.instructions,
            metadata = excluded.metadata,
            updated_at = excluded.updated_at
        """,
        (agent_name, startup_mode, instructions, json.dumps(metadata), now),
    )
    conn.commit()
    conn.close()
    return get_agent_profile(agent_name)


def generate_change_digest(days=1, limit=20):
    """Generate a compact change digest for a recent window."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    rows = conn.execute(
        """
        SELECT id, content, type, tags, people, source, created_at, machine, trigger, status, priority
        FROM thoughts
        WHERE created_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()

    type_rows = conn.execute(
        """
        SELECT type, COUNT(*) as cnt
        FROM thoughts
        WHERE created_at >= ?
        GROUP BY type
        ORDER BY cnt DESC
        """,
        (cutoff,),
    ).fetchall()

    status_rows = conn.execute(
        """
        SELECT COALESCE(status, 'none') as status, COUNT(*) as cnt
        FROM thoughts
        WHERE created_at >= ?
        GROUP BY status
        ORDER BY cnt DESC
        """,
        (cutoff,),
    ).fetchall()

    conn.close()

    items = []
    for row in rows:
        items.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    return {
        "window_days": days,
        "generated_at": datetime.now().isoformat(),
        "count": len(items),
        "by_type": {r["type"]: r["cnt"] for r in type_rows},
        "by_status": {r["status"]: r["cnt"] for r in status_rows},
        "items": items,
    }


def get_startup_bundle(
    recent_limit=10,
    project_limit=5,
    blocker_limit=5,
    digest_days=1,
    agent_name="codex",
):
    """Return a one-call startup payload for agent boot."""
    conn = get_db()

    recent_rows = conn.execute(
        """
        SELECT id, content, type, tags, people, source, created_at, machine, trigger, status, priority
        FROM thoughts
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (recent_limit,),
    ).fetchall()

    project_rows = conn.execute(
        """
        SELECT id, content, type, tags, people, source, created_at, machine, trigger, status, priority
        FROM thoughts
        WHERE type = 'project'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (project_limit,),
    ).fetchall()

    blocker_rows = conn.execute(
        """
        SELECT id, content, type, tags, people, source, created_at, machine, trigger, status, priority
        FROM thoughts
        WHERE status IN ('open', 'in_progress')
        ORDER BY priority DESC, created_at DESC
        LIMIT ?
        """,
        (blocker_limit,),
    ).fetchall()

    # Fallback for existing data that has no status yet.
    if not blocker_rows:
        blocker_rows = conn.execute(
            """
            SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source, t.created_at, t.machine, t.trigger, t.status, t.priority
            FROM thoughts t, json_each(t.tags) j
            WHERE LOWER(j.value) IN ('blocker', 'blocked', 'todo')
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            (blocker_limit,),
        ).fetchall()

    conn.close()

    def _rows_to_items(rows):
        items = []
        for row in rows:
            items.append({
                "id": row["id"],
                "content": row["content"],
                "type": row["type"],
                "tags": json.loads(row["tags"]),
                "people": json.loads(row["people"]),
                "source": row["source"],
                "created_at": row["created_at"],
                "machine": row["machine"] or "unknown",
                "trigger": row["trigger"] or "unknown",
                "status": row["status"] or "none",
                "priority": row["priority"] or 0,
            })
        return items

    return {
        "generated_at": datetime.now().isoformat(),
        "agent_profile": get_agent_profile(agent_name),
        "recent_thoughts": _rows_to_items(recent_rows),
        "active_projects": _rows_to_items(project_rows),
        "open_blockers": _rows_to_items(blocker_rows),
        "change_digest": generate_change_digest(days=digest_days, limit=recent_limit),
    }


def get_thought_history(thought_id=None, limit=50):
    """Get audit trail entries from the thought_history table.

    Two modes:
    1. If thought_id is given: get the full mutation history for that specific thought.
       This answers "what happened to thought #42 over time?" — every create, update,
       and delete event, with old/new content and which fields changed.
    2. If thought_id is None: get the most recent history entries across ALL thoughts.
       This answers "what changed recently?" — useful for debugging or reviewing
       what agents have been doing to the database.

    Results are always sorted newest-first so the most recent changes appear at the top.

    Args:
        thought_id: Optional — filter to a specific thought's history
        limit: Max entries to return (default 50, which is generous enough
               to see a full thought's lifecycle without overwhelming the response)

    Returns:
        List of history entry dicts with all fields from thought_history
    """
    conn = get_db()

    if thought_id is not None:
        # Mode 1: History for a specific thought — uses idx_history_thought_id index
        rows = conn.execute(
            """SELECT id, thought_id, action, old_content, new_content, changed_fields, source, timestamp
               FROM thought_history
               WHERE thought_id = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (thought_id, limit)
        ).fetchall()
    else:
        # Mode 2: Recent history across all thoughts — uses idx_history_timestamp index
        rows = conn.execute(
            """SELECT id, thought_id, action, old_content, new_content, changed_fields, source, timestamp
               FROM thought_history
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,)
        ).fetchall()

    conn.close()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "thought_id": row["thought_id"],
            "action": row["action"],
            "old_content": row["old_content"],
            "new_content": row["new_content"],
            # Parse changed_fields back from JSON string to a Python list.
            # Stored as JSON in the DB for human readability + queryability.
            "changed_fields": json.loads(row["changed_fields"]) if row["changed_fields"] else [],
            "source": row["source"],
            "timestamp": row["timestamp"],
        })

    return results


def search_by_timerange(date_from=None, date_to=None, thought_type=None, source=None, machine=None, limit=20):
    """Search thoughts within a time range with optional filters.

    This is the go-to function for questions like:
    - "What did I capture last Tuesday?" (date_from + date_to)
    - "What has Telegram been saving this week?" (date_from + source='telegram')
    - "Show me all decisions from Helios in January" (date range + type + machine)

    WHY a dedicated function instead of using search_advanced?
    search_advanced is a general-purpose filter builder. This function is specifically
    optimized for temporal queries with a simpler interface — you don't need to
    construct a filters dict, just pass the date range and optional filters directly.
    It's the temporal complement to semantic_search (meaning) and search_by_tag (category).

    The date range uses >= for date_from and <= for date_to (inclusive on both ends).
    WHY inclusive? Because when Chris says "show me March 10" he means the whole day,
    not "up to but not including March 10". The date_to value gets " 23:59:59" appended
    to capture the entire end day.

    Args:
        date_from: ISO date string (YYYY-MM-DD) — start of range (inclusive).
                   If None, no lower bound (goes back to the beginning of time).
        date_to: ISO date string (YYYY-MM-DD) — end of range (inclusive).
                 If None, no upper bound (includes everything up to now).
        thought_type: Optional filter by type (e.g. 'decision', 'project', 'session')
        source: Optional filter by source (e.g. 'claude-code', 'telegram', 'codex')
        machine: Optional filter by machine (e.g. 'surface', 'helios', 'pi-nas')
        limit: Max results (default 20)

    Returns:
        List of thought dicts within the time range, newest first.
        Same dict format as list_recent / search_by_tag for consistency.
    """
    conn = get_db()

    # Build the query dynamically — same pattern as search_advanced but with
    # a focused interface for temporal queries. Only add WHERE clauses for
    # filters that were actually provided.
    query = """SELECT id, content, type, tags, people, source, created_at, machine, trigger, status, priority
               FROM thoughts"""
    conditions = []
    params = []

    if date_from is not None:
        # >= includes thoughts created at any time on the start date
        conditions.append("created_at >= ?")
        params.append(date_from)

    if date_to is not None:
        # Append time to include the ENTIRE end day — "2026-03-10" becomes
        # "2026-03-10 23:59:59" so thoughts created at 11pm on March 10 are included.
        # Without this, a bare date comparison would effectively mean "before midnight
        # on March 10" which excludes most of that day's thoughts.
        conditions.append("created_at <= ?")
        params.append(date_to + " 23:59:59")

    if thought_type is not None:
        conditions.append("type = ?")
        params.append(thought_type)

    if source is not None:
        conditions.append("source = ?")
        params.append(source)

    if machine is not None:
        conditions.append("machine = ?")
        params.append(machine)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    # Always order newest-first — temporal queries almost always want recent stuff on top.
    # The idx_thoughts_created_at index makes this ORDER BY essentially free.
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Format results using the same dict structure as every other query function.
    # WHY not use a shared _format_thought helper? Because the codebase doesn't have one
    # (each function does its own formatting), and introducing one now would mean
    # refactoring every existing function — a scope-creep risk for a targeted feature add.
    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": json.loads(row["tags"]),
            "people": json.loads(row["people"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    return results
