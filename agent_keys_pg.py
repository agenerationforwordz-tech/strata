# STRATA - Per-Agent API Key System (PostgreSQL)
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under PolyForm Noncommercial 1.0.0 - see LICENSE file
#
# Each AI agent gets its own unique API key with granular permissions.
# Uses the shared PostgreSQL connection pool from db.py.

import secrets
import hmac
from datetime import datetime

import psycopg2
import psycopg2.extras

# Use the shared PostgreSQL connection pool from db.py
from db import _get_conn


def init_agent_keys_table():
    """Verify agent_keys table exists in PostgreSQL.
    Schema is created by strata_pg_schema.sql — this is just a safety check."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agent_keys")
            count = cur.fetchone()[0]
            print(f"[agent_keys] {count} agent keys loaded")


def generate_agent_key():
    """Generate a unique, URL-safe agent API key.
    Format: agent-<32 random chars> so it's visually distinct from the admin key."""
    return f"agent-{secrets.token_urlsafe(24)}"


def create_agent(agent_name, can_read=True, can_write=True, can_delete=False, can_admin=False, notes=""):
    """Register a new agent and generate its API key.
    Returns (agent_dict, None) on success, (None, error) on failure."""
    if not agent_name or not agent_name.strip():
        return None, "Agent name is required."

    api_key = generate_agent_key()
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    """INSERT INTO agent_keys (agent_name, api_key, enabled, can_read, can_write, can_delete, can_admin, notes)
                       VALUES (%s, %s, 1, %s, %s, %s, %s, %s)""",
                    (agent_name.strip(), api_key, int(can_read), int(can_write),
                     int(can_delete), int(can_admin), notes),
                )
                conn.commit()
                cur.execute("SELECT * FROM agent_keys WHERE api_key = %s", (api_key,))
                row = cur.fetchone()
                return dict(row), None
            except psycopg2.IntegrityError:
                conn.rollback()
                return None, "Key collision (extremely rare). Try again."


def list_agents():
    """List all registered agents with their status and permissions.
    Keys are masked in the list view for safety."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_keys ORDER BY created_at DESC")
            rows = cur.fetchall()
            agents = []
            for r in rows:
                d = dict(r)
                key = d['api_key']
                d['api_key_masked'] = key[:10] + '...' + key[-4:] if len(key) > 14 else key
                # Convert datetime objects to strings for JSON serialization
                if d.get('created_at') and hasattr(d['created_at'], 'isoformat'):
                    d['created_at'] = d['created_at'].isoformat()
                if d.get('last_used') and hasattr(d['last_used'], 'isoformat'):
                    d['last_used'] = d['last_used'].isoformat()
                agents.append(d)
            return agents


def get_agent(agent_id):
    """Get a single agent by ID. Returns full record including unmasked key."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_keys WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            if d.get("last_used") and hasattr(d["last_used"], "isoformat"):
                d["last_used"] = d["last_used"].isoformat()
            return d


def get_agent_by_key(api_key):
    """Look up an agent by their API key. Used during auth checks.
    Returns agent dict or None. Updates last_used timestamp."""
    if not api_key:
        return None
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_keys WHERE api_key = %s", (api_key,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE agent_keys SET last_used = %s WHERE id = %s",
                    (datetime.now().isoformat(), row['id']),
                )
                conn.commit()
                return dict(row)
            return None


def update_agent(agent_id, enabled=None, can_read=None, can_write=None, can_delete=None, can_admin=None, agent_name=None, notes=None):
    """Update an agent's permissions or status. Only provided fields are changed."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_keys WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if not row:
                return False, "Agent not found."

            updates = []
            params = []
            if enabled is not None:
                updates.append("enabled = %s")
                params.append(int(enabled))
            if can_read is not None:
                updates.append("can_read = %s")
                params.append(int(can_read))
            if can_write is not None:
                updates.append("can_write = %s")
                params.append(int(can_write))
            if can_delete is not None:
                updates.append("can_delete = %s")
                params.append(int(can_delete))
            if can_admin is not None:
                updates.append("can_admin = %s")
                params.append(int(can_admin))
            if agent_name is not None:
                updates.append("agent_name = %s")
                params.append(agent_name.strip())
            if notes is not None:
                updates.append("notes = %s")
                params.append(notes)

            if not updates:
                return True, None

            params.append(agent_id)
            cur.execute(f"UPDATE agent_keys SET {', '.join(updates)} WHERE id = %s", params)
            conn.commit()
            return True, None


def delete_agent(agent_id):
    """Permanently revoke an agent's key. Cannot be undone."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_keys WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if not row:
                return False, "Agent not found."
            cur.execute("DELETE FROM agent_keys WHERE id = %s", (agent_id,))
            conn.commit()
            return True, None


def regenerate_key(agent_id):
    """Generate a new API key for an agent (invalidates the old one)."""
    new_key = generate_agent_key()
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_keys WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if not row:
                return None, "Agent not found."
            cur.execute("UPDATE agent_keys SET api_key = %s WHERE id = %s", (new_key, agent_id))
            conn.commit()
            return new_key, None
