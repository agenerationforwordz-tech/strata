# STRATA - Self-Hosted AI Memory Server
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under PolyForm Noncommercial 1.0.0 - see LICENSE file
#
# Central config. Override paths and keys via environment variables.

import os

# --- Server ---
HOST = os.environ.get("STRATA_HOST", "0.0.0.0")  # Listen on all interfaces for LAN access
PORT = int(os.environ.get("STRATA_PORT", "4320"))
SERVER_NAME = "strata"

# --- Authentication ---
# API key protects all REST endpoints (/api/capture, /api/search).
# MCP transport (/mcp) has its own auth flow and is NOT gated by this key.
# Set via environment variable for security. The default is intentionally
# obvious so nobody ships with an open server by accident.
API_KEY = os.environ.get("STRATA_API_KEY", "change-me-before-deploy")

# Set to False to disable auth entirely (NOT recommended for shared networks)
AUTH_ENABLED = os.environ.get("STRATA_AUTH_ENABLED", "true").lower() == "true"

# --- Database Backend ---
# "sqlite" = works out of the box, no external DB needed (good for demos/single user)
# "postgresql" = concurrent multi-agent access, pgvector similarity search, scales to 1M+ thoughts
DB_BACKEND = os.environ.get("STRATA_DB_BACKEND", "sqlite")

# --- Demo Mode ---
# When STRATA_DEMO_MODE=true, the dashboard auth flow accepts blank passwords:
# the login screen still renders (so visitors see the auth feature exists)
# but anyone can sign in by leaving the password empty. Behind the scenes
# the server substitutes a fixed internal password so all the existing
# hashing / session / device-name plumbing keeps working.
#
# This is for the public-facing demo at port 4320 ONLY. NEVER set this on a
# real deployment — it removes the authentication barrier entirely.
DEMO_MODE = os.environ.get("STRATA_DEMO_MODE", "").lower() == "true"
DEMO_BYPASS_PASSWORD = "strata-demo-bypass-password-do-not-use-in-production"

# --- Admin Key (for destructive operations) ---
# Destructive actions (delete thought, detach file) require this key.
# AI clients DON'T know this key, so they literally cannot delete anything
# without the human owner providing it. This is the safety net.
# If not set, delete operations are DISABLED entirely - no fallback to API_KEY.
# This prevents any API client from automatically having admin privileges.
ADMIN_KEY = os.environ.get("STRATA_ADMIN_KEY", "")

# --- Paths ---
# Override DATA_DIR via environment variable to store the DB wherever you want.
# Default: ./data (relative to where you run the server)
DATA_DIR = os.environ.get("STRATA_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))

# DB filename: 'strata.db' is the default. If an older filename is already
# on disk from an upgrade we keep using it rather than forcing a rename out
# from under a running deploy. Anyone who wants the new name can rename the
# file manually (and the WAL/SHM sidecars if present).
_strata_db = os.path.join(DATA_DIR, "strata.db")
_legacy_candidates = [os.path.join(DATA_DIR, name) for name in ("brain.db",)]
DB_PATH = _strata_db
for _candidate in _legacy_candidates:
    if os.path.exists(_candidate) and not os.path.exists(_strata_db):
        DB_PATH = _candidate
        break

BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOG_DIR = os.environ.get("STRATA_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))

# --- Embedding Model ---
# BAAI/bge-base-en-v1.5: 768 dims, ONNX format via fastembed
# Scores better than all-mpnet-base-v2 on most benchmarks AND runs on
# Raspberry Pi without PyTorch (which needs ~1.5GB RAM on ARM).
# fastembed uses ONNX Runtime instead (~100MB RAM). Embeds in ~0.16s per thought on Pi 4B.
MODEL_NAME = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIM = 768
IDLE_TIMEOUT = 300  # 5 minutes - unload model after this many seconds of no use

# --- Search Defaults ---
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_RECENT_LIMIT = 20
DEFAULT_RECENT_HOURS = 168  # 7 days

# --- Deduplication ---
# When capturing a new thought, check existing thoughts for cosine similarity.
# If any existing thought scores above this threshold, warn instead of blindly saving.
# 0.85 = very similar content (likely a duplicate or near-duplicate)
# 0.90 = almost identical wording
DEDUP_THRESHOLD = 0.85

# --- Input Limits ---
# Prevents abuse / DoS via oversized payloads. 50KB is plenty for any note.
# Large files should be stored on disk and referenced by path, not inlined.
MAX_CONTENT_LENGTH = 50_000    # 50KB per thought - generous for text notes
MAX_TAGS = 50                   # Max tags per thought
MAX_PEOPLE = 50                 # Max people per thought
MAX_TAG_LENGTH = 100            # Max chars per individual tag
MAX_PERSON_LENGTH = 100         # Max chars per person name

# --- File Vault ---
# The vault stores actual files attached to thoughts. This is what turns
# Strata from a text memory into a full knowledge system. Thoughts are
# the semantic index; the vault holds the real content - code files, documents,
# entire projects. Organized as vault/{device}/{YYYY-MM}/{thought_id}/
VAULT_DIR = os.environ.get("STRATA_VAULT_DIR", os.path.join(DATA_DIR, "vault"))
MAX_FILE_SIZE = 1_000_000_000          # 1GB per file - enough for a whole project archive
MAX_ATTACHMENT_CONTENT = 50_000_000    # 50MB for base64 content via MCP (larger files use REST upload)
MAX_ATTACHMENTS_PER_THOUGHT = 500     # generous cap - a project could have hundreds of files

# --- Build Info ---
# Internal build identifier for version tracking and support diagnostics.
BUILD_SIGNATURE = "AGFW-CM-MV2026"  # Do not modify - used by health endpoint

# --- Thought Types ---
# Used for classification. "thought" is the catch-all default.
# IMPORTANT: every type the dashboard renders MUST appear in this list, or
# agents capturing that type get rejected with "Invalid type". The dashboard
# shows 10 sections; if you add a new section there, add it here too.
VALID_TYPES = [
    "thought",      # General note that doesn't fit other categories - the catch-all
    "decision",     # A choice that was made, with reasoning - captures the WHY behind it
    "session",      # End-of-session summary - what happened, what was accomplished
    "person",       # Notes about a specific person - relationships, context, preferences
    "insight",      # A realization or learning - something clicked, a pattern recognized
    "project",      # Project-specific context - status, architecture, dependencies, goals
    "instruction",  # How-to, working preferences, rules to follow - operational guidance
    "reference",    # Technical docs, links, specs, factual records - look-up material
    "idea",         # Something that HASN'T been decided yet - brainstorm, what-if, exploration (NOT a decision)
    "observation",  # A pattern noticed but no conclusions drawn - "I noticed X keeps happening" (NOT an insight)
]
