# STRATA — Agent Audit Log
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under PolyForm Noncommercial 1.0.0 - see LICENSE file
#
# Every agent interaction with Strata gets logged here — who did what,
# when, and what came back. This is the "replay tape" that lets you trace
# exactly how an AI agent built its understanding of your world model.
#
# WHY THIS MATTERS (Edge 4 — Interpretive Boundary):
# When an agent makes a bad decision, you can trace it back: "Agent X
# searched for Y, got thoughts [12, 45, 89], missed thought #67 which
# had the critical context." No other AI memory system offers this
# transparency. The audit log IS the interpretive boundary made visible.
#
# STORAGE:
# Daily CSV files in {DATA_DIR}/audit/ (or STRATA_AUDIT_DIR env var).
# One file per day: audit_2026-04-19.csv, audit_2026-04-20.csv, etc.
# CSV because it's greppable, portable, and doesn't need a database.
# You can open these in Excel, import them into pandas, or just cat them.
#
# PERFORMANCE:
# Logging happens AFTER the operation completes — it never slows down
# the actual Strata response. Writes are buffered and flushed periodically.

import csv
import io
import os
import threading
import time
from datetime import datetime, timezone

# --- Configuration ---
# Override the audit log directory with STRATA_AUDIT_DIR env var.
# Default: {script_dir}/data/audit/ (alongside the database)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_DIR = os.environ.get(
    "STRATA_AUDIT_DIR",
    os.path.join(_SCRIPT_DIR, "data", "audit")
)

# Set STRATA_AUDIT_ENABLED=false to disable logging entirely.
# Default: enabled. There's no reason to turn this off unless you're
# running a throwaway test instance and don't want disk writes.
AUDIT_ENABLED = os.environ.get("STRATA_AUDIT_ENABLED", "true").lower() == "true"

# CSV column headers — this is the schema for the audit log.
# Every row in every CSV file has exactly these columns in this order.
AUDIT_COLUMNS = [
    "timestamp",       # ISO 8601 UTC — when the operation completed
    "agent_name",      # Human-readable name from agent_keys table (e.g. "claude-code-surface")
    "agent_key_hint",  # First 10 + last 4 chars of the API key (enough to identify, not enough to use)
    "action",          # What happened: capture_thought, semantic_search, update_thought, etc.
    "detail",          # What was requested — search query, captured content preview, thought ID, etc.
    "thought_ids",     # Which thought IDs were touched (comma-separated, e.g. "759,726,311")
    "result_count",    # How many results came back (for searches) or 1 for single-thought ops
    "response_ms",     # How long the operation took in milliseconds
    "source",          # Where the request came from: mcp, rest-api, dashboard
]

# --- Internal state ---
# Write buffer — entries accumulate here and get flushed to disk periodically
# or when the buffer hits a certain size. This prevents disk I/O on every
# single Strata operation while still capturing everything.
_buffer = []
_buffer_lock = threading.Lock()
_FLUSH_INTERVAL = 5.0   # Flush to disk every 5 seconds
_FLUSH_SIZE = 20         # Or when buffer hits 20 entries, whichever comes first
_flush_thread = None
_initialized = False


def _mask_key(api_key: str) -> str:
    """Mask an API key for the audit log.
    Shows enough to identify which agent, not enough to impersonate.
    'agent-sU1vd1THi8cL2vCDAus37eLwG1imBbsD' → 'agent-sU1v...BbsD'"""
    if not api_key or len(api_key) < 14:
        return api_key or "unknown"
    return api_key[:10] + "..." + api_key[-4:]


def _today_filename() -> str:
    """Get today's audit log filename.
    Format: audit_YYYY-MM-DD.csv — one file per day."""
    return f"audit_{datetime.now().strftime('%Y-%m-%d')}.csv"


def _ensure_dir():
    """Create the audit directory if it doesn't exist."""
    os.makedirs(AUDIT_DIR, exist_ok=True)


def _write_header_if_new(filepath: str):
    """Write CSV header row if the file is new or empty."""
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(AUDIT_COLUMNS)


def _flush_buffer():
    """Write all buffered entries to today's CSV file.
    Called by the flush thread or manually when needed."""
    with _buffer_lock:
        if not _buffer:
            return
        entries = list(_buffer)
        _buffer.clear()

    _ensure_dir()
    filepath = os.path.join(AUDIT_DIR, _today_filename())
    _write_header_if_new(filepath)

    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for entry in entries:
            writer.writerow(entry)


def _flush_loop():
    """Background thread that flushes the buffer every FLUSH_INTERVAL seconds.
    Daemon thread — dies with the process, no cleanup needed."""
    while True:
        time.sleep(_FLUSH_INTERVAL)
        try:
            _flush_buffer()
        except Exception as e:
            # Never crash the flush thread — audit logging is best-effort.
            # If the disk is full or permissions are wrong, we lose some
            # log entries but Strata keeps running.
            print(f"[audit] Flush error: {e}")


def init_audit():
    """Initialize the audit log system.
    Call this once during server startup. Creates the directory and
    starts the background flush thread."""
    global _flush_thread, _initialized

    if not AUDIT_ENABLED:
        print("[audit] Audit logging DISABLED (STRATA_AUDIT_ENABLED=false)")
        return

    if _initialized:
        return

    _ensure_dir()
    filepath = os.path.join(AUDIT_DIR, _today_filename())
    _write_header_if_new(filepath)

    # Start the background flush thread
    _flush_thread = threading.Thread(target=_flush_loop, daemon=True, name="strata-audit-flush")
    _flush_thread.start()
    _initialized = True

    print(f"[audit] Audit logging ENABLED — writing to {AUDIT_DIR}")


def log_action(
    agent_name: str = "unknown",
    agent_key: str = "",
    action: str = "",
    detail: str = "",
    thought_ids: list = None,
    result_count: int = 0,
    response_ms: float = 0.0,
    source: str = "mcp",
):
    """Log a single agent action to the audit trail.

    Call this AFTER the operation completes — it should never block
    or slow down the actual Strata response.

    Args:
        agent_name: Human-readable agent name (from agent_keys table)
        agent_key: The raw API key (gets masked automatically)
        action: What happened — e.g. "capture_thought", "semantic_search"
        detail: Human-readable summary — search query, content preview, etc.
                Automatically truncated to 200 chars to keep CSV rows manageable.
        thought_ids: List of thought IDs involved in the operation
        result_count: Number of results returned (for searches)
        response_ms: Operation duration in milliseconds
        source: Where the request came from — "mcp", "rest-api", "dashboard"
    """
    if not AUDIT_ENABLED:
        return

    # Truncate detail to keep CSV rows manageable
    if detail and len(detail) > 200:
        detail = detail[:197] + "..."

    # Format thought IDs as comma-separated string
    ids_str = ",".join(str(tid) for tid in (thought_ids or []))

    entry = [
        datetime.now(timezone.utc).isoformat(),  # timestamp (UTC)
        agent_name,                                # agent_name
        _mask_key(agent_key),                      # agent_key_hint
        action,                                    # action
        detail,                                    # detail
        ids_str,                                   # thought_ids
        result_count,                              # result_count
        round(response_ms, 1),                     # response_ms
        source,                                    # source
    ]

    with _buffer_lock:
        _buffer.append(entry)

        # Flush immediately if buffer is full (don't wait for the timer)
        if len(_buffer) >= _FLUSH_SIZE:
            # Schedule flush without holding the lock
            threading.Thread(target=_flush_buffer, daemon=True).start()


def flush():
    """Manually flush the buffer. Call this during graceful shutdown
    to make sure no entries are lost."""
    _flush_buffer()


def get_today_log() -> str:
    """Read today's audit log as a string. Useful for dashboard display."""
    filepath = os.path.join(AUDIT_DIR, _today_filename())
    if not os.path.exists(filepath):
        return ""
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def get_recent_entries(limit: int = 50) -> list:
    """Get the most recent audit entries as a list of dicts.
    Reads from today's CSV file, returns newest first."""
    filepath = os.path.join(AUDIT_DIR, _today_filename())
    if not os.path.exists(filepath):
        return []

    # Flush first so we have the latest data
    _flush_buffer()

    entries = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entries.append(row)

    # Return newest first, limited
    return list(reversed(entries[-limit:]))


def list_log_files() -> list:
    """List all audit log files with their sizes.
    Returns list of dicts with filename, date, size_kb."""
    if not os.path.exists(AUDIT_DIR):
        return []

    files = []
    for fname in sorted(os.listdir(AUDIT_DIR), reverse=True):
        if fname.startswith("audit_") and fname.endswith(".csv"):
            fpath = os.path.join(AUDIT_DIR, fname)
            # Extract date from filename: audit_2026-04-19.csv → 2026-04-19
            date_str = fname.replace("audit_", "").replace(".csv", "")
            files.append({
                "filename": fname,
                "date": date_str,
                "size_kb": round(os.path.getsize(fpath) / 1024, 1),
            })
    return files
