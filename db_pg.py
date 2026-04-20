# STRATA — Database Layer (PostgreSQL + pgvector)
# Migrated from SQLite to PostgreSQL for concurrent multi-agent access.
#
# WHY POSTGRESQL?
# SQLite only allows one writer at a time — when multiple agents (Surface, Helios,
# Telegram, Codex, Vox) hit Strata simultaneously, they block each other.
# PostgreSQL handles concurrent reads AND writes natively. No more write queue.
#
# WHY PGVECTOR?
# The old SQLite approach loaded ALL embeddings into Python memory and computed
# cosine similarity with numpy. That works for <10k entries but doesn't scale,
# and it serializes all search requests through a single Python process.
# pgvector does similarity search IN the database using HNSW indexes — concurrent,
# indexed, and the database handles the math.
#
# WHY TSVECTOR?
# Replaces SQLite FTS5 for full-text keyword search. tsvector supports negative IDs
# (FTS5 didn't), weighted search (content > tags > people), and concurrent access.

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
import psycopg2.pool
from pgvector.psycopg2 import register_vector

from config import (
    EMBEDDING_DIM,
    DEDUP_THRESHOLD,
)

# --- PostgreSQL connection config ---
# Override via environment variables. Defaults match the Pi NAS setup.
PG_HOST = os.environ.get("STRATA_PG_HOST", "localhost")
PG_PORT = os.environ.get("STRATA_PG_PORT", "5432")
PG_DB = os.environ.get("STRATA_PG_DB", "strata_db")
PG_USER = os.environ.get("STRATA_PG_USER", "strata")
PG_PASSWORD = os.environ.get("STRATA_PG_PASSWORD", "strata_brain_2026")

PG_DSN = f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} user={PG_USER} password={PG_PASSWORD}"

# Connection pool — initialized in init_db().
# ThreadedConnectionPool allows multiple threads to check out connections simultaneously.
# min=2 keeps connections warm, max=10 handles burst traffic from multiple agents.
_pool = None

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


@contextmanager
def _get_conn():
    """Get a PostgreSQL connection from the pool.

    Usage:
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT ...")

    Connection is automatically returned to the pool when the block exits.
    On exception, the transaction is rolled back. On success, the caller
    must call conn.commit() for write operations.
    """
    conn = _pool.getconn()
    try:
        # Register pgvector type adapter so we can pass/receive vector columns.
        # Safe to call multiple times on the same connection (idempotent).
        register_vector(conn)
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        # Reset connection state so next checkout starts clean.
        # This rolls back any uncommitted transaction.
        try:
            conn.reset()
        except Exception:
            pass
        _pool.putconn(conn)




class _PoolConnWrapper:
    """Wraps a psycopg2 connection to add .execute() convenience method
    and return to pool on .close(). Used by server.py dashboard code."""
    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
        self._cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    def execute(self, sql, params=None):
        if params:
            self._cur.execute(sql, params)
        else:
            self._cur.execute(sql)
        return self._cur
    def cursor(self, cursor_factory=None):
        if cursor_factory:
            return self._conn.cursor(cursor_factory=cursor_factory)
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass
        try:
            self._conn.reset()
        except Exception:
            pass
        self._pool.putconn(self._conn)

def get_db():
    """Compatibility wrapper — returns a wrapped PostgreSQL connection from the pool.
    Supports conn.execute() for backward compatibility with server.py dashboard code.
    Caller MUST call conn.close() when done (returns to pool)."""
    conn = _pool.getconn()
    register_vector(conn)
    return _PoolConnWrapper(conn, _pool)

def init_db():
    """Initialize the PostgreSQL connection pool.

    Called once at server startup. The actual schema (tables, indexes, triggers)
    is created by strata_pg_schema.sql — this function just sets up the pool
    and verifies the connection works.
    """
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=PG_DSN,
    )

    # Verify connection and pgvector extension
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            if row:
                print(f"[db] PostgreSQL connected — pgvector {row[0]}")
            else:
                print("[db] WARNING: pgvector extension not found!")
            cur.execute("SELECT COUNT(*) FROM thoughts")
            count = cur.fetchone()[0]
            print(f"[db] Database ready — {count} thoughts loaded")


def log_history(thought_id, action, old_content=None, new_content=None, changed_fields=None, source="unknown"):
    """Log a mutation to the thought_history audit trail.

    Every create, update, and delete gets recorded here so we have a complete
    paper trail of what happened to every thought. This is a write-only log —
    history entries are never modified or deleted.

    WHY a separate helper instead of inline SQL in each function?
    Because three different functions (store, update, delete) all need to log,
    and centralizing the logic means one place to fix bugs or add fields later.
    """
    if changed_fields is None:
        changed_fields = []

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO thought_history (thought_id, action, old_content, new_content, changed_fields, source)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (thought_id, action, old_content, new_content,
                 psycopg2.extras.Json(changed_fields), source)
            )
        conn.commit()


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

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Insert the thought — RETURNING id gives us the auto-generated ID
            # without needing a separate query (PostgreSQL's SERIAL + RETURNING is
            # cleaner than SQLite's lastrowid)
            cur.execute(
                """INSERT INTO thoughts (content, type, tags, people, source, machine, trigger, status, priority)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (content, thought_type, psycopg2.extras.Json(tags),
                 psycopg2.extras.Json(people), source, machine, trigger, status, priority)
            )
            thought_id = cur.fetchone()[0]

            # Insert the vector embedding — pgvector handles the vector type natively.
            # No more struct.pack/unpack or binary blobs. Just pass the list of floats.
            cur.execute(
                "INSERT INTO thought_embeddings (thought_id, embedding) VALUES (%s, %s)",
                (thought_id, embedding)
            )

            # NOTE: Full-text search (search_vector) is auto-updated by the
            # PostgreSQL trigger on INSERT — no manual FTS index management needed.
            # This is a huge win over SQLite FTS5 which required explicit INSERT
            # into a separate virtual table.

        conn.commit()

    # Log the creation to the audit trail AFTER the main transaction commits.
    # WHY after and not inside the same transaction? Because log_history() opens
    # its own connection, and we don't want a logging failure to roll back
    # the actual thought creation.
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

    Negative IDs create a clean namespace:
      - Positive IDs (1, 2, 3...) = live Strata thoughts (real-time captures)
      - Negative IDs (-1, -2, -3...) = historical imports (pre-Strata era)

    Unlike SQLite FTS5, PostgreSQL tsvector handles negative IDs just fine,
    so legacy thoughts ARE fully searchable via both semantic AND keyword search.

    Returns:
        The new thought's negative ID (e.g. -1, -42, -200)
    """
    tags = tags or []
    people = people or []
    status = _normalize_status(status)
    priority = _normalize_priority(priority)

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Calculate the next negative ID: MIN(existing negatives) - 1.
            # If no negative IDs exist yet, start at -1.
            cur.execute("SELECT COALESCE(MIN(id), 0) - 1 AS next_id FROM thoughts WHERE id < 0")
            next_id = cur.fetchone()[0]

            # Insert with explicit negative ID — PostgreSQL SERIAL only generates
            # positive IDs, but we can always INSERT with an explicit ID value.
            cur.execute(
                """INSERT INTO thoughts (id, content, type, tags, people, source, machine, trigger, status, priority, original_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (next_id, content, thought_type, psycopg2.extras.Json(tags),
                 psycopg2.extras.Json(people), source, machine, trigger, status,
                 priority, original_date)
            )

            # Store the vector embedding
            cur.execute(
                "INSERT INTO thought_embeddings (thought_id, embedding) VALUES (%s, %s)",
                (next_id, embedding)
            )

        conn.commit()

    log_history(next_id, action="create", new_content=content, source=source)
    return next_id


def find_duplicates(embedding, threshold=None):
    """Check if any existing thoughts are too similar to a new one.

    Uses pgvector's cosine distance operator (<=>). Cosine distance = 1 - cosine_similarity,
    so we filter WHERE distance <= (1 - threshold) to find matches above the similarity threshold.

    Returns:
        List of dicts with id, content preview, and similarity score
        for any existing thoughts above the threshold. Empty list = no dupes.
    """
    if threshold is None:
        threshold = DEDUP_THRESHOLD

    # Convert similarity threshold to maximum cosine distance
    max_distance = 1.0 - threshold

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # pgvector does the heavy lifting here — cosine distance computed in the DB,
            # filtered and sorted by the HNSW index. No Python math needed.
            cur.execute("""
                SELECT t.id AS thought_id, t.content,
                       1 - (e.embedding <=> %s::vector) AS similarity
                FROM thought_embeddings e
                JOIN thoughts t ON t.id = e.thought_id
                WHERE (e.embedding <=> %s::vector) <= %s
                ORDER BY e.embedding <=> %s::vector
            """, (embedding, embedding, max_distance, embedding))

            rows = cur.fetchall()

    duplicates = []
    for row in rows:
        content = row["content"]
        preview = content[:150] + "..." if len(content) > 150 else content
        duplicates.append({
            "id": row["thought_id"],
            "preview": preview,
            "similarity": round(float(row["similarity"]), 4),
        })

    return duplicates


def search_similar(query_embedding, limit=10, threshold=0.0):
    """Find thoughts most similar to the query by cosine similarity.

    This is the core semantic search — finds thoughts by MEANING, not keywords.
    pgvector computes cosine distance in the database using the HNSW index.
    No more loading all embeddings into Python memory.

    Args:
        query_embedding: 768-dim float list
        limit: Max results to return
        threshold: Minimum cosine similarity score (0.0-1.0)

    Returns list of dicts with id, content, type, tags, people, source, created_at, similarity
    """
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if threshold > 0:
                max_distance = 1.0 - threshold
                cur.execute("""
                    SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                           t.created_at, t.machine, t.trigger, t.status, t.priority,
                           1 - (e.embedding <=> %s::vector) AS similarity
                    FROM thought_embeddings e
                    JOIN thoughts t ON t.id = e.thought_id
                    WHERE (e.embedding <=> %s::vector) <= %s
                    ORDER BY e.embedding <=> %s::vector
                    LIMIT %s
                """, (query_embedding, query_embedding, max_distance, query_embedding, limit))
            else:
                cur.execute("""
                    SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                           t.created_at, t.machine, t.trigger, t.status, t.priority,
                           1 - (e.embedding <=> %s::vector) AS similarity
                    FROM thought_embeddings e
                    JOIN thoughts t ON t.id = e.thought_id
                    ORDER BY e.embedding <=> %s::vector
                    LIMIT %s
                """, (query_embedding, query_embedding, limit))

            rows = cur.fetchall()

    # Record access for retrieved thoughts (fire and forget — separate connection)
    if rows:
        thought_ids = [row["id"] for row in rows]
        try:
            record_access(thought_ids)
        except Exception:
            pass  # Access tracking is supplementary, never block search results

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
            "similarity": round(float(row["similarity"]), 4),
        })

    return results


def find_related_by_id(thought_id, limit=5):
    """Find thoughts most similar to an existing thought by its stored embedding.

    Instead of searching by text query, this says "find more like THIS one"
    using the thought's already-computed embedding.
    """
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # First get the source thought's embedding
            cur.execute(
                "SELECT embedding FROM thought_embeddings WHERE thought_id = %s",
                (thought_id,)
            )
            source_row = cur.fetchone()
            if not source_row:
                return None

            # Use that embedding to find similar thoughts (excluding itself)
            source_embedding = source_row["embedding"]
            cur.execute("""
                SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                       t.created_at, t.machine, t.trigger, t.status, t.priority,
                       1 - (e.embedding <=> %s::vector) AS similarity
                FROM thought_embeddings e
                JOIN thoughts t ON t.id = e.thought_id
                WHERE e.thought_id != %s
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
            """, (source_embedding, thought_id, source_embedding, limit))

            rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
            "similarity": round(float(row["similarity"]), 4),
        })

    return results


def hybrid_search(query_text, query_embedding, limit=10, keyword_weight=0.3, threshold=0.0):
    """Blended search: tsvector keyword (ts_rank) + pgvector cosine similarity.

    Combines the precision of keyword matching with the flexibility of semantic search.
    A query for "CarPi HUD" will boost results that literally contain those words
    AND find semantically related thoughts about the car dashboard.

    Score formula: (keyword_weight * normalized_ts_rank) + ((1 - keyword_weight) * cosine_similarity)
    """
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # --- STEP 1: Get keyword scores from tsvector ---
            fts_scores = {}
            try:
                # plainto_tsquery safely converts text to a tsquery (no injection risk,
                # unlike SQLite FTS5 which had operators that could be exploited)
                cur.execute("""
                    SELECT id, ts_rank_cd(search_vector, plainto_tsquery('english', %s)) AS rank
                    FROM thoughts
                    WHERE search_vector @@ plainto_tsquery('english', %s)
                    ORDER BY rank DESC
                    LIMIT %s
                """, (query_text, query_text, limit * 3))

                fts_rows = cur.fetchall()
                if fts_rows:
                    max_rank = max(r["rank"] for r in fts_rows) or 1
                    for r in fts_rows:
                        fts_scores[r["id"]] = float(r["rank"]) / max_rank
            except Exception:
                pass  # FTS might fail on weird input — fall back to vector-only

            # --- STEP 2: Get cosine similarity scores from pgvector ---
            cur.execute("""
                SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                       t.created_at, t.machine, t.trigger, t.status, t.priority,
                       1 - (e.embedding <=> %s::vector) AS cosine_sim
                FROM thought_embeddings e
                JOIN thoughts t ON t.id = e.thought_id
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
            """, (query_embedding, query_embedding, limit * 3))

            vector_rows = cur.fetchall()

    # --- STEP 3: Blend scores ---
    # Build metadata lookup from vector results
    metadata = {}
    cosine_scores = {}
    for row in vector_rows:
        tid = row["id"]
        cosine_scores[tid] = float(row["cosine_sim"])
        metadata[tid] = row

    # Also include keyword-only matches that weren't in vector top N
    # (they might score high enough blended to make the cut)
    for tid in fts_scores:
        if tid not in metadata:
            # Need to fetch this thought's metadata
            thought = get_thought_by_id(tid)
            if thought:
                metadata[tid] = thought
                cosine_scores[tid] = 0  # No vector score

    all_ids = set(cosine_scores.keys()) | set(fts_scores.keys())
    blended = []
    for tid in all_ids:
        cos = cosine_scores.get(tid, 0)
        kw = fts_scores.get(tid, 0)
        score = (keyword_weight * kw) + ((1 - keyword_weight) * cos)

        if score < threshold:
            continue

        row = metadata[tid]
        result = {
            "id": tid,
            "content": row.get("content", ""),
            "type": row.get("type", "thought"),
            "tags": row.get("tags") or [],
            "people": row.get("people") or [],
            "source": row.get("source", ""),
            "created_at": row["created_at"].isoformat() if hasattr(row.get("created_at", ""), "isoformat") else row.get("created_at", ""),
            "machine": row.get("machine") or "unknown",
            "trigger": row.get("trigger") or "unknown",
            "status": row.get("status") or "none",
            "priority": row.get("priority") or 0,
            "similarity": round(score, 4),
            "match_type": "both" if kw > 0 and cos > 0 else ("keyword" if kw > 0 else "semantic"),
        }
        blended.append(result)

    blended.sort(key=lambda r: r["similarity"], reverse=True)
    return blended[:limit]


def search_advanced(filters, limit=20):
    """Multi-filter search with combined conditions.

    Supports filtering by: type, tag, person, source, machine, date range, status, priority.
    Uses JSONB operators for tag/person filtering (replaces SQLite's json_each).
    """
    conditions = []
    params = []

    # Tag filter — case-insensitive search within JSONB array
    if "tag" in filters and filters["tag"]:
        conditions.append("""
            EXISTS (SELECT 1 FROM jsonb_array_elements_text(t.tags) elem
                    WHERE LOWER(elem) = LOWER(%s))
        """)
        params.append(filters["tag"])

    # Person filter — case-insensitive partial match within JSONB array
    if "person" in filters and filters["person"]:
        conditions.append("""
            EXISTS (SELECT 1 FROM jsonb_array_elements_text(t.people) elem
                    WHERE LOWER(elem) LIKE LOWER(%s))
        """)
        params.append(f"%{filters['person']}%")

    if "type" in filters and filters["type"]:
        conditions.append("t.type = %s")
        params.append(filters["type"])

    if "source" in filters and filters["source"]:
        conditions.append("t.source = %s")
        params.append(filters["source"])

    if "machine" in filters and filters["machine"]:
        conditions.append("t.machine = %s")
        params.append(filters["machine"])

    if "status" in filters and filters["status"]:
        conditions.append("t.status = %s")
        params.append(_normalize_status(filters["status"]))

    if "priority_min" in filters and filters["priority_min"] is not None:
        conditions.append("t.priority >= %s")
        params.append(_normalize_priority(filters["priority_min"]))

    if "priority_max" in filters and filters["priority_max"] is not None:
        conditions.append("t.priority <= %s")
        params.append(_normalize_priority(filters["priority_max"]))

    if "date_from" in filters and filters["date_from"]:
        conditions.append("t.created_at >= %s")
        params.append(filters["date_from"])

    if "date_to" in filters and filters["date_to"]:
        conditions.append("t.created_at <= %s")
        params.append(filters["date_to"])

    query = """SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                      t.created_at, t.machine, t.trigger, t.status, t.priority,
                      t.access_count, t.last_accessed
               FROM thoughts t"""

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY t.created_at DESC LIMIT %s"
    params.append(limit)

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
            "access_count": row["access_count"] or 0,
            "last_accessed": row["last_accessed"].isoformat() if row["last_accessed"] else None,
        })

    return results


def generate_report(days=7):
    """Generate a trend report comparing current period vs previous period."""
    now = datetime.now()
    current_start = (now - timedelta(days=days)).isoformat()
    previous_start = (now - timedelta(days=days * 2)).isoformat()

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Current period count
            cur.execute("SELECT COUNT(*) AS cnt FROM thoughts WHERE created_at >= %s", (current_start,))
            current_count = cur.fetchone()["cnt"]

            # Previous period count
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM thoughts WHERE created_at >= %s AND created_at < %s",
                (previous_start, current_start)
            )
            previous_count = cur.fetchone()["cnt"]

            # Tag trends: current period
            cur.execute("""
                SELECT LOWER(elem::text) AS tag, COUNT(*) AS cnt
                FROM thoughts t, jsonb_array_elements_text(t.tags) AS elem
                WHERE t.created_at >= %s
                GROUP BY LOWER(elem::text) ORDER BY cnt DESC LIMIT 20
            """, (current_start,))
            current_tags = {r["tag"]: r["cnt"] for r in cur.fetchall()}

            # Tag trends: previous period
            cur.execute("""
                SELECT LOWER(elem::text) AS tag, COUNT(*) AS cnt
                FROM thoughts t, jsonb_array_elements_text(t.tags) AS elem
                WHERE t.created_at >= %s AND t.created_at < %s
                GROUP BY LOWER(elem::text) ORDER BY cnt DESC LIMIT 20
            """, (previous_start, current_start))
            previous_tags = {r["tag"]: r["cnt"] for r in cur.fetchall()}

            # Rising and declining tags
            rising, declining = [], []
            all_tags = set(list(current_tags.keys()) + list(previous_tags.keys()))
            for tag in all_tags:
                cur_cnt = current_tags.get(tag, 0)
                prev_cnt = previous_tags.get(tag, 0)
                if prev_cnt == 0 and cur_cnt > 0:
                    rising.append({"tag": tag, "current": cur_cnt, "previous": 0, "change": "new"})
                elif prev_cnt > 0:
                    pct = ((cur_cnt - prev_cnt) / prev_cnt) * 100
                    if pct > 50:
                        rising.append({"tag": tag, "current": cur_cnt, "previous": prev_cnt, "change": f"+{pct:.0f}%"})
                    elif pct < -30:
                        declining.append({"tag": tag, "current": cur_cnt, "previous": prev_cnt, "change": f"{pct:.0f}%"})

            # Activity by machine
            cur.execute("""
                SELECT COALESCE(machine, 'unknown') AS machine, COUNT(*) AS cnt
                FROM thoughts WHERE created_at >= %s
                GROUP BY machine ORDER BY cnt DESC
            """, (current_start,))
            by_machine = {r["machine"]: r["cnt"] for r in cur.fetchall()}

            # Activity by source
            cur.execute("""
                SELECT source, COUNT(*) AS cnt
                FROM thoughts WHERE created_at >= %s
                GROUP BY source ORDER BY cnt DESC
            """, (current_start,))
            by_source = {r["source"]: r["cnt"] for r in cur.fetchall()}

            # Hottest memories
            cur.execute("""
                SELECT id, content, type, access_count, last_accessed
                FROM thoughts WHERE access_count > 0
                ORDER BY access_count DESC LIMIT 10
            """)
            hottest = []
            for r in cur.fetchall():
                preview = r["content"][:100] + "..." if len(r["content"]) > 100 else r["content"]
                hottest.append({
                    "id": r["id"], "preview": preview, "type": r["type"],
                    "access_count": r["access_count"],
                    "last_accessed": r["last_accessed"].isoformat() if r["last_accessed"] else None,
                })

            # Type breakdown
            cur.execute("""
                SELECT type, COUNT(*) AS cnt FROM thoughts WHERE created_at >= %s
                GROUP BY type ORDER BY cnt DESC
            """, (current_start,))
            by_type = {r["type"]: r["cnt"] for r in cur.fetchall()}

    return {
        "period_days": days,
        "current_period": {
            "thoughts_captured": current_count,
            "by_type": by_type,
            "by_machine": by_machine,
            "by_source": by_source,
        },
        "previous_period": {"thoughts_captured": previous_count},
        "change": f"{((current_count - previous_count) / max(previous_count, 1)) * 100:+.0f}%" if previous_count else "no previous data",
        "trending": {"rising": rising, "declining": declining},
        "hottest_memories": hottest,
    }


def record_access(thought_ids):
    """Bump access_count and last_accessed for the given thought IDs.

    No more write queue needed — PostgreSQL handles concurrent updates natively.
    """
    if not thought_ids:
        return

    now = datetime.now().isoformat()

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Use ANY() array syntax for batch update — single query for all IDs
            cur.execute(
                "UPDATE thoughts SET access_count = access_count + 1, last_accessed = %s WHERE id = ANY(%s)",
                (now, list(thought_ids))
            )
        conn.commit()


def list_recent(limit=20, hours=168, offset=0):
    """Get the most recent thoughts within a time window.
    Default: last 7 days (168 hours).
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, content, type, tags, people, source, created_at,
                       machine, trigger, status, priority
                FROM thoughts
                WHERE created_at >= %s
                  AND (status IS NULL OR status != 'archived')
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (cutoff, limit, offset))
            rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    return results


def search_by_tag(tag, limit=20):
    """Find all thoughts with a specific tag. Case-insensitive."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                       t.created_at, t.machine, t.trigger, t.status, t.priority
                FROM thoughts t
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(t.tags) elem
                    WHERE LOWER(elem) = LOWER(%s)
                )
                ORDER BY t.created_at DESC
                LIMIT %s
            """, (tag, limit))
            rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    return results


def search_by_person(person, limit=20):
    """Find all thoughts that mention a specific person.
    Case-insensitive partial match — "chris" matches "Chris Mitchell"."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.id, t.content, t.type, t.tags, t.people, t.source,
                       t.created_at, t.machine, t.trigger, t.status, t.priority
                FROM thoughts t
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(t.people) elem
                    WHERE LOWER(elem) LIKE LOWER(%s)
                )
                ORDER BY t.created_at DESC
                LIMIT %s
            """, (f"%{person}%", limit))
            rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

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
    If content changes, the caller should also pass new_embedding.
    tsvector search index is auto-updated by the PostgreSQL trigger.
    """
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch current state for audit trail
            cur.execute(
                "SELECT id, content, type, tags, people, source, status, priority FROM thoughts WHERE id = %s",
                (thought_id,)
            )
            existing = cur.fetchone()
            if not existing:
                return False

            old_content = existing["content"]
            old_source = existing["source"]

            # Build dynamic UPDATE
            updates = []
            params = []
            changed_fields = []

            if content is not None:
                updates.append("content = %s")
                params.append(content)
                changed_fields.append("content")
            if thought_type is not None:
                updates.append("type = %s")
                params.append(thought_type)
                changed_fields.append("type")
            if tags is not None:
                updates.append("tags = %s")
                params.append(psycopg2.extras.Json(tags))
                changed_fields.append("tags")
            if people is not None:
                updates.append("people = %s")
                params.append(psycopg2.extras.Json(people))
                changed_fields.append("people")
            if status is not None:
                updates.append("status = %s")
                params.append(_normalize_status(status))
                changed_fields.append("status")
            if priority is not None:
                updates.append("priority = %s")
                params.append(_normalize_priority(priority))
                changed_fields.append("priority")

            if updates:
                params.append(thought_id)
                cur.execute(
                    f"UPDATE thoughts SET {', '.join(updates)} WHERE id = %s",
                    params
                )

            # Update embedding if provided
            if new_embedding is not None:
                cur.execute(
                    "UPDATE thought_embeddings SET embedding = %s WHERE thought_id = %s",
                    (new_embedding, thought_id)
                )
                changed_fields.append("embedding")

            # NOTE: tsvector search_vector is auto-updated by the trigger —
            # no manual FTS rebuild needed (unlike the old SQLite FTS5 approach)

        conn.commit()

    # Audit trail
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

    With PostgreSQL's ON DELETE CASCADE, deleting from thoughts automatically
    removes the embedding from thought_embeddings. No manual cleanup needed.
    tsvector entries are also removed automatically (they're columns, not a separate table).
    """
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch before delete for audit trail
            cur.execute(
                "SELECT id, content, tags, people, source FROM thoughts WHERE id = %s",
                (thought_id,)
            )
            existing = cur.fetchone()
            if not existing:
                return False

            old_content = existing["content"]
            old_source = existing["source"]

            # Single DELETE — CASCADE handles thought_embeddings and thought_files
            cur.execute("DELETE FROM thoughts WHERE id = %s", (thought_id,))

        conn.commit()

    log_history(thought_id, action="delete", old_content=old_content, source=old_source)
    return True


def get_thought_by_id(thought_id):
    """Fetch a single thought by its ID. Returns dict or None if not found."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, content, type, tags, people, source, created_at,
                          last_accessed, access_count, machine, trigger, status, priority
                   FROM thoughts WHERE id = %s""",
                (thought_id,)
            )
            row = cur.fetchone()

    if not row:
        return None

    return {
        "id": row["id"],
        "content": row["content"],
        "type": row["type"],
        "tags": row["tags"] or [],
        "people": row["people"] or [],
        "source": row["source"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_accessed": row["last_accessed"].isoformat() if row["last_accessed"] else None,
        "access_count": row["access_count"] or 0,
        "machine": row["machine"] or "unknown",
        "trigger": row["trigger"] or "unknown",
        "status": row["status"] or "none",
        "priority": row["priority"] or 0,
    }


def get_stats():
    """Get database statistics — total thoughts, type breakdown, top tags, top people."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM thoughts")
            total = cur.fetchone()["cnt"]

            cur.execute("SELECT type, COUNT(*) AS cnt FROM thoughts GROUP BY type ORDER BY cnt DESC")
            types = {r["type"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("SELECT source, COUNT(*) AS cnt FROM thoughts GROUP BY source ORDER BY cnt DESC")
            sources = {r["source"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("""
                SELECT COALESCE(machine, 'unknown') AS machine, COUNT(*) AS cnt
                FROM thoughts GROUP BY machine ORDER BY cnt DESC
            """)
            machines = {r["machine"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("""
                SELECT COALESCE(trigger, 'unknown') AS trigger, COUNT(*) AS cnt
                FROM thoughts GROUP BY trigger ORDER BY cnt DESC
            """)
            triggers = {r["trigger"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("""
                SELECT COALESCE(status, 'none') AS status, COUNT(*) AS cnt
                FROM thoughts GROUP BY status ORDER BY cnt DESC
            """)
            statuses = {r["status"]: r["cnt"] for r in cur.fetchall()}

            # Top 10 tags (using JSONB unnest)
            cur.execute("""
                SELECT LOWER(elem::text) AS tag, COUNT(*) AS cnt
                FROM thoughts t, jsonb_array_elements_text(t.tags) AS elem
                GROUP BY LOWER(elem::text)
                ORDER BY cnt DESC LIMIT 10
            """)
            top_tags = {r["tag"]: r["cnt"] for r in cur.fetchall()}

            # Top 10 people
            cur.execute("""
                SELECT LOWER(elem::text) AS person, COUNT(*) AS cnt
                FROM thoughts t, jsonb_array_elements_text(t.people) AS elem
                GROUP BY LOWER(elem::text)
                ORDER BY cnt DESC LIMIT 10
            """)
            top_people = {r["person"]: r["cnt"] for r in cur.fetchall()}

            # Database size (PostgreSQL way)
            cur.execute("SELECT pg_database_size(%s) AS size_bytes", (PG_DB,))
            db_size_bytes = cur.fetchone()["size_bytes"]
            db_size_mb = round(db_size_bytes / (1024 * 1024), 2)

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
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT agent_name, startup_mode, instructions, metadata, updated_at FROM agent_profiles WHERE agent_name = %s",
                (agent_name,),
            )
            row = cur.fetchone()

    if not row:
        return None

    return {
        "agent_name": row["agent_name"],
        "startup_mode": row["startup_mode"] or "standard",
        "instructions": row["instructions"] or "",
        "metadata": row["metadata"] or {},
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def upsert_agent_profile(agent_name, startup_mode="standard", instructions="", metadata=None):
    """Create or update an agent profile."""
    metadata = metadata or {}
    now = datetime.now().isoformat()

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # PostgreSQL's ON CONFLICT is cleaner than SQLite's — same syntax though
            cur.execute("""
                INSERT INTO agent_profiles (agent_name, startup_mode, instructions, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(agent_name) DO UPDATE SET
                    startup_mode = EXCLUDED.startup_mode,
                    instructions = EXCLUDED.instructions,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
            """, (agent_name, startup_mode, instructions, psycopg2.extras.Json(metadata), now))
        conn.commit()

    return get_agent_profile(agent_name)


def generate_change_digest(days=1, limit=20):
    """Generate a compact change digest for a recent window."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, content, type, tags, people, source, created_at,
                       machine, trigger, status, priority
                FROM thoughts WHERE created_at >= %s
                ORDER BY created_at DESC LIMIT %s
            """, (cutoff, limit))
            rows = cur.fetchall()

            cur.execute("""
                SELECT type, COUNT(*) AS cnt FROM thoughts
                WHERE created_at >= %s GROUP BY type ORDER BY cnt DESC
            """, (cutoff,))
            type_rows = cur.fetchall()

            cur.execute("""
                SELECT COALESCE(status, 'none') AS status, COUNT(*) AS cnt
                FROM thoughts WHERE created_at >= %s GROUP BY status ORDER BY cnt DESC
            """, (cutoff,))
            status_rows = cur.fetchall()

    items = []
    for row in rows:
        items.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
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
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, content, type, tags, people, source, created_at,
                       machine, trigger, status, priority
                FROM thoughts ORDER BY created_at DESC LIMIT %s
            """, (recent_limit,))
            recent_rows = cur.fetchall()

            cur.execute("""
                SELECT id, content, type, tags, people, source, created_at,
                       machine, trigger, status, priority
                FROM thoughts WHERE type = 'project'
                ORDER BY created_at DESC LIMIT %s
            """, (project_limit,))
            project_rows = cur.fetchall()

            cur.execute("""
                SELECT id, content, type, tags, people, source, created_at,
                       machine, trigger, status, priority
                FROM thoughts WHERE status IN ('open', 'in_progress')
                ORDER BY priority DESC, created_at DESC LIMIT %s
            """, (blocker_limit,))
            blocker_rows = cur.fetchall()

            # Fallback for data without status
            if not blocker_rows:
                cur.execute("""
                    SELECT DISTINCT t.id, t.content, t.type, t.tags, t.people, t.source,
                           t.created_at, t.machine, t.trigger, t.status, t.priority
                    FROM thoughts t
                    WHERE EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(t.tags) elem
                        WHERE LOWER(elem) IN ('blocker', 'blocked', 'todo')
                    )
                    ORDER BY t.created_at DESC LIMIT %s
                """, (blocker_limit,))
                blocker_rows = cur.fetchall()

    def _rows_to_items(rows):
        items = []
        for row in rows:
            items.append({
                "id": row["id"],
                "content": row["content"],
                "type": row["type"],
                "tags": row["tags"] or [],
                "people": row["people"] or [],
                "source": row["source"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
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
    """Get audit trail entries from the thought_history table."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if thought_id is not None:
                cur.execute("""
                    SELECT id, thought_id, action, old_content, new_content,
                           changed_fields, source, timestamp
                    FROM thought_history WHERE thought_id = %s
                    ORDER BY timestamp DESC LIMIT %s
                """, (thought_id, limit))
            else:
                cur.execute("""
                    SELECT id, thought_id, action, old_content, new_content,
                           changed_fields, source, timestamp
                    FROM thought_history ORDER BY timestamp DESC LIMIT %s
                """, (limit,))
            rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "thought_id": row["thought_id"],
            "action": row["action"],
            "old_content": row["old_content"],
            "new_content": row["new_content"],
            "changed_fields": row["changed_fields"] or [],
            "source": row["source"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
        })

    return results


def search_by_timerange(date_from=None, date_to=None, thought_type=None, source=None, machine=None, limit=20):
    """Search thoughts within a time range with optional filters."""
    query = """SELECT id, content, type, tags, people, source, created_at,
                      machine, trigger, status, priority
               FROM thoughts"""
    conditions = []
    params = []

    if date_from is not None:
        conditions.append("created_at >= %s")
        params.append(date_from)

    if date_to is not None:
        conditions.append("created_at <= %s")
        params.append(date_to + " 23:59:59")

    if thought_type is not None:
        conditions.append("type = %s")
        params.append(thought_type)

    if source is not None:
        conditions.append("source = %s")
        params.append(source)

    if machine is not None:
        conditions.append("machine = %s")
        params.append(machine)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "content": row["content"],
            "type": row["type"],
            "tags": row["tags"] or [],
            "people": row["people"] or [],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "machine": row["machine"] or "unknown",
            "trigger": row["trigger"] or "unknown",
            "status": row["status"] or "none",
            "priority": row["priority"] or 0,
        })

    return results
