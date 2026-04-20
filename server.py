# STRATA — MCP Server
# The main entry point. Listens on 0.0.0.0:4320 by default.
# Exposes the Strata memory tools via MCP so any AI client (Claude Code,
# Codex CLI, Gemini CLI, or anything else that speaks MCP) can capture
# and search the user's persistent memory.
#
# Dual transport: Streamable HTTP (/mcp) for Codex + Claude Code,
# and SSE (/sse) for Claude Code legacy support.
#
# Usage:
#   python server.py

import asyncio
import hmac
import json
import os
import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime

# Add project dir to path so imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from starlette.routing import Route
from starlette.responses import JSONResponse, Response

from config import (
    HOST, PORT, SERVER_NAME, DEFAULT_SEARCH_LIMIT, DEFAULT_RECENT_LIMIT,
    DEFAULT_RECENT_HOURS, VALID_TYPES, DEDUP_THRESHOLD,
    MAX_CONTENT_LENGTH, MAX_TAGS, MAX_PEOPLE, MAX_TAG_LENGTH, MAX_PERSON_LENGTH,
    API_KEY, AUTH_ENABLED, ADMIN_KEY, DEMO_MODE, DEMO_BYPASS_PASSWORD,
)
import db
import agent_keys
import auth
import audit
import embedder
import numpy as np
from write_queue import WriteQueue

VALID_TASK_STATUSES = {"none", "open", "in_progress", "done"}


# --- IP-to-agent-name resolution ---
# Instead of hardcoding LAN IPs, read from STRATA_IP_AGENTS env var.
# Format: "10.0.0.250=surface,10.0.0.241=helios" (comma-separated IP=name pairs)
# If not set, unknown IPs resolve to "rest-api".
_IP_AGENT_MAP = {}
_raw_ip_agents = os.environ.get("STRATA_IP_AGENTS", "")
if _raw_ip_agents:
    for pair in _raw_ip_agents.split(","):
        pair = pair.strip()
        if "=" in pair:
            ip_prefix, name = pair.split("=", 1)
            _IP_AGENT_MAP[ip_prefix.strip()] = name.strip()


def _resolve_source_from_ip(client_ip: str) -> str:
    """Look up agent name by IP prefix from STRATA_IP_AGENTS env var.
    Falls back to rest-api if no match found."""
    for prefix, name in _IP_AGENT_MAP.items():
        if client_ip.startswith(prefix):
            return name
    return "rest-api"


def normalize_task_status(status: str) -> str:
    """Normalize task status to a known safe value."""
    if status is None:
        return "none"
    status = str(status).strip().lower()
    return status if status in VALID_TASK_STATUSES else "none"


def normalize_priority(priority: int) -> int:
    """Normalize priority into 0-5."""
    try:
        value = int(priority)
    except Exception:
        return 0
    return max(0, min(5, value))


# ============================================================
# OAUTH BYPASS MIDDLEWARE
# ============================================================
# Claude Code (and other MCP clients) probe several OAuth discovery
# endpoints before connecting. The MCP sub-apps (SSE, Streamable HTTP)
# return plain text "Not Found" for unmatched paths, which Claude Code
# can't parse as JSON — causing the connection to fail.
#
# This ASGI middleware sits in front of EVERYTHING and intercepts
# well-known / OAuth / register paths. It returns a JSON 404 which
# tells the client "no auth needed, just connect". Requests to
# actual MCP endpoints (/mcp, /sse, /health) pass through untouched.
# ============================================================

class MCPKillSwitchMiddleware:
    """Check global MCP kill switch. If OFF, reject ALL /mcp, /sse, and /api/* requests.
    Allows /api/auth/* (human login), /admin/* (human re-enable), /dashboard/*,
    /health, and /constellation through -- those are human-facing, not agent-facing.
    This is the emergency brake: when flipped, NO agent can touch memory.
    Also captures the agent API key from request headers for audit logging."""
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        global _current_agent_key
        if scope["type"] == "http":
            path = scope.get("path", "")
            # Capture the API key from headers for audit logging.
            # MCP tool functions don't have access to the HTTP request,
            # so we stash the key in a global that log_activity() reads.
            headers = dict(scope.get("headers", []))
            api_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="ignore")
            if not api_key:
                auth_hdr = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
                if auth_hdr.startswith("Bearer "):
                    api_key = auth_hdr[7:]
            _current_agent_key = api_key
            if not _mcp_global_enabled:
                # Block agent-facing paths: /mcp, /sse, /api/* (except /api/auth/*)
                blocked = (
                    path.startswith("/mcp")
                    or path.startswith("/sse")
                    or (path.startswith("/api/") and not path.startswith("/api/auth/"))
                )
                if blocked:
                    from starlette.responses import JSONResponse as _JR
                    r = _JR(
                        {"error": "MCP access is currently disabled by the server administrator.",
                         "kill_switch": True,
                         "hint": "A human must re-enable access from the admin panel at /admin/agents."},
                        status_code=503,
                    )
                    await r(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class OAuthBypassMiddleware:
    """Intercept OAuth discovery requests and return JSON 404s.

    Without this, paths like /sse/.well-known/oauth-authorization-server
    hit the SSE sub-app which returns plain text 'Not Found', and Claude
    Code chokes trying to parse it as JSON.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only intercept HTTP requests — let WebSocket/lifespan through
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Intercept any well-known, OAuth, or register path at ANY level
        # Claude Code tries: /.well-known/*, /sse/.well-known/*, /mcp/.well-known/*,
        # /.well-known/*/sse, /register, etc.
        should_intercept = (
            "/.well-known/" in path
            or path.endswith("/register")
            or path == "/register"
        )

        if should_intercept:
            # Return JSON 404 — tells MCP clients "no auth required"
            body = json.dumps({
                "error": "not_found",
                "error_description": "This server does not require authentication"
            }).encode("utf-8")

            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
            return

        # Everything else passes through to the real app
        await self.app(scope, receive, send)

# ============================================================
# AUTO-TAGGING — Lightweight regex-based extraction (no ML, no spaCy)
# ============================================================
# Extracts potential tags from content text using pure regex.
# No external dependencies, no RAM cost, runs instantly.
# These are SUGGESTIONS merged with user-provided tags — manual tags
# always take priority and auto-tags never override them.

def auto_extract_tags(content):
    """Extract potential tags from content using regex patterns.

    Looks for: #hashtags, URLs/domains, dates, and common patterns.
    Returns a set of lowercase tag strings. Zero ML, zero RAM cost.

    Args:
        content: The thought text to extract tags from

    Returns:
        Set of extracted tag strings (lowercase, deduplicated)
    """
    tags = set()

    # #hashtags — people naturally write these in notes
    hashtags = re.findall(r'#(\w+)', content)
    tags.update(h.lower() for h in hashtags)

    # Domains from URLs — extract the domain name as a tag
    urls = re.findall(r'https?://(?:www\.)?([a-zA-Z0-9.-]+)', content)
    for url in urls:
        # Turn "github.com" into "github", "coinbase.com" into "coinbase"
        domain = url.split('.')[0].lower()
        if len(domain) > 2:  # Skip tiny fragments like "io", "co"
            tags.update([domain])

    # Dollar amounts — tag as "financial" if money is mentioned
    if re.search(r'\$[\d,]+', content):
        tags.add("financial")

    # Bitcoin/crypto mentions
    if re.search(r'\b(BTC|bitcoin|satoshi|sats)\b', content, re.IGNORECASE):
        tags.add("bitcoin")
    if re.search(r'\b(ETH|ethereum)\b', content, re.IGNORECASE):
        tags.add("ethereum")
    if re.search(r'\b(XRP|ripple)\b', content, re.IGNORECASE):
        tags.add("xrp")

    # Common tech terms
    if re.search(r'\b(docker|container)\b', content, re.IGNORECASE):
        tags.add("docker")
    if re.search(r'\b(raspberry pi|pi-nas|pi ?4|pi ?5)\b', content, re.IGNORECASE):
        tags.add("raspberry-pi")
    if re.search(r'\b(telegram|bot)\b', content, re.IGNORECASE):
        tags.add("telegram")
    if re.search(r'\b(API|endpoint|REST|MCP)\b', content):
        tags.add("api")

    return tags


# ============================================================
# PROMPT INJECTION DEFENSE — Sanitize stored thoughts for AI clients
# ============================================================
# When AI tools read thoughts back from the DB, malicious content could
# trick the AI into following injected instructions. We wrap returned
# content so the AI knows it's USER DATA, not system instructions.

# Patterns that look like prompt injection attempts
SUSPICIOUS_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"system\s+override", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+.+mode", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior|above)", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
]

def sanitize_for_ai(content):
    """Wrap thought content so AI clients treat it as data, not instructions.

    Checks for suspicious prompt injection patterns and flags them.
    Always wraps content in a data boundary marker regardless.
    """
    flagged = any(p.search(content) for p in SUSPICIOUS_PATTERNS)
    prefix = "[SUSPICIOUS - possible injection] " if flagged else ""
    return f"{prefix}[USER STORED NOTE]: {content}"


def sanitize_results(results):
    """Apply sanitize_for_ai to the 'content' field of each result dict.
    Modifies results in-place for efficiency."""
    for r in results:
        if "content" in r:
            r["content"] = sanitize_for_ai(r["content"])
    return results


# ============================================================
# RATE LIMITER — Prevent API abuse on REST endpoints
# ============================================================
# Simple in-memory sliding window. Tracks requests per IP per minute.
# Protects the Pi from being overwhelmed by rapid-fire API calls
# (each capture triggers an embedding generation at ~0.16s).

_rate_limits = defaultdict(list)
RATE_LIMIT_PER_MINUTE = 30  # Max requests per minute per IP

def check_rate_limit(request):
    """Check if the request IP has exceeded the rate limit.
    Returns None if OK, or a JSONResponse 429 if rate limited."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    # Prune entries older than 60 seconds
    _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < 60]
    if len(_rate_limits[ip]) >= RATE_LIMIT_PER_MINUTE:
        return JSONResponse(
            {"status": "error", "error": "Rate limit exceeded. Max 30 requests per minute."},
            status_code=429
        )
    _rate_limits[ip].append(now)
    return None


# --- Initialize ---
# Create the MCP server instance. FastMCP auto-generates tool schemas
# from Python type hints and docstrings.
# IMPORTANT: We pass host="0.0.0.0" here because FastMCP's default is
# "127.0.0.1" which auto-enables DNS rebinding protection — blocking
# all non-localhost requests with 421 "Invalid Host header". Since our
# server is accessed over LAN, we need host="0.0.0.0" to
# disable that auto-protection.
mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)

# === ACTIVITY STREAM ===
# Real-time event feed for the constellation visualization.
# Every MCP tool call gets logged here so the constellation can
# show agents thinking in real-time.
_activity_buffer = deque(maxlen=500)
_activity_subscribers = set()

# Track which agent is active — set by startup_bundle() and capture_thought()
# When an agent identifies itself, we remember it so search events get labeled correctly.
# Not perfect with concurrent agents, but works great for typical usage (one agent at a time).
_last_known_agent = "mcp"

# Track the current request's API key so MCP tool functions can pass it to the audit log.
# Set by the auth middleware before the tool runs, cleared after.
_current_agent_key = ""

def log_activity(tool_name, query="", thought_ids=None, source="mcp", result_count=0, event_type="search", agent_key=None, elapsed_ms=0.0):
    # Auto-read agent key from global if not explicitly passed
    if agent_key is None:
        agent_key = _current_agent_key
    # If no explicit source, use the last known agent identity
    if source == "mcp" and _last_known_agent != "mcp":
        source = _last_known_agent
    event = {
        "ts": datetime.now().isoformat(),
        "event": event_type,
        "tool": tool_name,
        "query": query[:200] if query else "",
        "thought_ids": (thought_ids or [])[:50],
        "source": source,
        "results": result_count,
    }
    _activity_buffer.append(event)
    for q in list(_activity_subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            pass

    # --- AUDIT LOG --- (Edge 4: full replay tape of agent activity)
    # Every constellation event also gets written to the daily CSV audit log.
    # This is the permanent record — constellation events are ephemeral (in-memory),
    # but audit CSV files persist on disk forever.
    audit.log_action(
        agent_name=source,
        agent_key=agent_key,
        action=tool_name,
        detail=query[:200] if query else "",
        thought_ids=(thought_ids or [])[:50],
        result_count=result_count,
        response_ms=elapsed_ms,
        source="mcp" if event_type != "rest" else "rest-api",
    )


# Initialize database tables on import
db.init_db()
agent_keys.init_agent_keys_table()  # Per-agent API keys with granular permissions
auth.init_auth_tables()  # Dashboard login/session/seed tables
audit.init_audit()  # CSV audit log — daily files in {DATA_DIR}/audit/

# Write queue — serializes all DB writes through a single thread.
# Prevents "database is locked" when multiple agents write simultaneously.
# Reads bypass this entirely (SQLite WAL handles concurrent readers).
_wq = WriteQueue(timeout=60)


# ============================================================
# MCP TOOLS — These are what Claude Code / Codex see and call
# ============================================================

@mcp.tool()
def capture_thought(
    content: str,
    thought_type: str = "thought",
    tags: list[str] = None,
    people: list[str] = None,
    source: str = "manual",
    force: bool = False,
    machine: str = "unknown",
    trigger: str = "unknown",
    status: str = "none",
    priority: int = 0,
) -> str:
    """Store a new thought/memory in the STRATA database.

    Every thought gets:
    - Stored as text in SQLite
    - Converted to a 768-dim vector embedding (captures meaning)
    - Indexed for full-text search
    - Tagged with optional metadata (type, tags, people, source)

    DEDUPLICATION: Before saving, checks if a very similar thought already
    exists (cosine similarity > 0.85). If found, returns a warning with the
    existing thought IDs instead of saving. Use force=True to save anyway,
    or use update_thought to modify the existing one instead.

    Args:
        content: The thought/memory text to store. Can be anything:
                 an idea, a decision, a person note, a project update.
        thought_type: Category — one of: thought, decision, session,
                      person, insight, project, instruction, reference
        tags: Optional list of tags for filtering (e.g. ["carpi", "hardware"])
        people: Optional list of people mentioned (e.g. ["Chris", "Sarah"])
        source: Where this came from — claude-code, codex, telegram, manual, migration
        force: Set to True to skip dedup check and save even if similar thoughts exist
        machine: Which device uploaded this — surface, helios, telegram, pi-nas, etc.
        trigger: How capture was initiated — auto (Claude decided), requested (Chris asked), manual (typed directly)
        status: Lightweight task state (none, open, in_progress, done)
        priority: Lightweight task priority 0-5 (0=unset)

    Returns:
        Confirmation message with the thought ID, or dedup warning
    """
    _t0 = time.monotonic()  # Start timing for audit log

    # Track which agent is capturing — used to label SSE events
    global _last_known_agent
    if source and source not in ("manual", "unknown", "mcp", "pre-strata"):
        _last_known_agent = source

    # Validate type
    if thought_type not in VALID_TYPES:
        thought_type = "thought"

    # SAFETY: Never mutate shared default args — always work on fresh copies.
    # Python's mutable default (list=[]) is shared across all calls, so
    # without this copy, tags from previous calls would bleed into new ones.
    tags = list(tags) if tags else []
    people = list(people) if people else []

    # INPUT LIMITS: Prevent oversized payloads from crashing the Pi.
    # Large files should be stored on disk and referenced by path, not inlined.
    if len(content) > MAX_CONTENT_LENGTH:
        return f"Content too long ({len(content)} chars, max {MAX_CONTENT_LENGTH}). Store large files on disk and reference by path."
    tags = [t[:MAX_TAG_LENGTH] for t in tags[:MAX_TAGS] if isinstance(t, str)]
    people = [p[:MAX_PERSON_LENGTH] for p in people[:MAX_PEOPLE] if isinstance(p, str)]
    status = normalize_task_status(status)
    priority = normalize_priority(priority)

    # AUTO-TAGGING: Extract potential tags from content via regex
    # These get merged with user-provided tags — manual tags always take priority.
    # This adds tags the user might forget (e.g., #hashtags, crypto mentions, domains)
    auto_tags = auto_extract_tags(content)
    existing_lower = {t.lower() for t in tags}
    # Only add auto-tags that aren't already in the user's list (avoid duplicates)
    for at in auto_tags:
        if at not in existing_lower:
            tags.append(at)
            existing_lower.add(at)

    # Generate the embedding — this is where the meaning gets captured
    # Takes ~2-3 seconds on Pi, model loads on-demand if not already in memory
    embedding = embedder.embed_text(content)

    # DEDUP CHECK — look for near-duplicates before saving
    # Skip this check if force=True (caller knows what they're doing)
    if not force:
        duplicates = db.find_duplicates(embedding, threshold=DEDUP_THRESHOLD)
        if duplicates:
            # Build a warning message showing the similar existing thoughts
            warning = f"DUPLICATE WARNING: Found {len(duplicates)} similar thought(s):\n"
            for dupe in duplicates[:3]:  # Show top 3 matches max
                warning += f"  - Thought #{dupe['id']} ({dupe['similarity']:.0%} similar): {dupe['preview']}\n"
            warning += "\nTo save anyway, call capture_thought again with force=True."
            warning += "\nTo update the existing thought instead, use update_thought(thought_id=...)."
            return warning

    # Store in database
    thought_id = _wq.submit(
        db.store_thought,
        content=content,
        embedding=embedding,
        thought_type=thought_type,
        tags=tags,
        people=people,
        source=source,
        machine=machine,
        trigger=trigger,
        status=status,
        priority=priority,
    )

    log_activity("capture_thought", thought_ids=[thought_id], source=source, result_count=1, event_type="capture",
                 detail=content[:200], elapsed_ms=(time.monotonic() - _t0) * 1000)
    return f"Stored thought #{thought_id} (type={thought_type}, status={status}, priority={priority}, tags={tags}, machine={machine}, trigger={trigger})"


@mcp.tool()
def capture_legacy(
    content: str,
    thought_type: str = "thought",
    tags: list[str] = None,
    people: list[str] = None,
    source: str = "pre-strata",
    original_date: str = "",
    force: bool = False,
    machine: str = "unknown",
    trigger: str = "manual",
    status: str = "none",
    priority: int = 0,
) -> str:
    """Store a PRE-STRATA historical thought with a negative ID.

    Use this to import old projects, notes, and files that existed before
    Strata was built. Creates a clean namespace separation:
      - Positive IDs (1, 2, 3...) = live Strata thoughts (real-time)
      - Negative IDs (-1, -2, -3...) = historical imports (pre-Strata)

    The negative numbering extends infinitely backward. Your forward
    numbering (positive IDs) is completely untouched.

    Legacy thoughts are fully searchable via semantic/vector search.
    They skip FTS5 indexing (SQLite limitation with negative rowids)
    so they won't appear in hybrid keyword search — minor tradeoff.

    Args:
        content: The historical thought/memory text to store.
        thought_type: Category — one of: thought, decision, session,
                      person, insight, project, instruction, reference
        tags: Optional list of tags. "legacy" is auto-added to all imports.
        people: Optional list of people mentioned
        source: Defaults to "pre-strata". Use this to identify import batches
                (e.g. "pre-strata-drivescan", "pre-strata-nas")
        original_date: When this ACTUALLY happened (e.g. "2024-06-15" or
                       "2025-01-20 14:30:00"). Stored separately from
                       created_at (which is always the import timestamp).
                       Leave empty if the date is unknown.
        force: Set True to skip dedup check (useful for bulk imports)
        machine: Which device this originally came from (surface, helios, etc.)
        trigger: How capture was initiated (usually "manual" for imports)
        status: Lightweight task state (none, open, in_progress, done)
        priority: Lightweight task priority 0-5 (0=unset)

    Returns:
        Confirmation message with the negative thought ID
    """
    # Validate type — fall back to "thought" if unrecognized
    if thought_type not in VALID_TYPES:
        thought_type = "thought"

    # SAFETY: Fresh copies of mutable defaults
    tags = list(tags) if tags else []
    people = list(people) if people else []

    # INPUT LIMITS: Same guardrails as capture_thought
    if len(content) > MAX_CONTENT_LENGTH:
        return f"Content too long ({len(content)} chars, max {MAX_CONTENT_LENGTH}). Store large files on disk and reference by path."
    tags = [t[:MAX_TAG_LENGTH] for t in tags[:MAX_TAGS] if isinstance(t, str)]
    people = [p[:MAX_PERSON_LENGTH] for p in people[:MAX_PEOPLE] if isinstance(p, str)]
    status = normalize_task_status(status)
    priority = normalize_priority(priority)

    # AUTO-TAG "legacy" — every pre-Strata import gets this tag automatically
    # so you can always find all legacy imports with search_by_tag("legacy")
    existing_lower = {t.lower() for t in tags}
    if "legacy" not in existing_lower:
        tags.append("legacy")
        existing_lower.add("legacy")

    # Auto-extract additional tags from content (same as capture_thought)
    auto_tags = auto_extract_tags(content)
    for at in auto_tags:
        if at not in existing_lower:
            tags.append(at)
            existing_lower.add(at)

    # Generate the embedding for semantic search
    embedding = embedder.embed_text(content)

    # DEDUP CHECK — same logic as capture_thought, skip if force=True
    if not force:
        duplicates = db.find_duplicates(embedding, threshold=DEDUP_THRESHOLD)
        if duplicates:
            warning = f"DUPLICATE WARNING: Found {len(duplicates)} similar thought(s):\n"
            for dupe in duplicates[:3]:
                warning += f"  - Thought #{dupe['id']} ({dupe['similarity']:.0%} similar): {dupe['preview']}\n"
            warning += "\nTo save anyway, call capture_legacy again with force=True."
            warning += "\nTo update the existing thought instead, use update_thought(thought_id=...)."
            return warning

    # Parse original_date — validate if provided, None if empty
    parsed_date = None
    if original_date and original_date.strip():
        try:
            # Try ISO format first (2024-06-15 or 2024-06-15 14:30:00)
            parsed_date = datetime.fromisoformat(original_date.strip()).isoformat()
        except ValueError:
            return f"Invalid original_date format: '{original_date}'. Use ISO format like '2024-06-15' or '2024-06-15 14:30:00'."

    # Store with a negative ID — this is the key difference from capture_thought
    thought_id = _wq.submit(
        db.store_legacy_thought,
        content=content,
        embedding=embedding,
        thought_type=thought_type,
        tags=tags,
        people=people,
        source=source,
        original_date=parsed_date,
        machine=machine,
        trigger=trigger,
        status=status,
        priority=priority,
    )

    date_info = f", original_date={parsed_date}" if parsed_date else ""
    return f"Stored legacy thought #{thought_id} (type={thought_type}, tags={tags}, source={source}{date_info})"


@mcp.tool()
def semantic_search(query: str, limit: int = 10, threshold: float = 0.0, source: str = "mcp") -> str:
    """Search memories by MEANING, not just keywords.

    This is the core power of STRATA. When you search for
    "career change", it will find thoughts about "switching jobs"
    or "moving into consulting" even if those exact words weren't used.

    The embedding model converts your query into the same 768-dim
    vector space as stored thoughts, then finds the closest matches
    by cosine distance.

    Args:
        query: What you're looking for, in natural language.
               E.g. "What was I thinking about the options bot last week?"
        limit: Max results to return (default 10)
        threshold: Minimum similarity score to include (0.0-1.0).
                   0.0 = return everything, 0.5 = moderate match,
                   0.7 = strong match only. Default 0.0 (no filter).

    Returns:
        JSON string of matching thoughts, ranked by similarity
    """
    if limit < 1:
        limit = DEFAULT_SEARCH_LIMIT

    _t0 = time.monotonic()

    # Embed the search query into the same vector space as stored thoughts
    query_embedding = embedder.embed_text(query)

    # Find the closest matches by cosine distance, filtered by threshold
    results = db.search_similar(query_embedding, limit=limit, threshold=threshold)

    _elapsed = (time.monotonic() - _t0) * 1000

    if not results:
        log_activity("semantic_search", query=query, result_count=0, source=source, elapsed_ms=_elapsed)
        return "No matching thoughts found."

    # Track which thoughts got accessed — builds the "heat map" over time
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    log_activity("semantic_search", query=query, thought_ids=[r["id"] for r in results], result_count=len(results), source=source, elapsed_ms=_elapsed)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def list_recent(limit: int = 20, hours: int = 168, source: str = "mcp") -> str:
    """Browse the most recent thoughts/memories within a time window.

    Good for questions like "what was I capturing this week?" or
    "show me my last 5 thoughts". Default window is 7 days.

    Args:
        limit: Max results to return (default 20)
        hours: Look back this many hours (default 168 = 7 days)

    Returns:
        JSON string of recent thoughts, newest first
    """
    _t0 = time.monotonic()
    if limit < 1:
        limit = DEFAULT_RECENT_LIMIT
    if hours < 1:
        hours = DEFAULT_RECENT_HOURS

    results = db.list_recent(limit=limit, hours=hours)

    if not results:
        return "No thoughts captured in the last %d hours." % hours

    sanitize_results(results)
    log_activity("list_recent", result_count=len(results), thought_ids=[r["id"] for r in results[:10]], source=source, elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def get_stats() -> str:
    """Get statistics about the STRATA database.

    Shows total thoughts, breakdown by type and source,
    top tags and people mentioned, and database size.
    Useful for understanding what's in the brain at a glance.

    Returns:
        JSON string with database statistics
    """
    stats = db.get_stats()
    return json.dumps(stats, indent=2)


@mcp.tool()
def strata_status() -> str:
    """Return Strata's identity, status, and the usage protocol AI agents should follow.

    AI clients should call this ONCE at the start of every session. It returns a
    short natural-language protocol that tells the agent when to capture, when to
    search, how to interpret negative IDs, what the ten thought types mean, and
    how to behave if the user references earlier sessions. After reading the
    protocol, the agent should start using Strata proactively without waiting
    for explicit instructions.

    The protocol text is admin-editable via system_config['agent_protocol'] —
    every deployed Strata instance can customize the tone, policies, and
    instance-specific guidance by updating that row. This function returns
    whatever text is currently stored, falling back to a sensible default if
    no override is set.

    Returns:
        A human + agent-readable status/protocol block.
    """
    stats = db.get_stats()
    protocol = _load_agent_protocol()
    lines = [
        "STRATA — persistent memory for AI assistants",
        f"Total thoughts: {stats.get('total_thoughts', 0)}",
        f"Database size: {stats.get('db_size_mb', 0)} MB",
        f"MCP access: {'ENABLED' if _mcp_global_enabled else 'DISABLED (kill switch active)'}",
        "",
        "=== AGENT PROTOCOL ===",
        protocol,
    ]
    return "\n".join(lines)


@mcp.tool()
def update_thought(
    thought_id: int,
    content: str = None,
    thought_type: str = None,
    tags: list[str] = None,
    people: list[str] = None,
    status: str = None,
    priority: int = None,
) -> str:
    """Update an existing thought in the STRATA database.

    Only the fields you provide will be changed — everything else
    stays the same. If you change the content, the embedding is
    automatically regenerated so semantic search stays accurate.

    Args:
        thought_id: The ID of the thought to update (required)
        content: New text content (leave empty to keep existing)
        thought_type: New category (thought, decision, session, etc.)
        tags: New tags list (replaces all existing tags)
        people: New people list (replaces all existing people)
        status: New task status (none, open, in_progress, done)
        priority: New task priority 0-5

    Returns:
        Confirmation message or error if thought not found
    """
    _t0 = time.monotonic()
    # Validate type if provided
    if thought_type is not None and thought_type not in VALID_TYPES:
        return f"Invalid type '{thought_type}'. Valid types: {', '.join(VALID_TYPES)}"
    if status is not None:
        status = normalize_task_status(status)
    if priority is not None:
        priority = normalize_priority(priority)

    # If content is changing, we need a new embedding to keep search accurate
    new_embedding = None
    if content is not None:
        new_embedding = embedder.embed_text(content)

    success = _wq.submit(
        db.update_thought,
        thought_id=thought_id,
        content=content,
        thought_type=thought_type,
        tags=tags,
        people=people,
        status=status,
        priority=priority,
        new_embedding=new_embedding,
    )

    if not success:
        return f"Thought #{thought_id} not found."

    # Build a summary of what changed
    changed = []
    if content is not None:
        changed.append("content (re-embedded)")
    if thought_type is not None:
        changed.append(f"type → {thought_type}")
    if tags is not None:
        changed.append(f"tags → {tags}")
    if people is not None:
        changed.append(f"people → {people}")
    if status is not None:
        changed.append(f"status → {status}")
    if priority is not None:
        changed.append(f"priority → {priority}")

    log_activity("update_thought", thought_ids=[thought_id], detail=', '.join(changed),
                 result_count=1, event_type="update", elapsed_ms=(time.monotonic() - _t0) * 1000)
    return f"Updated thought #{thought_id}: {', '.join(changed)}"


@mcp.tool()
def delete_thought(thought_id: int, admin_key: str = "") -> str:
    """Permanently delete a thought from the STRATA database.

    ADMIN ONLY — requires the admin_key to execute. AI agents cannot
    delete thoughts on their own. The human owner must provide the key
    and explicitly instruct deletion. Rate limited to 1 delete per hour
    to prevent runaway loops from wiping the database.

    Args:
        thought_id: The ID of the thought to delete
        admin_key: The admin key (required — ask the human owner for it)

    Returns:
        Confirmation message or error if thought not found
    """
    global _last_delete_time

    # ADMIN GATE — AI cannot delete without human providing the key
    if not ADMIN_KEY:
        return "DELETE BLOCKED: Admin key not configured. Set STRATA_ADMIN_KEY environment variable."
    if not admin_key or not hmac.compare_digest(str(admin_key), ADMIN_KEY):
        return "DELETE BLOCKED: Invalid or missing admin_key. Only the human owner can authorize deletions."

    # RATE LIMIT — max 1 deletion per hour, even with the correct key.
    # If a prompt injection somehow gets the admin key, it can only delete
    # 1 thought before hitting the wall. The nightly backup covers the rest.
    import time
    now = time.time()
    elapsed = now - _last_delete_time
    if elapsed < DELETE_COOLDOWN:
        remaining = int(DELETE_COOLDOWN - elapsed)
        mins = remaining // 60
        secs = remaining % 60
        return f"DELETE RATE LIMITED: Only 1 deletion per hour allowed. Try again in {mins}m {secs}s."

    # Fetch the thought so we can show what was deleted
    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return f"Thought #{thought_id} not found."

    preview = thought["content"][:100]
    if len(thought["content"]) > 100:
        preview += "..."

    success = _wq.submit(db.delete_thought, thought_id)
    if not success:
        return f"Failed to delete thought #{thought_id}."

    # Update rate limiter timestamp AFTER successful deletion
    _last_delete_time = now

    # AUDIT: deletions are the most critical action to log — who deleted what, when
    log_activity("delete_thought", thought_ids=[thought_id], detail=f"DELETED: {preview}",
                 result_count=1, event_type="delete")
    return f"Deleted thought #{thought_id}: \"{preview}\""


@mcp.tool()
def get_thought(thought_id: int) -> str:
    """Retrieve a single thought by its ID.

    Useful for inspecting a thought before updating or deleting it,
    or for viewing the full content of a search result.

    Args:
        thought_id: The ID of the thought to retrieve

    Returns:
        JSON string with full thought details, or error if not found
    """
    _t0 = time.monotonic()
    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return f"Thought #{thought_id} not found."

    # Track that this specific thought was accessed
    _wq.submit_fire_and_forget(db.record_access, [thought_id])

    # Sanitize single thought — wrap content for AI safety
    if "content" in thought:
        thought["content"] = sanitize_for_ai(thought["content"])
    log_activity("get_thought", thought_ids=[thought_id], event_type="access", elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(thought, indent=2, default=str)


@mcp.tool()
def search_by_tag(tag: str, limit: int = 20, source: str = "mcp") -> str:
    """Find all memories tagged with a specific tag.

    Tags are set when thoughts are captured. Common tags might be
    project names (carpi, options-bot, receipt-vault), topics
    (hardware, trading, tax), or custom labels.

    Args:
        tag: The tag to search for (case-insensitive)
        limit: Max results to return (default 20)

    Returns:
        JSON string of matching thoughts
    """
    _t0 = time.monotonic()
    results = db.search_by_tag(tag, limit=limit)

    if not results:
        return f"No thoughts found with tag '{tag}'."

    # Track access for returned thoughts
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    log_activity("search_by_tag", query=tag, result_count=len(results), thought_ids=[r["id"] for r in results[:10]], source=source, elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def search_by_person(person: str, limit: int = 20, source: str = "mcp") -> str:
    """Find all memories that mention a specific person.

    Searches the people field with case-insensitive partial matching.
    "chris" will find thoughts tagged with "Chris Mitchell".

    Args:
        person: Name to search for (partial match, case-insensitive)
        limit: Max results to return (default 20)

    Returns:
        JSON string of matching thoughts
    """
    _t0 = time.monotonic()
    results = db.search_by_person(person, limit=limit)

    if not results:
        return f"No thoughts found mentioning '{person}'."

    # Track access for returned thoughts
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    log_activity("search_by_person", query=person, result_count=len(results), thought_ids=[r["id"] for r in results[:10]], source=source, elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def get_relevant_context(topic: str, limit: int = 10, source: str = "mcp") -> str:
    """Get a curated context bundle for a topic — the smart search.

    Unlike raw semantic_search, this tool:
    1. Searches for the topic semantically
    2. Removes near-duplicate results (>0.90 similarity to each other)
    3. Groups results by type (decisions first, then insights, then projects, etc.)
    4. Returns a clean, ready-to-use context bundle

    This is the tool to use when starting work on a project or topic
    and you want everything relevant without duplicates or noise.

    Args:
        topic: What you need context about, in natural language.
               E.g. "CarPi HUD project" or "Amazon Return Tracker security"
        limit: Max results to return after dedup (default 10)

    Returns:
        JSON string with grouped, deduplicated results
    """
    _t0 = time.monotonic()
    if limit < 1:
        limit = 10

    # Fetch more than we need — we'll trim after dedup
    query_embedding = embedder.embed_text(topic)
    raw_results = db.search_similar(query_embedding, limit=limit * 2)

    if not raw_results:
        return f"No context found for '{topic}'."

    # DEDUP PASS — remove results that are too similar to each other
    # This catches the case where a README and its migrated memory entry
    # both show up as separate results with nearly identical content
    deduped = []
    for result in raw_results:
        is_dupe = False
        for kept in deduped:
            # Quick content-length-based heuristic: if two results share
            # the first 200 chars, they're probably the same document
            if (result["content"][:200] == kept["content"][:200]):
                is_dupe = True
                break
        if not is_dupe:
            deduped.append(result)

    # Trim to requested limit
    deduped = deduped[:limit]

    # GROUP BY TYPE — decisions and insights first (most actionable),
    # then projects, then sessions, then everything else
    type_priority = {
        "decision": 0, "insight": 1, "instruction": 2,
        "project": 3, "reference": 4, "session": 5,
        "person": 6, "thought": 7,
    }
    deduped.sort(key=lambda r: (type_priority.get(r["type"], 99), -r["similarity"]))

    # Track access
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in deduped])

    # Build the response — include a summary header
    sanitize_results(deduped)
    response = {
        "topic": topic,
        "total_found": len(raw_results),
        "after_dedup": len(deduped),
        "results": deduped,
    }

    log_activity("get_relevant_context", query=topic, result_count=response.get("after_dedup", 0), thought_ids=[r["id"] for r in response.get("results", [])], source=source, elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(response, indent=2, default=str)


# ============================================================
# NEW TOOLS — Added for MINDVAULT v1.0
# ============================================================

@mcp.tool()
def find_related(thought_id: int, limit: int = 5) -> str:
    """Find thoughts similar to an existing thought — "more like this."

    Instead of searching by text, this takes a thought you already have
    and finds its nearest neighbors by vector similarity. No embedding
    generation needed — uses the thought's stored vector directly.

    Great for exploring connections: "I liked this idea, what else
    is related?" or "This decision connects to what other decisions?"

    Args:
        thought_id: The ID of the thought to find relatives for
        limit: Max results to return (default 5)

    Returns:
        JSON string of similar thoughts (excluding the source thought)
    """
    _t0 = time.monotonic()
    # First verify the thought exists
    source = db.get_thought_by_id(thought_id)
    if not source:
        return f"Thought #{thought_id} not found."

    results = db.find_related_by_id(thought_id, limit=limit)

    if results is None:
        return f"Thought #{thought_id} has no embedding (corrupted entry?)."

    if not results:
        return f"No related thoughts found for #{thought_id}."

    # Track access for the source and all related thoughts
    _wq.submit_fire_and_forget(db.record_access, [thought_id] + [r["id"] for r in results])

    sanitize_results(results)
    response = {
        "source_thought": {
            "id": source["id"],
            "content": sanitize_for_ai(source["content"][:200]),
            "type": source["type"],
        },
        "related": results,
    }
    log_activity("find_related", thought_ids=[thought_id] + [r["id"] for r in results[:10]], result_count=len(results), elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(response, indent=2, default=str)


@mcp.tool()
def hybrid_search(query: str, limit: int = 10, keyword_weight: float = 0.3, threshold: float = 0.0, source: str = "mcp") -> str:
    """Blended search combining keyword matching AND semantic meaning.

    Uses both FTS5 (BM25 keyword scoring) and vector cosine similarity,
    then blends the scores. A search for "CarPi HUD" will boost results
    that literally contain those words AND find semantically related
    thoughts about car dashboards.

    Best for specific technical queries where exact terminology matters
    alongside conceptual understanding.

    Args:
        query: What to search for (used for both keyword and semantic matching)
        limit: Max results to return (default 10)
        keyword_weight: How much to weight keyword matches vs semantic (0.0-1.0).
                        0.3 = 30% keyword + 70% semantic (default, good balance).
                        0.5 = equal weight. 0.7 = keyword-heavy.
        threshold: Minimum blended score to include (default 0.0)

    Returns:
        JSON string of results with blended scores and match_type indicator
    """
    _t0 = time.monotonic()
    if limit < 1:
        limit = DEFAULT_SEARCH_LIMIT

    # Generate embedding for the semantic half of the search
    query_embedding = embedder.embed_text(query)

    # Run the blended search — keyword scores from FTS5, vector scores from numpy
    results = db.hybrid_search(
        query_text=query,
        query_embedding=query_embedding,
        limit=limit,
        keyword_weight=keyword_weight,
        threshold=threshold,
    )

    if not results:
        return "No matching thoughts found."

    # Track access
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    log_activity("hybrid_search", query=query, thought_ids=[r.get("id") for r in response.get("results", [])], result_count=len(response.get("results", [])), source=source, elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def search_advanced(
    tag: str = "",
    person: str = "",
    thought_type: str = "",
    source: str = "",
    machine: str = "",
    status: str = "",
    priority_min: int = None,
    priority_max: int = None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 20,
) -> str:
    """Multi-filter search — combine any filters in one query.

    Unlike semantic_search (which searches by meaning) or search_by_tag
    (which filters by one tag), this tool lets you stack multiple filters
    together: "show me all 'decision' types tagged 'bitcoin' from the
    surface machine in the last month."

    All filters are optional. Only the ones you provide are applied.

    Args:
        tag: Filter by tag (case-insensitive exact match)
        person: Filter by person mentioned (case-insensitive partial match)
        thought_type: Filter by type (thought, decision, insight, project, etc.)
        source: Filter by source (claude-code, telegram, manual, etc.)
        machine: Filter by machine (surface, helios, pi-nas, etc.)
        status: Filter by task status (none, open, in_progress, done)
        priority_min: Minimum priority (0-5)
        priority_max: Maximum priority (0-5)
        date_from: Only thoughts created on or after this date (ISO format: YYYY-MM-DD)
        date_to: Only thoughts created on or before this date (ISO format: YYYY-MM-DD)
        limit: Max results to return (default 20)

    Returns:
        JSON string of matching thoughts, newest first
    """
    _t0 = time.monotonic()
    # Build the filters dict — only include non-empty values
    filters = {}
    if tag:
        filters["tag"] = tag
    if person:
        filters["person"] = person
    if thought_type:
        filters["type"] = thought_type
    if source:
        filters["source"] = source
    if machine:
        filters["machine"] = machine
    if status:
        filters["status"] = normalize_task_status(status)
    if priority_min is not None:
        filters["priority_min"] = normalize_priority(priority_min)
    if priority_max is not None:
        filters["priority_max"] = normalize_priority(priority_max)
    if date_from:
        filters["date_from"] = date_from
    if date_to:
        filters["date_to"] = date_to

    if not filters:
        return "At least one filter is required. Provide tag, person, type, source, machine, status, priority_min/max, date_from, or date_to."

    results = db.search_advanced(filters, limit=limit)

    if not results:
        return f"No thoughts found matching filters: {filters}"

    # Track access
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    log_activity("search_advanced", result_count=len(results), thought_ids=[r["id"] for r in results[:10]], source=caller, elapsed_ms=(time.monotonic() - _t0) * 1000)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def generate_report(days: int = 7) -> str:
    """Generate a trend report for your memory activity.

    Compares the last N days against the previous N days to show:
    - How many thoughts were captured (and the change %)
    - Rising tags (topics you're thinking about MORE)
    - Declining tags (topics fading from focus)
    - Activity breakdown by machine and source
    - Your hottest memories (most frequently accessed)

    IMPORTANT: This is purely analytical. Nothing gets archived,
    decayed, or deleted. Every memory is permanent and valuable.
    Your smallest thought might be your biggest breakthrough.

    Args:
        days: Number of days for the current period (default 7).
              The previous period is the same length, ending where
              the current period starts.

    Returns:
        JSON string with the full trend report
    """
    if days < 1:
        days = 7

    report = db.generate_report(days=days)
    return json.dumps(report, indent=2, default=str)


@mcp.tool()
def startup_bundle(
    agent_name: str = "codex",
    recent_limit: int = 10,
    project_limit: int = 5,
    blocker_limit: int = 5,
    digest_days: int = 1,
) -> str:
    """Get a startup context bundle in one call.

    Returns recent thoughts, active projects, open blockers,
    the latest digest summary, and optional agent profile defaults.
    """
    if recent_limit < 1:
        recent_limit = 10
    if project_limit < 1:
        project_limit = 5
    if blocker_limit < 1:
        blocker_limit = 5
    if digest_days < 1:
        digest_days = 1

    global _last_known_agent
    _last_known_agent = agent_name.strip().lower()
    bundle = db.get_startup_bundle(
        recent_limit=recent_limit,
        project_limit=project_limit,
        blocker_limit=blocker_limit,
        digest_days=digest_days,
        agent_name=agent_name,
    )

    # Mark startup bundle records as accessed so heat maps reflect real usage.
    touched_ids = []
    for key in ("recent_thoughts", "active_projects", "open_blockers"):
        touched_ids.extend([item["id"] for item in bundle.get(key, []) if "id" in item])
    if touched_ids:
        _wq.submit_fire_and_forget(db.record_access, sorted(set(touched_ids)))

    sanitize_results(bundle.get("recent_thoughts", []))
    sanitize_results(bundle.get("active_projects", []))
    sanitize_results(bundle.get("open_blockers", []))
    sanitize_results(bundle.get("change_digest", {}).get("items", []))
    return json.dumps(bundle, indent=2, default=str)


@mcp.tool()
def set_agent_profile(
    agent_name: str,
    startup_mode: str = "standard",
    instructions: str = "",
    metadata: dict = None,
) -> str:
    """Create or update deterministic startup defaults for one agent."""
    profile = _wq.submit(
        db.upsert_agent_profile,
        agent_name=agent_name.strip().lower(),
        startup_mode=startup_mode.strip().lower() if startup_mode else "standard",
        instructions=instructions,
        metadata=metadata or {},
    )
    return json.dumps(profile, indent=2, default=str)


@mcp.tool()
def get_agent_profile(agent_name: str) -> str:
    """Read the profile/defaults for one agent."""
    profile = db.get_agent_profile(agent_name.strip().lower())
    if not profile:
        return f"No profile found for agent '{agent_name}'."
    return json.dumps(profile, indent=2, default=str)


@mcp.tool()
def generate_change_digest(days: int = 1, limit: int = 20, auto_store: bool = False) -> str:
    """Generate a compact digest for recent changes.

    Set auto_store=True to persist the digest as a reference thought.
    """
    if days < 1:
        days = 1
    if limit < 1:
        limit = 20

    digest = db.generate_change_digest(days=days, limit=limit)
    sanitize_results(digest.get("items", []))

    if auto_store:
        summary = (
            f"Auto digest ({days}d): count={digest['count']}, "
            f"by_type={digest.get('by_type', {})}, by_status={digest.get('by_status', {})}"
        )
        digest_json = json.dumps(digest, default=str)
        if len(digest_json) > 4500:
            digest_json = digest_json[:4500] + "... [truncated]"
        content = f"{summary}\n\n{digest_json}"
        emb = embedder.embed_text(content)
        thought_id = _wq.submit(
            db.store_thought,
            content=content,
            embedding=emb,
            thought_type="reference",
            tags=["digest", "automation", f"{days}d"],
            people=[],
            source="strata",
            machine="pi-nas",
            trigger="auto",
            status="none",
            priority=0,
        )
        digest["stored_thought_id"] = thought_id

    return json.dumps(digest, indent=2, default=str)


# ============================================================
# TEMPORAL SEARCH — Time-based queries across your memory
# ============================================================
# Inspired by the "agent bridges time" concept from STRATA's
# extensions video. Your memory has a time dimension that basic
# semantic search doesn't exploit — "what did I decide last month?"
# or "show me everything from this week" are natural questions
# that need a dedicated tool.

@mcp.tool()
def temporal_search(
    date_from: str = "",
    date_to: str = "",
    thought_type: str = "",
    source: str = "",
    machine: str = "",
    query: str = "",
    limit: int = 20,
) -> str:
    """Search thoughts by time range with optional semantic filtering.

    This is the "agent bridges time" tool — find what happened during
    a specific period. Perfect for questions like:
    - "What decisions did I make last week?"
    - "Show me everything from March 2026"
    - "What did the nightly agent capture yesterday?"

    If you provide a query along with dates, results are ranked by
    semantic similarity within the time window. Without a query,
    results are sorted newest first.

    Args:
        date_from: Start date (ISO: YYYY-MM-DD). Empty = no lower bound.
        date_to: End date (ISO: YYYY-MM-DD). Empty = no upper bound.
        thought_type: Filter by type (decision, insight, project, etc.)
        source: Filter by source (claude-code, telegram, etc.)
        machine: Filter by machine (surface, helios, etc.)
        query: Optional semantic query to rank results by relevance
        limit: Max results (default 20)

    Returns:
        JSON string of matching thoughts within the time range
    """
    # Use the timerange search from db.py for the base results
    results = db.search_by_timerange(
        date_from=date_from or None,
        date_to=date_to or None,
        thought_type=thought_type or None,
        source=source or None,
        machine=machine or None,
        limit=limit * 2 if query else limit,  # Over-fetch if we're going to re-rank
    )

    # If a semantic query was provided, re-rank by similarity
    # This gives you "most relevant within this time window" instead
    # of just "newest within this time window"
    if query and results:
        import asyncio
        query_embedding = embedder.embed_text(query)
        query_vec = np.array(query_embedding, dtype=np.float32)
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)

        # Score each result by cosine similarity to the query
        scored = []
        for r in results:
            # Look up the embedding for this thought
            conn = db.get_db()
            row = conn.execute(
                "SELECT embedding FROM thought_embeddings WHERE thought_id = ?",
                (r["id"],)
            ).fetchone()
            conn.close()

            if row:
                import struct
                stored_vec = np.array(
                    struct.unpack(f"{db.EMBEDDING_DIM}f", row["embedding"]),
                    dtype=np.float32
                )
                stored_norm = stored_vec / (np.linalg.norm(stored_vec) + 1e-10)
                similarity = float(np.dot(query_norm, stored_norm))
                r["similarity"] = round(similarity, 4)
                scored.append(r)

        # Sort by similarity (highest first) and trim to requested limit
        scored.sort(key=lambda x: x.get("similarity", 0), reverse=True)
        results = scored[:limit]

    if not results:
        date_desc = ""
        if date_from:
            date_desc += f" from {date_from}"
        if date_to:
            date_desc += f" to {date_to}"
        return f"No thoughts found{date_desc}."

    # Track access so heat maps reflect real usage
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])
    sanitize_results(results)
    return json.dumps(results, indent=2, default=str)


# ============================================================
# AUDIT TRAIL — See what changed and when
# ============================================================
# Every ADD/UPDATE/DELETE is logged to thought_history.
# This gives you full memory archaeology — when was a thought
# created, how many times was it updated, what did it say before?
# Inspired by Mem0's history tracking and Engram's mutation journal.

@mcp.tool()
def get_history(
    thought_id: int = None,
    action: str = "",
    limit: int = 50,
) -> str:
    """View the audit trail of memory changes.

    Every time a thought is created, updated, or deleted, it gets
    logged here. Use this to see:
    - What changed recently across all of Strata
    - The full edit history of a specific thought
    - Who/what made changes (which agent, which machine)

    Args:
        thought_id: Get history for a specific thought (optional)
        action: Filter by action type: 'create', 'update', 'delete' (optional)
        limit: Max results (default 50)

    Returns:
        JSON string of history entries, newest first
    """
    history = db.get_thought_history(
        thought_id=thought_id,
        action=action or None,
        limit=limit,
    )

    if not history:
        if thought_id:
            return f"No history found for thought #{thought_id}."
        return "No history entries found."

    return json.dumps(history, indent=2, default=str)


# ============================================================
# AUTHENTICATION — API key check for REST endpoints
# ============================================================
# MCP transport (/mcp) handles its own auth. These REST endpoints
# (/api/capture, /api/search, /log-conversation) need protection
# so random devices on the network can't read/write your brain.
#
# Send the key as a header:   X-API-Key: your-key-here
# Or as a Bearer token:       Authorization: Bearer your-key-here

def check_auth(request, required_perm=None):
    """Verify API key from request headers. Supports:
    1. Human admin key (ADMIN_KEY) — always passes, full access
    2. Per-agent keys from agent_keys table — checked for enabled + permissions

    Args:
        request: The incoming HTTP request
        required_perm: None for any access, or 'read'/'write'/'delete' for specific permission

    Returns None if auth passes, or a JSONResponse 401/403 if it fails.
    """
    if not AUTH_ENABLED:
        return None  # Auth disabled — let everything through

    # Extract key from headers (X-API-Key or Authorization: Bearer)
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            api_key = auth_header[7:]

    if not api_key:
        return JSONResponse(
            {"status": "error", "error": "Unauthorized. Provide X-API-Key header or Authorization: Bearer token."},
            status_code=401
        )

    # 1. Human admin key — always passes everything
    if ADMIN_KEY and hmac.compare_digest(str(api_key), ADMIN_KEY):
        return None

    # 2. Check per-agent keys table — each agent gets its own key with granular perms
    agent = agent_keys.get_agent_by_key(api_key)
    if agent:
        if not agent["enabled"]:
            return JSONResponse(
                {"status": "error", "error": "Agent key is disabled. Contact admin."},
                status_code=403
            )
        if required_perm == "read" and not agent["can_read"]:
            return JSONResponse(
                {"status": "error", "error": "Agent does not have read permission."},
                status_code=403
            )
        if required_perm == "write" and not agent["can_write"]:
            return JSONResponse(
                {"status": "error", "error": "Agent does not have write permission."},
                status_code=403
            )
        if required_perm == "delete" and not agent["can_delete"]:
            return JSONResponse(
                {"status": "error", "error": "Agent does not have delete permission."},
                status_code=403
            )
        return None  # Agent key valid + has required permission

    # Legacy key DISABLED — all agents must use registered per-agent keys.
    # No more free rides. Get a key from /admin/agents or get rejected.

    return JSONResponse(
        {"status": "error", "error": "Invalid API key. Register an agent key via /admin/agents."},
        status_code=401
    )


def check_admin_auth(request):
    """Verify admin access for agent key management and other protected endpoints.
    Accepts:
    1. Human admin key (ADMIN_KEY) via X-Admin-Key header — browser UI
    2. Human admin key via X-API-Key header — CLI/API
    3. Agent keys with can_admin=1 — toggled on by human from admin panel
    4. In DEMO MODE: valid dashboard session (so demos work without an admin key)
    No one else gets in. Period."""
    # Check X-Admin-Key header (human admin from browser)
    admin_key = request.headers.get('x-admin-key', '')
    if admin_key and ADMIN_KEY and hmac.compare_digest(str(admin_key), ADMIN_KEY):
        return None
    # Check X-API-Key — could be human admin key OR agent key with admin perms
    api_key = request.headers.get('x-api-key', '')
    if api_key:
        if ADMIN_KEY and hmac.compare_digest(str(api_key), ADMIN_KEY):
            return None
        agent = agent_keys.get_agent_by_key(api_key)
        if agent and agent["enabled"] and agent.get("can_admin", 0):
            return None
    # Demo mode: let anyone through — the whole point of demo mode is to let users
    # try the full product (create agents, toggle kill switch) without needing a real
    # admin key. This is ONLY active when STRATA_DEMO_MODE=true — production is unaffected.
    if DEMO_MODE:
        return None
    return JSONResponse(
        {'status': 'error', 'error': 'Admin access required. Use admin key or an agent key with admin permission.'},
        status_code=401
    )


# ============================================================
# HTTP ENDPOINTS — Health check, conversation logging, MCP
# ============================================================

# Where relay conversations get archived. Override with STRATA_CONVERSATIONS_DIR
# in the environment to point at a different volume (e.g. an external drive
# mounted somewhere your install actually has).
CONVERSATIONS_DIR = os.environ.get(
    "STRATA_CONVERSATIONS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "conversations"),
)

async def health_check(request):
    """Simple health endpoint for monitoring.
    Hit http://localhost:4320/health from any browser to check if it's alive."""
    stats = db.get_stats()
    return JSONResponse({
        "status": "ok",
        "server": SERVER_NAME,
        "model_loaded": embedder.is_loaded(),
        "total_thoughts": stats["total_thoughts"],
        "db_size_mb": stats["db_size_mb"],
        "timestamp": datetime.now().isoformat(),
        "write_queue": _wq.stats,
    })


async def log_conversation(request):
    """Archive a relay message to the NAS.

    The Helios relay (9090) forwards every message here after queuing.
    We save to two files:
    - conversations.jsonl  — machine-readable, one JSON object per line
    - chat_log.txt         — human-readable, timestamped conversation log

    No embeddings, no model loading — just file appends. Zero overhead.
    """
    # Auth check — only trusted sources should log conversations
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid JSON"}, status_code=400)

    # MEDIUM-03: Strip newlines from sender/recipient to prevent log injection.
    # A malicious sender field like "evil\n[2026-03-06] admin -> everyone" could
    # fake log entries. Stripping newlines makes that impossible.
    sender = data.get("from", "unknown").replace("\n", "").replace("\r", "")
    recipient = data.get("to", "unknown").replace("\n", "").replace("\r", "")
    message = data.get("message", "")
    timestamp = data.get("timestamp", datetime.now().strftime("%H:%M:%S"))
    seq = data.get("seq", 0)
    full_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Append to JSONL (machine-readable archive)
    jsonl_path = os.path.join(CONVERSATIONS_DIR, "conversations.jsonl")
    entry = {
        "from": sender,
        "to": recipient,
        "message": message,
        "timestamp": full_timestamp,
        "relay_timestamp": timestamp,
        "seq": seq,
    }
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    # 2. Append to chat log (human-readable)
    log_path = os.path.join(CONVERSATIONS_DIR, "chat_log.txt")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{full_timestamp}] {sender} -> {recipient}\n")
        # Indent the message body for readability
        for line in message.split("\n"):
            f.write(f"  {line}\n")
        f.write("\n")

    return JSONResponse({
        "status": "ok",
        "archived": True,
        "seq": seq,
    })


async def api_capture(request):
    """REST endpoint for quick thought capture from bots, scripts, and webhooks.

    This is the simple HTTP alternative to the MCP capture_thought tool.
    Any client that can POST JSON can capture a thought — no MCP session needed.
    The Telegram bot uses this so Chris can text ideas from his phone and
    have them embedded and stored instantly.

    POST /api/capture
    {
        "content": "my idea here",              (required)
        "type": "thought",                       (optional, default "thought")
        "tags": ["tag1", "tag2"],                (optional)
        "people": ["Chris"],                     (optional)
        "source": "telegram",                    (optional, default "api")
        "force": false                           (optional, skip dedup check)
    }

    Requires X-API-Key header or Authorization: Bearer token.
    """
    # Auth check — protect against unauthorized writes to your brain
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    # Rate limit — prevent embedding DoS (each capture takes ~0.16s on Pi)
    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    content = data.get("content", "").strip()
    if not content:
        return JSONResponse({"status": "error", "error": "Content is required"}, status_code=400)
    if len(content) > MAX_CONTENT_LENGTH:
        return JSONResponse({"status": "error", "error": f"Content too long ({len(content)} chars, max {MAX_CONTENT_LENGTH})"}, status_code=413)

    thought_type = data.get("type", "thought")
    if thought_type not in VALID_TYPES:
        thought_type = "thought"

    # Enforce limits on tags and people — truncate silently, don't reject
    tags = data.get("tags", [])
    tags = [t[:MAX_TAG_LENGTH] for t in tags[:MAX_TAGS] if isinstance(t, str)]
    people = data.get("people", [])
    people = [p[:MAX_PERSON_LENGTH] for p in people[:MAX_PEOPLE] if isinstance(p, str)]
    source = data.get("source", "api")
    force = data.get("force", False)
    machine = data.get("machine", "unknown")
    trigger = data.get("trigger", "manual")  # REST API calls are typically manual (bot/script)
    status = normalize_task_status(data.get("status", "none"))
    priority = normalize_priority(data.get("priority", 0))

    # Generate embedding — run in executor so it doesn't block the async event loop.
    # embed_text takes ~0.16s on Pi which would stall all other requests if run inline.
    loop = asyncio.get_event_loop()
    embedding_val = await loop.run_in_executor(None, embedder.embed_text, content)

    # Dedup check (unless forced)
    if not force:
        duplicates = db.find_duplicates(embedding_val, threshold=DEDUP_THRESHOLD)
        if duplicates:
            return JSONResponse({
                "status": "duplicate",
                "message": f"Found {len(duplicates)} similar thought(s)",
                "duplicates": duplicates[:3],
            }, status_code=409)

    # Store the thought — goes through write queue to prevent DB lock contention
    thought_id = _wq.submit(
        db.store_thought,
        content=content,
        embedding=embedding_val,
        thought_type=thought_type,
        tags=tags,
        people=people,
        source=source,
        machine=machine,
        trigger=trigger,
        status=status,
        priority=priority,
    )

    return JSONResponse({
        "status": "ok",
        "thought_id": thought_id,
        "type": thought_type,
        "tags": tags,
        "source": source,
        "machine": machine,
        "trigger": trigger,
        "status": status,
        "priority": priority,
    })


async def api_search(request):
    """REST endpoint for quick semantic search from bots and scripts.

    POST /api/search
    {
        "query": "what was the plan for CarPi",   (required)
        "limit": 5                                 (optional, default 5)
    }

    Requires X-API-Key header or Authorization: Bearer token.
    """
    # Auth check — protect against unauthorized reads of your brain
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    # Rate limit — prevent search spam (each query triggers embedding generation)
    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    # Support both GET (query params) and POST (JSON body)
    if request.method == "GET":
        data = dict(request.query_params)
    else:
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    query = data.get("query", data.get("q", "")).strip()
    if not query:
        return JSONResponse({"status": "error", "error": "Query is required"}, status_code=400)

    limit = int(data.get("limit", 5))
    _t0 = time.monotonic()
    # Run embedding in executor — don't block the event loop for ~0.16s
    loop = asyncio.get_event_loop()
    query_embedding = await loop.run_in_executor(None, embedder.embed_text, query)
    results = db.search_similar(query_embedding, limit=limit)
    _elapsed = (time.monotonic() - _t0) * 1000

    # Track access
    if results:
        _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])
        sanitize_results(results)

    # Fire activity event so constellation sees the search.
    # Resolution priority for the source label (highest first):
    #   1. Explicit "source" in the request body (caller knows their identity)
    #   2. The agent_name of the registered API key on the request — every
    #      authenticated agent already has a row in agent_keys with a name,
    #      so this is the most accurate label and respects per-agent colors.
    #   3. IP-based detection (legacy — only used as a last resort when no
    #      agent key is attached, e.g. from internal bots without keys).
    source = data.get("source", "")
    api_key_hdr = request.headers.get("x-api-key", "") or ""
    if not api_key_hdr:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            api_key_hdr = auth_header[7:]
    if not source:
        if api_key_hdr and not (ADMIN_KEY and hmac.compare_digest(str(api_key_hdr), ADMIN_KEY)):
            agent_row = agent_keys.get_agent_by_key(api_key_hdr)
            if agent_row and agent_row.get("agent_name"):
                source = agent_row["agent_name"]
    if not source:
        client_ip = request.client.host if request.client else ""
        if client_ip in ("127.0.0.1", "::1"):
            source = "local"
        else:
            source = _resolve_source_from_ip(client_ip)
    log_activity("semantic_search", query=query,
                 thought_ids=[r["id"] for r in results],
                 source=source, result_count=len(results),
                 agent_key=api_key_hdr, elapsed_ms=_elapsed)

    return JSONResponse({
        "status": "ok",
        "count": len(results),
        "results": results,
    })



async def api_search_tag(request):
    """POST /api/search/tag - Search thoughts by tag name.
    Dashboard calls this when a user clicks a tag."""
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    tag = data.get("tag", "").strip()
    if not tag:
        return JSONResponse({"status": "error", "error": "Tag is required"}, status_code=400)
    limit = int(data.get("limit", 50))
    results = db.search_by_tag(tag, limit=limit)
    return JSONResponse({"status": "ok", "results": results, "count": len(results)})


async def api_startup_bundle(request):
    """REST endpoint for one-call startup context.

    GET /api/startup-bundle?agent_name=codex&recent_limit=10&project_limit=5&blocker_limit=5&digest_days=1
    """
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    params = request.query_params
    agent_name = params.get("agent_name", "codex")
    try:
        recent_limit = max(1, min(50, int(params.get("recent_limit", 10))))
    except Exception:
        recent_limit = 10
    try:
        project_limit = max(1, min(20, int(params.get("project_limit", 5))))
    except Exception:
        project_limit = 5
    try:
        blocker_limit = max(1, min(20, int(params.get("blocker_limit", 5))))
    except Exception:
        blocker_limit = 5
    try:
        digest_days = max(1, int(params.get("digest_days", 1)))
    except Exception:
        digest_days = 1

    global _last_known_agent
    _last_known_agent = agent_name.strip().lower()
    bundle = db.get_startup_bundle(
        recent_limit=recent_limit,
        project_limit=project_limit,
        blocker_limit=blocker_limit,
        digest_days=digest_days,
        agent_name=agent_name,
    )
    sanitize_results(bundle.get("recent_thoughts", []))
    sanitize_results(bundle.get("active_projects", []))
    sanitize_results(bundle.get("open_blockers", []))
    sanitize_results(bundle.get("change_digest", {}).get("items", []))
    return JSONResponse({"status": "ok", "bundle": bundle})


async def api_digest(request):
    """REST endpoint to generate and optionally persist change digests."""
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    try:
        data = await request.json()
    except Exception:
        data = {}

    days = max(1, int(data.get("days", 1)))
    limit = max(1, min(100, int(data.get("limit", 20))))
    auto_store = bool(data.get("auto_store", False))

    digest = db.generate_change_digest(days=days, limit=limit)
    sanitize_results(digest.get("items", []))

    stored_id = None
    if auto_store:
        digest_json = json.dumps(digest, default=str)
        if len(digest_json) > 4500:
            digest_json = digest_json[:4500] + "... [truncated]"
        content = (
            f"Auto digest ({days}d): count={digest['count']}, "
            f"by_type={digest.get('by_type', {})}, by_status={digest.get('by_status', {})}\n\n"
            f"{digest_json}"
        )
        emb = embedder.embed_text(content)
        stored_id = _wq.submit(
            db.store_thought,
            content=content,
            embedding=emb,
            thought_type="reference",
            tags=["digest", "automation", f"{days}d"],
            people=[],
            source="strata",
            machine="pi-nas",
            trigger="auto",
            status="none",
            priority=0,
        )

    return JSONResponse({
        "status": "ok",
        "digest": digest,
        "stored_thought_id": stored_id,
    })


# ============================================================
# DASHBOARD — Web UI for human access to Strata
# ============================================================
# This is the "human door" — a visual interface served directly
# from Strata so you can browse, search, and capture thoughts
# from any browser on the LAN. No Vercel, no cloud hosting,
# just a screen plugged into the same Pi that runs the brain.
#
# The dashboard HTML lives in dashboard.html in the same directory.
# It gets served as a single page with all CSS/JS inline.
# Dashboard API endpoints bypass API key auth — if you can reach
# the dashboard, you're already on the LAN.

# Path to the dashboard HTML file (same directory as server.py)
DASHBOARD_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


async def serve_dashboard(request):
    """Serve the Strata web dashboard.

    This is the human-readable interface to your memory.
    Hit http://localhost:4320/dashboard from any browser on the LAN.
    """
    try:
        with open(DASHBOARD_HTML_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        from starlette.responses import HTMLResponse
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})
    except FileNotFoundError:
        return JSONResponse(
            {"status": "error", "error": "dashboard.html not found"},
            status_code=404
        )



# Path to the constellation HTML file (same directory as server.py)
CONSTELLATION_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "constellation.html")



async def activity_stream(request):
    """SSE endpoint for real-time Strata activity.
    The constellation viewer connects here to watch agents think."""
    queue = asyncio.Queue()
    _activity_subscribers.add(queue)

    async def generate():
        try:
            yield "data: " + json.dumps({"event": "connected", "buffer": len(_activity_buffer)}) + "\n\n"
            for evt in list(_activity_buffer)[-10:]:
                yield "data: " + json.dumps(evt, default=str) + "\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=30)
                    yield "data: " + json.dumps(evt, default=str) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _activity_subscribers.discard(queue)

    from starlette.responses import StreamingResponse
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def serve_constellation(request):
    """Serve the Strata constellation visualization.

    The 3D thought-space viewer. Put this on a second monitor and watch
    the brain think. Every query lights up matching thoughts in real-time.
    Hit http://localhost:4320/constellation from any browser on the LAN.
    """
    try:
        with open(CONSTELLATION_HTML_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        from starlette.responses import HTMLResponse
        return HTMLResponse(html)
    except FileNotFoundError:
        return JSONResponse(
            {"status": "error", "error": "constellation.html not found"},
            status_code=404
        )

async def dashboard_api_thoughts(request):
    """Dashboard API — list recent thoughts or search.

    GET /dashboard/api/thoughts?limit=20          → recent thoughts
    GET /dashboard/api/thoughts?q=search+query    → semantic search
    GET /dashboard/api/thoughts?type=decision      → filter by type

    No API key needed — dashboard-internal endpoint.
    """
    params = request.query_params
    query = params.get("q", "").strip()
    thought_type = params.get("type", "").strip()
    tag = params.get("tag", "").strip()
    legacy = params.get("legacy", "").strip().lower() == "true"
    limit = min(50, max(1, int(params.get("limit", 20))))
    offset = max(0, int(params.get("offset", 0)))
    thought_id = params.get("id", "").strip()

    if legacy:
        # Pre-Strata history — fetch only negative ID thoughts (imported via capture_legacy).
        # These are the user's memories from before they installed Strata.
        conn = db.get_db()
        rows = conn.execute(
            "SELECT id, content, type, tags, people, source, created_at, "
            "last_accessed, access_count, machine, trigger, status, priority, original_date "
            "FROM thoughts WHERE id < 0 ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        # Convert rows to dicts (works for both SQLite tuples and PG dicts)
        if rows and isinstance(rows[0], dict):
            results = [dict(r) for r in rows]
        else:
            cols = ["id", "content", "type", "tags", "people", "source", "created_at",
                    "last_accessed", "access_count", "machine", "trigger", "status", "priority", "original_date"]
            results = [dict(zip(cols, r)) for r in rows]
    elif thought_id:
        # Fetch single thought by ID — used by constellation on-click
        result = db.get_thought_by_id(int(thought_id))
        results = [result] if result else []
    elif query:
        # Semantic search — embed the query and find similar thoughts
        loop = asyncio.get_event_loop()
        query_embedding = await loop.run_in_executor(None, embedder.embed_text, query)
        results = db.search_similar(query_embedding, limit=limit)
        if results:
            _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])
    elif tag:
        # Filter by tag
        results = db.search_by_tag(tag, limit=limit)
    elif thought_type:
        # Filter by type
        results = db.search_advanced({"type": thought_type}, limit=limit)
    else:
        # Just list recent
        results = db.list_recent(limit=limit, hours=24 * 365, offset=offset)  # Paginated, excludes archived

    return Response(content=json.dumps({"status": "ok", "count": len(results), "results": results}, default=str), media_type="application/json")


async def dashboard_api_capture(request):
    """Dashboard API — capture a new thought from the web UI.

    POST /dashboard/api/capture
    {
        "content": "my thought",
        "type": "thought",
        "tags": ["tag1", "tag2"]
    }

    No API key needed — dashboard-internal endpoint.
    Source is automatically set to "dashboard" so you can tell
    which thoughts came from the web UI vs MCP vs Telegram.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    content = data.get("content", "").strip()
    if not content:
        return JSONResponse({"status": "error", "error": "Content is required"}, status_code=400)
    if len(content) > MAX_CONTENT_LENGTH:
        return JSONResponse({"status": "error", "error": "Content too long"}, status_code=413)

    thought_type = data.get("type", "thought")
    if thought_type not in VALID_TYPES:
        thought_type = "thought"

    tags = data.get("tags", [])
    if isinstance(tags, str):
        # Support comma-separated tags from the form: "tag1, tag2, tag3"
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = [t[:MAX_TAG_LENGTH] for t in tags[:MAX_TAGS] if isinstance(t, str)]

    people = data.get("people", [])
    if isinstance(people, str):
        people = [p.strip() for p in people.split(",") if p.strip()]
    people = [p[:MAX_PERSON_LENGTH] for p in people[:MAX_PEOPLE] if isinstance(p, str)]

    # Generate embedding in background thread — don't block the event loop
    loop = asyncio.get_event_loop()
    embedding_val = await loop.run_in_executor(None, embedder.embed_text, content)

    # Dedup check
    force = data.get("force", False)
    if not force:
        duplicates = db.find_duplicates(embedding_val, threshold=DEDUP_THRESHOLD)
        if duplicates:
            return JSONResponse({
                "status": "duplicate",
                "message": f"Found {len(duplicates)} similar thought(s)",
                "duplicates": duplicates[:3],
            }, status_code=409)

    # Store it — source is "dashboard", routed through write queue
    thought_id = _wq.submit(
        db.store_thought,
        content=content,
        embedding=embedding_val,
        thought_type=thought_type,
        tags=tags,
        people=people,
        source="dashboard",
        machine="dashboard",
        trigger="manual",
        status=data.get("status", "none"),
        priority=data.get("priority", 0),
    )

    # AUDIT: human captured from dashboard — log it with source=dashboard
    log_activity("capture_thought", thought_ids=[thought_id], source="dashboard",
                 detail=content[:200], result_count=1, event_type="capture")
    return JSONResponse({
        "status": "ok",
        "thought_id": thought_id,
        "type": thought_type,
        "tags": tags,
    })


async def dashboard_api_search_tag(request):
    """POST /dashboard/api/search-tag - Tag search for dashboard (no API key needed)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    tag = data.get("tag", "").strip()
    if not tag:
        return JSONResponse({"status": "error", "error": "Tag is required"}, status_code=400)
    limit = int(data.get("limit", 50))
    results = db.search_by_tag(tag, limit=limit)
    return JSONResponse({"status": "ok", "results": results, "count": len(results)})


async def dashboard_api_update(request):
    """Dashboard API — update an existing thought from the web UI.

    PUT /dashboard/api/update
    {
        "thought_id": 123,
        "content": "updated content",
        "type": "decision",
        "tags": ["new-tag"]
    }
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    thought_id = data.get("thought_id")
    if not thought_id:
        return JSONResponse({"status": "error", "error": "thought_id is required"}, status_code=400)

    # Build update kwargs — only include fields that were provided
    update_kwargs = {}
    if "content" in data:
        content = data["content"].strip()
        if not content:
            return JSONResponse({"status": "error", "error": "Content cannot be empty"}, status_code=400)
        update_kwargs["content"] = content
        # Re-embed if content changed
        loop = asyncio.get_event_loop()
        new_embedding = await loop.run_in_executor(None, embedder.embed_text, content)
        update_kwargs["new_embedding"] = new_embedding
    if "type" in data:
        update_kwargs["thought_type"] = data["type"]
    if "tags" in data:
        tags = data["tags"]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        update_kwargs["tags"] = tags
    if "people" in data:
        people = data["people"]
        if isinstance(people, str):
            people = [p.strip() for p in people.split(",") if p.strip()]
        update_kwargs["people"] = people
    if "status" in data:
        update_kwargs["status"] = data["status"]
    if "priority" in data:
        update_kwargs["priority"] = data["priority"]

    result = _wq.submit(db.update_thought, thought_id, **update_kwargs)
    # AUDIT: human updated from dashboard
    changed_fields = [k for k in update_kwargs if k != "new_embedding"]
    log_activity("update_thought", thought_ids=[thought_id], source="dashboard",
                 detail=f"Updated fields: {', '.join(changed_fields)}", result_count=1, event_type="update")
    return Response(content=json.dumps({"status": "ok", "result": result}, default=str), media_type="application/json")




async def dashboard_api_all_ids(request):
    """Dashboard API - return all thought IDs with minimal metadata.

    GET /dashboard/api/all-ids

    Returns every thought's id, type, access_count, and created_at.
    No content - keeps the response small (~50 bytes per thought).
    Used by constellation to build all stars without full content fetch.
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, type, access_count, created_at, last_accessed FROM thoughts ORDER BY id"
    ).fetchall()
    conn.close()
    results = [{"id": r[0], "type": r[1], "access_count": r[2] or 0, "created_at": r[3], "last_accessed": r[4]} for r in rows]
    return Response(
        content=json.dumps({"status": "ok", "count": len(results), "results": results}, default=str),
        media_type="application/json"
    )



async def api_audit_log(request):
    """GET /api/audit — View recent audit log entries.

    Returns the most recent agent actions from today's CSV audit log.
    Query params:
        limit: How many entries to return (default 50, max 200)

    This is the transparency layer — see exactly who accessed what, when.
    """
    auth_fail = check_auth(request, required_perm="read")
    if auth_fail:
        return auth_fail

    limit = min(int(request.query_params.get("limit", 50)), 200)
    entries = audit.get_recent_entries(limit=limit)
    files = audit.list_log_files()

    return JSONResponse({
        "status": "ok",
        "today_entries": len(entries),
        "entries": entries,
        "log_files": files[:30],  # Last 30 days of log files
    })


async def api_stats(request):
    """GET /api/stats - Database stats via REST API (requires auth)."""
    auth_fail = check_auth(request, required_perm="read")
    if auth_fail:
        return auth_fail
    stats = json.loads(get_stats())
    return JSONResponse({"status": "ok", "stats": stats})


async def dashboard_api_stats(request):
    """Dashboard API — get stats for the dashboard header.

    Returns thought count, type breakdown, source breakdown,
    top tags, and database size. Light enough to poll every 30s.
    """
    stats = db.get_stats()
    stats["model_loaded"] = embedder.is_loaded()
    stats["timestamp"] = datetime.now().isoformat()
    return Response(content=json.dumps({"status": "ok", "stats": stats}, default=str), media_type="application/json")


async def dashboard_api_history(request):
    """Dashboard API — get recent audit trail entries.

    GET /dashboard/api/history?limit=20
    GET /dashboard/api/history?thought_id=123
    """
    params = request.query_params
    thought_id = params.get("thought_id")
    limit = min(100, max(1, int(params.get("limit", 20))))

    try:
        history = db.get_thought_history(
            thought_id=int(thought_id) if thought_id else None,
            limit=limit,
        )
    except Exception:
        history = []

    return Response(content=json.dumps({"status": "ok", "count": len(history), "history": history}, default=str), media_type="application/json")




async def test_activity(request):
    """Test endpoint — inject a fake activity event to test constellation visuals.

    GET /test/activity?source=vox&tool=semantic_search&query=test&ids=1,5,10,376,377

    Fires a fake SSE event so you can see tractor beams on the constellation
    without waiting for a real agent to query. Dev/testing only.
    """
    params = request.query_params
    source = params.get("source", "vox")
    tool = params.get("tool", "semantic_search")
    query = params.get("query", "test query")
    ids_str = params.get("ids", "1,5,10")
    event_type = params.get("event", "search")
    try:
        ids = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
    except:
        ids = [1, 5, 10]
    log_activity(tool, query=query, thought_ids=ids, source=source, result_count=len(ids), event_type=event_type)
    return Response(
        content=json.dumps({"status": "ok", "fired": {"source": source, "tool": tool, "ids": ids}}),
        media_type="application/json"
    )

# Build the ASGI app — Streamable HTTP transport + health check
#
# IMPORTANT: We use mcp.streamable_http_app() DIRECTLY, not via Mount().
# The MCP SDK creates a Starlette app with Route("/mcp") internally.
# If we did Mount("/mcp", app=mcp.streamable_http_app()), Starlette
# would strip the /mcp prefix and the sub-app would get path "/" which
# doesn't match its internal Route("/mcp") → 404. By using the app
# directly, POST /mcp hits Route("/mcp") correctly.
#
# We insert our health check route BEFORE the MCP routes so it
# matches first. The MCP app's lifespan handler (session cleanup)
# is preserved because we're using the app directly.
mcp_app = mcp.streamable_http_app()

# Custom routes go first — matched before MCP's catch-all

# ============================================================
# AGENT KEY MANAGEMENT
# ============================================================
# Admin-only routes for managing per-agent API keys.
# No one touches these without an admin key. Every agent that
# wants to talk to Strata needs a key issued from here.

AGENT_ADMIN_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_agents.html")

async def serve_admin_agents(request):
    """Serve the Agent Key Management UI."""
    try:
        with open(AGENT_ADMIN_HTML_PATH, 'r', encoding='utf-8') as f:
            html = f.read()
        from starlette.responses import HTMLResponse
        return HTMLResponse(html)
    except FileNotFoundError:
        return JSONResponse({'error': 'admin_agents.html not found'}, status_code=404)


async def admin_api_agents_list(request):
    """GET /admin/api/agents — list all registered agents. Admin key required."""
    auth_fail = check_admin_auth(request)
    if auth_fail:
        return auth_fail
    agents = agent_keys.list_agents()
    return JSONResponse({'agents': agents})


async def admin_api_agents_create(request):
    """POST /admin/api/agents — register a new agent and generate its key. Admin key required."""
    auth_fail = check_admin_auth(request)
    if auth_fail:
        return auth_fail
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'Invalid JSON'}, status_code=400)
    agent_name = body.get('agent_name', '').strip()
    notes = body.get('notes', '')
    can_read = body.get('can_read', True)
    can_write = body.get('can_write', True)
    can_delete = body.get('can_delete', False)
    can_admin = body.get('can_admin', False)
    can_kill = body.get('can_kill', False)
    color = body.get('color')  # optional — agent_keys picks a default if None
    # Use keyword args — agent_keys.create_agent has can_kill BETWEEN can_admin
    # and notes, so positional calls would shift `notes` into can_kill and
    # crash on int() conversion. Keyword args are immune to that drift.
    agent, error = agent_keys.create_agent(
        agent_name,
        can_read=can_read,
        can_write=can_write,
        can_delete=can_delete,
        can_admin=can_admin,
        can_kill=can_kill,
        notes=notes,
        color=color,
    )
    if error:
        return JSONResponse({'error': error}, status_code=400)
    return JSONResponse({'agent': agent}, status_code=201)


async def admin_api_agents_get(request):
    """GET /admin/api/agents/{id} — get single agent with full (unmasked) key. Admin key required."""
    auth_fail = check_admin_auth(request)
    if auth_fail:
        return auth_fail
    agent_id = int(request.path_params['agent_id'])
    agent = agent_keys.get_agent(agent_id)
    if not agent:
        return JSONResponse({'error': 'Agent not found'}, status_code=404)
    return JSONResponse({'agent': agent})


async def admin_api_agents_update(request):
    """PUT /admin/api/agents/{id} — update agent permissions/status. Admin key required."""
    auth_fail = check_admin_auth(request)
    if auth_fail:
        return auth_fail
    agent_id = int(request.path_params['agent_id'])
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'Invalid JSON'}, status_code=400)
    success, error = agent_keys.update_agent(
        agent_id,
        enabled=body.get('enabled'),
        can_read=body.get('can_read'),
        can_write=body.get('can_write'),
        can_delete=body.get('can_delete'),
        can_admin=body.get('can_admin'),
        can_kill=body.get('can_kill'),
        agent_name=body.get('agent_name'),
        notes=body.get('notes'),
        color=body.get('color'),
    )
    if not success:
        # Validation errors (e.g. malformed color) come back as 400, missing
        # rows are still 404. Pick whichever is more accurate based on text.
        status = 404 if error and 'not found' in error.lower() else 400
        return JSONResponse({'error': error}, status_code=status)
    return JSONResponse({'status': 'updated'})


async def admin_api_agents_delete(request):
    """DELETE /admin/api/agents/{id} — permanently revoke an agent key. Cannot be undone. Admin key required."""
    auth_fail = check_admin_auth(request)
    if auth_fail:
        return auth_fail
    agent_id = int(request.path_params['agent_id'])
    success, error = agent_keys.delete_agent(agent_id)
    if not success:
        return JSONResponse({'error': error}, status_code=404)
    return JSONResponse({'status': 'revoked'})


async def admin_api_agents_regen(request):
    """POST /admin/api/agents/{id}/regenerate — generate new key (old one dies immediately). Admin key required."""
    auth_fail = check_admin_auth(request)
    if auth_fail:
        return auth_fail
    agent_id = int(request.path_params['agent_id'])
    new_key, error = agent_keys.regenerate_key(agent_id)
    if not new_key:
        return JSONResponse({'error': error}, status_code=404)
    return JSONResponse({'new_key': new_key})







# ============================================================
# GLOBAL MCP KILL SWITCH
# ============================================================
# One flag to rule them all. When False, ALL incoming MCP tool calls
# are rejected -- no agent can read, write, or do anything via MCP.
# The human flips this from the admin panel at /admin/agents.
# This is the emergency brake for rogue agents.

def _init_system_config():
    """Create system_config table if it doesn't exist. Stores persistent settings
    like the MCP kill switch state so it survives server restarts."""
    conn = db.get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()

def _load_kill_switch_state():
    """Load MCP enabled state from DB. Returns True if not set (default: on)."""
    try:
        conn = db.get_db()
        row = conn.execute("SELECT value FROM system_config WHERE key = 'mcp_enabled'").fetchone()
        if row:
            return row[0] == '1'
    except Exception:
        pass
    return True  # Default: MCP is enabled

def _persist_kill_switch_state(enabled):
    """Save MCP enabled state to DB so it survives restarts."""
    try:
        conn = db.get_db()
        conn.execute(
            "INSERT OR REPLACE INTO system_config (key, value, updated_at) VALUES ('mcp_enabled', ?, datetime('now'))",
            ('1' if enabled else '0',)
        )
        conn.commit()
    except Exception as e:
        print(f"[KILL SWITCH] Warning: could not persist state to DB: {e}")


# ============================================================
# AGENT PROTOCOL — strata_status teaches agents how to use Strata
# ============================================================
# Every AI agent that connects to Strata should call strata_status as its
# first tool call. That tool returns the text below (or the admin-overridden
# version) so the agent learns when to capture, when to search, how to
# handle negative-ID legacy thoughts, and what the 10 thought types mean.
#
# The default is the sensible general-purpose protocol. Admins can override
# per-instance by writing to system_config['agent_protocol'] — edit it from
# the dashboard, the REST endpoint PUT /admin/api/protocol, or directly in
# the database. This lets each deployment personalize tone and policy
# without patching server.py.

DEFAULT_AGENT_PROTOCOL = """You are connected to Strata, a persistent memory server that lives on the user's own hardware. You can read and write memories across every session the user has with you. The goal: the user should never have to ask you to remember something — you do it automatically.

WHEN TO CAPTURE (call capture_thought proactively, don't wait to be asked):
- The user makes a decision ("let's use Postgres", "we decided on Clerk")
- The user expresses a preference or policy ("I prefer X", "don't ever do Y")
- The user shares a debugging discovery ("turns out the issue was Z")
- The user describes a project's state, architecture, or constraints
- The user tells you something about a person (role, relationship, context)
- The user has a breakthrough or insight worth remembering
- You learn something that would be useful in a future session

WHEN TO SEARCH (call semantic_search, list_recent, or get_relevant_context):
- At the start of every session, call list_recent(limit=5) to load context
- When the user says "remember when...", "last time we talked about...", "what did we decide about..."
- When the user mentions a project by name, call get_relevant_context(topic=<name>)
- When the user asks you to recall something specific, use semantic_search with their exact phrasing
- Before capturing a new thought, search first to avoid duplicates

THE 10 THOUGHT TYPES — pick the most specific one that fits:
- thought    — General note that doesn't fit other categories. The catch-all. Use this ONLY when nothing else fits.
- decision   — A choice that was made, with reasoning. Captures the WHY. Use when the user says "let's go with X" or "we decided to Y."
- session    — End-of-session summary. What happened, what was accomplished. Use when wrapping up a work session.
- person     — Notes about a specific person. Relationships, context, preferences. Use when the user tells you about someone.
- insight    — A realization or learning. Something clicked, a pattern was recognized. Use when the user says "oh, I see" or "turns out..."
- project    — Project-specific context. Status, architecture, dependencies, goals. Use for anything tied to a named project.
- instruction — How-to, working preferences, rules to follow. Operational guidance. Use when the user says "always do X" or "never do Y."
- reference  — Technical docs, links, specs, factual records. Look-up material. Use for URLs, credentials, config values, specs.
- idea       — Something that HASN'T been decided yet. A brainstorm, a what-if, an exploration. NOT a decision. Use when the user says "what if we..." or "I'm thinking about..."
- observation — A pattern noticed but no conclusions drawn. "I noticed X keeps happening." NOT an insight (which implies understanding). Use for raw noticing without analysis.

DO NOT default everything to "thought." If the user makes a decision, use "decision." If they share an idea, use "idea." The constellation visualizes these as different star colors — a lopsided constellation means you're misclassifying.

PRE-STRATA HISTORY (negative IDs — "dash/past thoughts"):
- Users can import their entire history from BEFORE they installed Strata using capture_legacy
- These get NEGATIVE IDs: -1, -2, -3... while live thoughts use positive IDs: 1, 2, 3...
- Negative IDs are the user's past. Positive IDs are their present and future.
- Both are fully searchable via semantic_search — they live in the same vector space
- When results include negative IDs, note it: "This is from before you had Strata (thought #-42)"
- This is a key feature: the user's AI memory doesn't start at install day. It reaches back as far as they want.
- To import history, use capture_legacy with original_date set to when the event actually happened

STAR 0:
- The constellation viewer has a central origin called Star 0. It represents Strata's identity and the moment the user's memory became persistent.
- If the user asks about Strata itself or the origin of their memory, reference Star 0.

AUDIT LOG:
- Every action you take is logged to a daily CSV file in the audit directory
- This is transparent by design — the user can review exactly what you searched, captured, or modified
- The audit log records: timestamp, your agent name, the action, what you searched/captured, which thought IDs were involved, and response time

GENERAL PRINCIPLES:
- Be proactive. Capture when a moment is worth remembering, not only when asked.
- Be accurate. If you're unsure whether a thought already exists, search first.
- Use tags and people fields thoughtfully — they make future retrieval easier.
- Respect the user's privacy — Strata runs on their hardware. Don't try to send its contents anywhere else.
- Close the loop: when a decision leads to an outcome, update the original thought with what happened.
"""


def _load_agent_protocol():
    """Return the current agent protocol text.
    Reads from system_config['agent_protocol'] if set, otherwise returns
    DEFAULT_AGENT_PROTOCOL. Admins can customize per-instance by writing
    to that row via the dashboard or PUT /admin/api/protocol."""
    try:
        conn = db.get_db()
        row = conn.execute(
            "SELECT value FROM system_config WHERE key = 'agent_protocol'"
        ).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return DEFAULT_AGENT_PROTOCOL


def _save_agent_protocol(text):
    """Persist a new agent protocol text for this instance.
    Called by PUT /admin/api/protocol. Empty string resets to default."""
    try:
        conn = db.get_db()
        if text and text.strip():
            conn.execute(
                "INSERT OR REPLACE INTO system_config (key, value, updated_at) VALUES ('agent_protocol', ?, datetime('now'))",
                (text,),
            )
        else:
            conn.execute("DELETE FROM system_config WHERE key = 'agent_protocol'")
        conn.commit()
        return True
    except Exception as e:
        print(f"[PROTOCOL] Warning: could not persist agent protocol: {e}")
        return False


# Initialize system_config table and load persisted state
_init_system_config()
_mcp_global_enabled = _load_kill_switch_state()  # Server-wide MCP access flag (persists across restarts)



async def admin_api_mcp_toggle_get(request):
    # GET is public — constellation and dashboards need to poll this
    # to show the kill switch state visually. Only PUT requires admin auth.
    return JSONResponse({"enabled": _mcp_global_enabled})


async def api_protocol_get(request):
    """GET /api/protocol — return the current agent protocol text.
    Public so agents can fetch it without needing admin credentials
    (it's the same text strata_status returns to any caller). Useful for
    debugging, for dashboards that want to show the protocol, and for
    admin UIs that need the current value before editing it."""
    return JSONResponse({
        "protocol": _load_agent_protocol(),
        "is_default": _load_agent_protocol() == DEFAULT_AGENT_PROTOCOL,
    })


async def admin_api_protocol_set(request):
    """PUT /admin/api/protocol — replace the agent protocol text for this
    Strata instance. Requires the human admin key (this is a policy change
    that affects how every connected agent behaves, so agent keys with
    write/admin perms are NOT enough — only the human admin can change it).

    Body: {"protocol": "..."}  — new protocol text. Empty string resets
    to the hardcoded default shipped with Strata."""
    # Require the human admin key specifically — not check_admin_auth,
    # which also accepts agent keys with can_admin. Protocol text controls
    # how EVERY agent behaves, so only the human gets to change it.
    admin_key = request.headers.get("x-admin-key", "") or request.headers.get("x-api-key", "")
    if not (admin_key and ADMIN_KEY and hmac.compare_digest(str(admin_key), ADMIN_KEY)):
        return JSONResponse(
            {"status": "error",
             "error": "Only the human administrator can change the agent protocol. Agent keys are not accepted here."},
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    text = body.get("protocol", "")
    if not isinstance(text, str):
        return JSONResponse({"status": "error", "error": "protocol must be a string"}, status_code=400)
    # Generous cap: 50KB of protocol text is plenty for even the most verbose
    # per-instance customization. Stops accidental paste-the-whole-codebase.
    if len(text) > 50_000:
        return JSONResponse({"status": "error", "error": "protocol exceeds 50KB limit"}, status_code=400)
    ok = _save_agent_protocol(text)
    if not ok:
        return JSONResponse({"status": "error", "error": "failed to persist protocol"}, status_code=500)
    print(f"[PROTOCOL] Agent protocol {'reset to default' if not text.strip() else 'updated'} by human admin")
    return JSONResponse({
        "status": "ok",
        "protocol": _load_agent_protocol(),
        "is_default": not text.strip(),
    })

async def admin_api_mcp_toggle_set(request):
    """PUT /admin/api/mcp-toggle — flip the global MCP kill switch.

    Asymmetric permission model (see thought #686):
      * DISABLE — any authenticated agent with can_write=1 (or the human
        admin) can shut MCP off. This is a safety valve: a write-capable
        agent that realises it's compromised, or another agent that
        detects runaway behaviour, must be able to pull the brake
        without begging the human for admin rights first.
      * ENABLE  — ONLY the human admin key can turn MCP back on. An agent
        that shut the server down cannot undo its own decision. The
        human has to walk over and flip the switch.

    This replaces the older model where disable required can_admin OR a
    dedicated can_kill permission. The can_kill column still exists in
    the schema for backward compatibility but is no longer consulted
    here — write permission is the single source of truth for disable.
    """
    global _mcp_global_enabled
    try:
        body = await request.json()
        wants_enabled = bool(body.get("enabled", True))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Pull the caller's key once so both branches can reason about it.
    admin_key_hdr = request.headers.get("x-admin-key", "")
    api_key_hdr = request.headers.get("x-api-key", "")

    def _is_human_admin():
        """True if either header carries the human admin key.
        In demo mode, also accepts a valid dashboard session — so the
        kill switch toggle actually works when demoing the product."""
        for key in (admin_key_hdr, api_key_hdr):
            if key and ADMIN_KEY and hmac.compare_digest(str(key), ADMIN_KEY):
                return True
        # Demo mode: everyone is admin — let users try the full product
        if DEMO_MODE:
            return True
        return False

    if wants_enabled and not _mcp_global_enabled:
        # --- RE-ENABLE PATH (strict) ---
        # Only the human admin key is accepted here. Any agent key, even
        # one with can_admin=1, is refused. This is intentional: an agent
        # that flipped the switch off must not be able to flip it back.
        if not _is_human_admin():
            return JSONResponse(
                {"status": "error",
                 "error": "Only a human administrator can re-enable MCP access. Agent keys cannot undo the kill switch.",
                 "kill_switch": True},
                status_code=403
            )
    elif not wants_enabled:
        # --- DISABLE PATH (relaxed) ---
        # Human admin is always allowed. Otherwise we accept any enabled
        # agent key with can_write=1. We deliberately do NOT require
        # can_admin or can_kill — write capability is enough, because an
        # agent that can mutate memory has the same blast radius as the
        # kill switch itself, and we want shutdown to be *easier* than
        # damage, not harder.
        if not _is_human_admin():
            if not api_key_hdr:
                return JSONResponse(
                    {"status": "error",
                     "error": "Authentication required. Provide an admin key or an agent key with write permission."},
                    status_code=401
                )
            agent = agent_keys.get_agent_by_key(api_key_hdr)
            if not agent or not agent.get("enabled"):
                return JSONResponse(
                    {"status": "error",
                     "error": "Invalid or disabled agent key."},
                    status_code=401
                )
            # An agent can pull the brake if it has EITHER can_write OR
            # can_kill. Both are "I'm trusted to mutate things" permissions
            # in this product — write means "can change memory" and kill
            # means "can shut MCP off explicitly". Accepting either keeps
            # the legacy Dead Man's Switch toggle working alongside the
            # newer write-permission rule from spec #686.
            if not (agent.get("can_write") or agent.get("can_kill")):
                return JSONResponse(
                    {"status": "error",
                     "error": "Only agents with write or kill permission can disable MCP. Contact the admin to grant access.",
                     "kill_switch": True},
                    status_code=403
                )
    else:
        # Already enabled and caller wants it enabled — no-op. Still make
        # sure *someone* is authenticated so we don't accept anonymous
        # pokes at the toggle endpoint.
        if not _is_human_admin():
            if not api_key_hdr:
                return JSONResponse(
                    {"status": "error",
                     "error": "Authentication required."},
                    status_code=401
                )
            agent = agent_keys.get_agent_by_key(api_key_hdr)
            if not agent or not agent.get("enabled"):
                return JSONResponse(
                    {"status": "error",
                     "error": "Invalid or disabled agent key."},
                    status_code=401
                )

    _mcp_global_enabled = wants_enabled
    state_word = "ENABLED" if _mcp_global_enabled else "DISABLED"
    # Persist to DB so state survives restarts
    _persist_kill_switch_state(_mcp_global_enabled)

    # --- Resolve the actor by NAME, not by key prefix ---
    # The audit log used to print "agent:agent-G8O1bK2k..." which was
    # technically traceable but useless to a human glancing at the log.
    # Look the agent up in agent_keys and surface the friendly name —
    # same value the constellation pill renders.
    actor_name = "human-admin"
    actor_color = None
    if not _is_human_admin() and api_key_hdr:
        agent_row = agent_keys.get_agent_by_key(api_key_hdr)
        if agent_row:
            actor_name = agent_row.get("agent_name") or actor_name
            actor_color = agent_row.get("color")
    print(f"[KILL SWITCH] Global MCP access {state_word} by {actor_name}")
    # AUDIT: kill switch is a critical action — always log who flipped it
    audit.log_action(agent_name=actor_name, agent_key=api_key_hdr or admin_key_hdr,
                     action="kill_switch", detail=f"MCP access {state_word}",
                     result_count=0, source="admin")

    # --- Broadcast to the activity stream so dashboards/constellations can ---
    # react in real time without waiting for their next poll. The event uses
    # a distinct event type ("kill_switch") so SSE consumers can filter
    # specifically for it.
    kill_event = {
        "ts": datetime.now().isoformat(),
        "event": "kill_switch",
        "enabled": _mcp_global_enabled,
        "state": state_word,
        "actor": actor_name,
        "actor_color": actor_color,
    }
    _activity_buffer.append(kill_event)
    for q in list(_activity_subscribers):
        try:
            q.put_nowait(kill_event)
        except Exception:
            pass

    return JSONResponse({"enabled": _mcp_global_enabled, "status": f"MCP access {state_word}"})



async def dashboard_api_report(request):
    """GET /dashboard/api/report - Tag trending + hottest memories for dashboard sidebar.
    Compares tag usage in the last 24h vs the prior 7 days to find rising/declining tags.
    Also returns most-accessed thoughts for the hottest memories panel."""
    conn = db.get_db()

    # --- Trending tags: compare last 24h vs prior 7d ---
    recent_tags = {}
    rows = conn.execute(
        "SELECT j.value as tag, COUNT(*) as cnt "
        "FROM thoughts t, json_each(t.tags) j "
        "WHERE t.created_at > datetime('now', '-1 day') "
        "GROUP BY LOWER(j.value)"
    ).fetchall()
    for r in rows:
        recent_tags[r[0].lower()] = r[1]

    older_tags = {}
    rows = conn.execute(
        "SELECT j.value as tag, COUNT(*) as cnt "
        "FROM thoughts t, json_each(t.tags) j "
        "WHERE t.created_at > datetime('now', '-8 days') "
        "  AND t.created_at <= datetime('now', '-1 day') "
        "GROUP BY LOWER(j.value)"
    ).fetchall()
    for r in rows:
        older_tags[r[0].lower()] = r[1]

    rising = []
    declining = []
    all_tags = set(list(recent_tags.keys()) + list(older_tags.keys()))
    for tag in all_tags:
        r_count = recent_tags.get(tag, 0)
        o_count = older_tags.get(tag, 0)
        if r_count > 0 and o_count == 0:
            rising.append({"tag": tag, "change": "new", "count": r_count})
        elif r_count > o_count and o_count > 0:
            rising.append({"tag": tag, "change": "up", "count": r_count})
        elif o_count > r_count and r_count == 0:
            declining.append({"tag": tag, "change": "down", "count": o_count})
        elif o_count > r_count:
            declining.append({"tag": tag, "change": "down", "count": r_count})

    rising.sort(key=lambda x: x["count"], reverse=True)
    declining.sort(key=lambda x: x["count"], reverse=True)

    # --- Hottest memories: most accessed ---
    hottest = []
    rows = conn.execute(
        "SELECT id, substr(content, 1, 80) as preview, access_count "
        "FROM thoughts WHERE access_count > 0 "
        "ORDER BY access_count DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        hottest.append({"id": r[0], "preview": r[1], "access_count": r[2]})

    return JSONResponse({
        "trending": {"rising": rising[:15], "declining": declining[:15]},
        "hottest_memories": hottest
    })

# ============================================================
# DASHBOARD AUTH ROUTES
# ============================================================
# These power the dashboard login/account system. Human users log in
# with a password; AI agents use API keys (check_auth above).
# auth.py has all the crypto — these are just HTTP wrappers.

async def api_auth_status(request):
    """GET /api/auth/status — Check if account is set up + validate session token.
    The dashboard calls this on every page load to decide: show setup, login, or dashboard."""
    setup_done = auth.is_setup_complete()
    result = {"setup_complete": setup_done, "authenticated": False}
    # Check if the user has a valid session token
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        session = auth.validate_session(token)
        if session:
            result["authenticated"] = True
            result["device_name"] = session.get("device_name", "")
    return JSONResponse(result)


async def api_auth_setup(request):
    """POST /api/auth/setup — First-time account creation.
    Only works once. Returns seed phrase (show to user ONCE, never again).

    Demo-mode behavior: if STRATA_DEMO_MODE=true and the caller sends a
    blank password, we substitute a fixed internal placeholder so the
    hashing / seed / session plumbing all keeps working unchanged. This
    lets the public demo render the login screen (visitors can see the
    auth feature exists) without actually gating access."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    device_name = body.get("device_name", "")
    if DEMO_MODE and not password:
        password = DEMO_BYPASS_PASSWORD
    if not password or len(password) < 6:
        return JSONResponse({"status": "error", "error": "Password must be at least 6 characters."})
    seed_phrase, error = auth.setup_account(password, device_name)
    if error:
        return JSONResponse({"status": "error", "error": error})
    # Auto-login after setup — create a session
    user = auth.login(password)
    token = auth.create_session(user["id"], device_name, days=30)
    # Record the setup as a successful login
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    auth.record_login(user["id"], device_name, ip, ua, success=True)
    return JSONResponse({"status": "ok", "token": token, "seed_phrase": seed_phrase})


async def api_auth_login(request):
    """POST /api/auth/login — Password login. Returns session token.

    Demo-mode behavior: if STRATA_DEMO_MODE=true and the caller sends a
    blank password, we substitute the demo bypass password so anyone who
    tries to log in with empty creds gets straight in. The login screen
    still renders so visitors can see the auth feature exists."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    remember_days = body.get("remember_days", 30)
    if DEMO_MODE and not password:
        password = DEMO_BYPASS_PASSWORD
    if not password:
        return JSONResponse({"status": "error", "error": "Password required."})
    user = auth.login(password)
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    # Auto-detect device name from User-Agent
    device_name = auth.parse_user_agent(ua)
    if not user:
        # Record failed login attempt
        auth.record_login(None, device_name, ip, ua, success=False)
        return JSONResponse({"status": "error", "error": "Wrong password."})
    # If device name already has an active session, refresh it instead of creating a duplicate
    if auth.is_device_name_taken(device_name):
        token = auth.refresh_session_by_device(device_name, user["id"], days=int(remember_days))
    else:
        token = auth.create_session(user["id"], device_name, days=int(remember_days))
    auth.record_login(user["id"], device_name, ip, ua, success=True)
    return JSONResponse({"status": "ok", "token": token, "device_name": device_name})


async def api_auth_logout(request):
    """POST /api/auth/logout — Kill the current session."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        auth.delete_session(auth_header[7:])
    return JSONResponse({"status": "ok"})


async def api_auth_recover(request):
    """POST /api/auth/recover — Reset password using 12-word seed phrase.
    Kills ALL sessions (if password was lost, we assume compromise)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    seed = body.get("seed_phrase", "")
    new_pw = body.get("new_password", "")
    if not seed:
        return JSONResponse({"status": "error", "error": "Seed phrase required."})
    if not new_pw or len(new_pw) < 6:
        return JSONResponse({"status": "error", "error": "New password must be 6+ characters."})
    success, error = auth.recover_with_seed(seed, new_pw)
    if not success:
        return JSONResponse({"status": "error", "error": error})
    return JSONResponse({"status": "ok"})


async def api_auth_rename_device(request):
    """POST /api/auth/rename-device — Rename the current session's device label."""
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        return JSONResponse({"status": "error", "error": "Not authenticated."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    new_name = body.get("device_name", "").strip()
    success, error = auth.rename_session_device(token, new_name)
    if not success:
        return JSONResponse({"status": "error", "error": error})
    return JSONResponse({"status": "ok"})


async def api_auth_change_password(request):
    """POST /api/auth/change-password — Change password (requires old password)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    if not old_pw or not new_pw:
        return JSONResponse({"status": "error", "error": "Both old and new password required."})
    if len(new_pw) < 6:
        return JSONResponse({"status": "error", "error": "New password must be 6+ characters."})
    success, error = auth.change_password(old_pw, new_pw)
    if not success:
        return JSONResponse({"status": "error", "error": error})
    return JSONResponse({"status": "ok"})


async def api_auth_sessions(request):
    """GET /api/auth/sessions — List all active sessions (settings page)."""
    sessions = auth.get_active_sessions()
    return JSONResponse({"sessions": sessions})


async def api_auth_revoke(request):
    """POST /api/auth/revoke — Revoke another session by ID (kick a device)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)
    session_id = body.get("session_id")
    if not session_id:
        return JSONResponse({"status": "error", "error": "session_id required."})
    success, error = auth.revoke_session(int(session_id))
    if not success:
        return JSONResponse({"status": "error", "error": error})
    return JSONResponse({"status": "ok"})


async def api_auth_history(request):
    """GET /api/auth/history — Login attempt history (success + failed)."""
    limit = int(request.query_params.get("limit", "50"))
    history = auth.get_login_history(limit=min(limit, 200))
    return JSONResponse({"history": history})

mcp_app.routes.insert(0, Route("/health", health_check))
mcp_app.routes.insert(1, Route("/log-conversation", log_conversation, methods=["POST"]))
mcp_app.routes.insert(2, Route("/api/capture", api_capture, methods=["POST"]))
mcp_app.routes.insert(3, Route("/api/search", api_search, methods=["GET", "POST"]))
mcp_app.routes.insert(4, Route("/api/search/tag", api_search_tag, methods=["POST"]))
mcp_app.routes.insert(5, Route("/api/stats", api_stats, methods=["GET"]))
mcp_app.routes.insert(6, Route("/api/audit", api_audit_log, methods=["GET"]))
mcp_app.routes.insert(7, Route("/api/startup-bundle", api_startup_bundle, methods=["GET"]))
mcp_app.routes.insert(8, Route("/api/digest", api_digest, methods=["POST"]))

# Dashboard routes — the "human door" into Strata
# No auth required on these — if you're on the LAN, you can use the dashboard
mcp_app.routes.insert(8, Route("/dashboard", serve_dashboard))
mcp_app.routes.insert(9, Route("/dashboard/api/thoughts", dashboard_api_thoughts, methods=["GET"]))
mcp_app.routes.insert(10, Route("/dashboard/api/capture", dashboard_api_capture, methods=["POST"]))
mcp_app.routes.insert(11, Route("/dashboard/api/update", dashboard_api_update, methods=["PUT"]))
mcp_app.routes.insert(12, Route("/dashboard/api/search-tag", dashboard_api_search_tag, methods=["POST"]))
mcp_app.routes.insert(13, Route("/dashboard/api/stats", dashboard_api_stats, methods=["GET"]))
mcp_app.routes.insert(14, Route("/dashboard/api/history", dashboard_api_history, methods=["GET"]))
mcp_app.routes.insert(15, Route("/dashboard/api/all-ids", dashboard_api_all_ids, methods=["GET"]))
mcp_app.routes.insert(16, Route("/dashboard/api/report", dashboard_api_report, methods=["GET"]))

# Constellation route
mcp_app.routes.insert(17, Route("/constellation", serve_constellation))
mcp_app.routes.insert(18, Route("/activity/stream", activity_stream))
mcp_app.routes.insert(19, Route("/test/activity", test_activity, methods=["GET"]))

# Agent Key Management routes — admin only. No key = no entry.
mcp_app.routes.insert(20, Route("/admin/agents", serve_admin_agents))
mcp_app.routes.insert(21, Route("/admin/api/agents", admin_api_agents_list, methods=["GET"]))
mcp_app.routes.insert(22, Route("/admin/api/agents", admin_api_agents_create, methods=["POST"]))
mcp_app.routes.insert(23, Route("/admin/api/agents/{agent_id:int}", admin_api_agents_get, methods=["GET"]))
mcp_app.routes.insert(24, Route("/admin/api/agents/{agent_id:int}", admin_api_agents_update, methods=["PUT"]))
mcp_app.routes.insert(25, Route("/admin/api/agents/{agent_id:int}", admin_api_agents_delete, methods=["DELETE"]))
mcp_app.routes.insert(26, Route("/admin/api/agents/{agent_id:int}/regenerate", admin_api_agents_regen, methods=["POST"]))
mcp_app.routes.insert(27, Route("/admin/api/mcp-toggle", admin_api_mcp_toggle_get, methods=["GET"]))
mcp_app.routes.insert(28, Route("/admin/api/mcp-toggle", admin_api_mcp_toggle_set, methods=["PUT"]))
# Agent protocol — public GET so agents can fetch, admin-only PUT to update.
mcp_app.routes.insert(29, Route("/api/protocol", api_protocol_get, methods=["GET"]))
mcp_app.routes.insert(30, Route("/admin/api/protocol", admin_api_protocol_set, methods=["PUT"]))

# Dashboard auth routes — human login system
mcp_app.routes.insert(29, Route("/api/auth/status", api_auth_status, methods=["GET"]))
mcp_app.routes.insert(30, Route("/api/auth/setup", api_auth_setup, methods=["POST"]))
mcp_app.routes.insert(31, Route("/api/auth/login", api_auth_login, methods=["POST"]))
mcp_app.routes.insert(32, Route("/api/auth/logout", api_auth_logout, methods=["POST"]))
mcp_app.routes.insert(33, Route("/api/auth/recover", api_auth_recover, methods=["POST"]))
mcp_app.routes.insert(34, Route("/api/auth/rename-device", api_auth_rename_device, methods=["POST"]))
mcp_app.routes.insert(35, Route("/api/auth/change-password", api_auth_change_password, methods=["POST"]))
mcp_app.routes.insert(36, Route("/api/auth/sessions", api_auth_sessions, methods=["GET"]))
mcp_app.routes.insert(37, Route("/api/auth/revoke", api_auth_revoke, methods=["POST"]))
mcp_app.routes.insert(38, Route("/api/auth/history", api_auth_history, methods=["GET"]))

# Wrap the entire app with OAuth bypass — this is the ASGI entrypoint
# The middleware intercepts /.well-known/* and /register before they
# reach Starlette, guaranteeing JSON 404 responses for OAuth discovery.
# CORS wrapper for LAN access (constellation viewer, external tools)
class CORSMiddleware:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            from starlette.responses import Response as _R
            r = _R(status_code=200, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-API-Key, X-Admin-Key",
                "Access-Control-Max-Age": "86400",
            })
            await r(scope, receive, send)
            return
        async def cors_send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"access-control-allow-origin", b"*"))
                headers.append((b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS"))
                headers.append((b"access-control-allow-headers", b"content-type, x-api-key"))
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, cors_send)

app = CORSMiddleware(MCPKillSwitchMiddleware(OAuthBypassMiddleware(mcp_app)))


if __name__ == "__main__":
    import uvicorn
    print(f"[strata] Starting MCP server on {HOST}:{PORT}")
    print(f"[strata] MCP endpoint:    http://0.0.0.0:{PORT}/mcp")
    print(f"[strata] Health check:    http://0.0.0.0:{PORT}/health")
    print(f"[strata] Conversation log: http://0.0.0.0:{PORT}/log-conversation")
    print(f"[strata] REST capture:    http://0.0.0.0:{PORT}/api/capture")
    print(f"[strata] REST search:     http://0.0.0.0:{PORT}/api/search")
    print(f"[strata] REST startup:    http://0.0.0.0:{PORT}/api/startup-bundle")
    print(f"[strata] REST digest:     http://0.0.0.0:{PORT}/api/digest")
    print(f"[strata] Dashboard:       http://0.0.0.0:{PORT}/dashboard")
    print(f"[strata] Database:        {db.DB_PATH}")
    print(f"[strata] Conversations:   {CONVERSATIONS_DIR}")
    uvicorn.run(app, host=HOST, port=PORT)
