#!/usr/bin/env python3
"""STRATA — SQLite to PostgreSQL Migration Script

Migrates all data from the SQLite brain.db to the new PostgreSQL strata_db.
Run this ONCE after creating the PostgreSQL schema.

Usage:
    cd /home/nacho/strata
    ./venv/bin/python migrate_to_pg.py

What it migrates:
    - All thoughts (positive AND negative IDs) with all fields
    - All embeddings (converted from binary blobs to pgvector vectors)
    - All thought_history audit entries
    - All agent_keys
    - All agent_profiles
    - All users/sessions (dashboard auth)

After migration, it resets the PostgreSQL sequence so new auto-generated
IDs start after the highest existing positive ID.
"""

import sqlite3
import struct
import json
import sys

import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector

# --- Config ---
SQLITE_PATH = "/mnt/nas-main/open-brain/brain.db"
PG_DSN = "host=localhost port=5432 dbname=strata_db user=strata password=strata_brain_2026"


def deserialize_embedding(blob):
    """Convert SQLite binary blob back to a list of floats."""
    n = len(blob) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{n}f", blob))


def migrate():
    # Connect to both databases
    print(f"[migrate] Opening SQLite: {SQLITE_PATH}")
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    print(f"[migrate] Connecting to PostgreSQL...")
    pg_conn = psycopg2.connect(PG_DSN)
    register_vector(pg_conn)
    pg_cur = pg_conn.cursor()

    # --- 1. Migrate thoughts ---
    print("[migrate] Migrating thoughts...")
    rows = sqlite_conn.execute("""
        SELECT id, content, type, tags, people, source, created_at,
               last_accessed, access_count, machine, trigger, status, priority, original_date
        FROM thoughts ORDER BY id
    """).fetchall()

    thought_count = 0
    for row in rows:
        # Tags and people are JSON strings in SQLite, need to be JSONB in PG
        tags = json.loads(row["tags"]) if row["tags"] else []
        people = json.loads(row["people"]) if row["people"] else []

        pg_cur.execute("""
            INSERT INTO thoughts (id, content, type, tags, people, source, created_at,
                                  last_accessed, access_count, machine, trigger, status,
                                  priority, original_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (
            row["id"], row["content"], row["type"],
            psycopg2.extras.Json(tags), psycopg2.extras.Json(people),
            row["source"], row["created_at"], row["last_accessed"],
            row["access_count"] or 0, row["machine"], row["trigger"],
            row["status"] or "none", row["priority"] or 0, row["original_date"]
        ))
        thought_count += 1

    print(f"[migrate] Migrated {thought_count} thoughts")

    # --- 2. Migrate embeddings ---
    print("[migrate] Migrating embeddings...")
    emb_rows = sqlite_conn.execute(
        "SELECT thought_id, embedding FROM thought_embeddings"
    ).fetchall()

    emb_count = 0
    skipped = 0
    for row in emb_rows:
        embedding = deserialize_embedding(row["embedding"])
        # Skip embeddings with wrong dimensions (BUG-06 legacy)
        if len(embedding) != 768:
            skipped += 1
            continue

        pg_cur.execute("""
            INSERT INTO thought_embeddings (thought_id, embedding)
            VALUES (%s, %s)
            ON CONFLICT (thought_id) DO NOTHING
        """, (row["thought_id"], embedding))
        emb_count += 1

    print(f"[migrate] Migrated {emb_count} embeddings ({skipped} skipped — wrong dimensions)")

    # --- 3. Migrate thought_history ---
    print("[migrate] Migrating thought_history...")
    hist_rows = sqlite_conn.execute("""
        SELECT thought_id, action, old_content, new_content, changed_fields, source, timestamp
        FROM thought_history ORDER BY id
    """).fetchall()

    hist_count = 0
    for row in hist_rows:
        changed = json.loads(row["changed_fields"]) if row["changed_fields"] else []
        pg_cur.execute("""
            INSERT INTO thought_history (thought_id, action, old_content, new_content,
                                         changed_fields, source, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            row["thought_id"], row["action"], row["old_content"], row["new_content"],
            psycopg2.extras.Json(changed), row["source"], row["timestamp"]
        ))
        hist_count += 1

    print(f"[migrate] Migrated {hist_count} history entries")

    # --- 4. Migrate agent_keys ---
    print("[migrate] Migrating agent_keys...")
    try:
        ak_rows = sqlite_conn.execute("SELECT * FROM agent_keys ORDER BY id").fetchall()
        ak_count = 0
        for row in ak_rows:
            pg_cur.execute("""
                INSERT INTO agent_keys (id, agent_name, api_key, enabled, can_read, can_write,
                                        can_delete, can_admin, created_at, last_used, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (
                row["id"], row["agent_name"], row["api_key"], row["enabled"],
                row["can_read"], row["can_write"], row["can_delete"],
                row.get("can_admin", 0) if hasattr(row, "get") else (row["can_admin"] if "can_admin" in row.keys() else 0),
                row["created_at"], row["last_used"], row["notes"]
            ))
            ak_count += 1
        print(f"[migrate] Migrated {ak_count} agent keys")
    except Exception as e:
        print(f"[migrate] agent_keys: {e} (may not exist — skipping)")

    # --- 5. Migrate agent_profiles ---
    print("[migrate] Migrating agent_profiles...")
    try:
        ap_rows = sqlite_conn.execute("SELECT * FROM agent_profiles").fetchall()
        ap_count = 0
        for row in ap_rows:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            pg_cur.execute("""
                INSERT INTO agent_profiles (agent_name, startup_mode, instructions, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (agent_name) DO NOTHING
            """, (
                row["agent_name"], row["startup_mode"], row["instructions"],
                psycopg2.extras.Json(metadata), row["updated_at"]
            ))
            ap_count += 1
        print(f"[migrate] Migrated {ap_count} agent profiles")
    except Exception as e:
        print(f"[migrate] agent_profiles: {e} (may not exist — skipping)")

    # --- 6. Migrate users/sessions ---
    print("[migrate] Migrating users and sessions...")
    try:
        user_rows = sqlite_conn.execute("SELECT * FROM users").fetchall()
        for row in user_rows:
            pg_cur.execute("""
                INSERT INTO users (id, username, password_hash, seed_hash, created_at, last_login)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (row["id"], row["username"], row["password_hash"],
                  row.get("seed_hash", None) if hasattr(row, "get") else None,
                  row["created_at"], row.get("last_login", None) if hasattr(row, "get") else None))
        print(f"[migrate] Migrated {len(user_rows)} users")
    except Exception as e:
        print(f"[migrate] users: {e} (may not exist — skipping)")

    # --- 7. Reset sequences ---
    # PostgreSQL SERIAL sequences need to know the current max ID so new
    # auto-generated IDs don't conflict with migrated ones.
    print("[migrate] Resetting PostgreSQL sequences...")
    pg_cur.execute("SELECT COALESCE(MAX(id), 0) FROM thoughts WHERE id > 0")
    max_thought_id = pg_cur.fetchone()[0]
    if max_thought_id > 0:
        pg_cur.execute(f"SELECT setval('thoughts_id_seq', {max_thought_id})")
        print(f"[migrate] thoughts_id_seq set to {max_thought_id}")

    pg_cur.execute("SELECT COALESCE(MAX(id), 0) FROM thought_history")
    max_hist_id = pg_cur.fetchone()[0]
    if max_hist_id > 0:
        pg_cur.execute(f"SELECT setval('thought_history_id_seq', {max_hist_id})")
        print(f"[migrate] thought_history_id_seq set to {max_hist_id}")

    pg_cur.execute("SELECT COALESCE(MAX(id), 0) FROM agent_keys")
    max_ak_id = pg_cur.fetchone()[0]
    if max_ak_id > 0:
        pg_cur.execute(f"SELECT setval('agent_keys_id_seq', {max_ak_id})")
        print(f"[migrate] agent_keys_id_seq set to {max_ak_id}")

    # --- 8. Commit and verify ---
    pg_conn.commit()

    # Verify counts
    pg_cur.execute("SELECT COUNT(*) FROM thoughts")
    pg_thoughts = pg_cur.fetchone()[0]
    pg_cur.execute("SELECT COUNT(*) FROM thought_embeddings")
    pg_embeddings = pg_cur.fetchone()[0]
    pg_cur.execute("SELECT COUNT(*) FROM thought_history")
    pg_history = pg_cur.fetchone()[0]

    print(f"\n[migrate] === MIGRATION COMPLETE ===")
    print(f"[migrate] Thoughts:   {pg_thoughts}")
    print(f"[migrate] Embeddings: {pg_embeddings}")
    print(f"[migrate] History:    {pg_history}")
    print(f"[migrate] SQLite DB preserved at: {SQLITE_PATH}")
    print(f"[migrate] PostgreSQL ready at: strata_db")

    # Quick semantic search test
    print("\n[migrate] Running quick vector search test...")
    pg_cur.execute("""
        SELECT t.id, LEFT(t.content, 80) AS preview,
               1 - (e.embedding <=> (SELECT embedding FROM thought_embeddings WHERE thought_id = 1)) AS similarity
        FROM thought_embeddings e
        JOIN thoughts t ON t.id = e.thought_id
        WHERE t.id != 1
        ORDER BY e.embedding <=> (SELECT embedding FROM thought_embeddings WHERE thought_id = 1)
        LIMIT 3
    """)
    test_rows = pg_cur.fetchall()
    if test_rows:
        print("[migrate] Top 3 similar to thought #1:")
        for r in test_rows:
            print(f"  #{r[0]} (sim={r[2]:.4f}): {r[1]}...")
    else:
        print("[migrate] No test results (thought #1 may not exist)")

    pg_cur.close()
    pg_conn.close()
    sqlite_conn.close()

    print("\n[migrate] Done! Now swap db.py and restart Strata.")


if __name__ == "__main__":
    migrate()
