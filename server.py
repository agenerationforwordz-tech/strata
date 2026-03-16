# STRATA - Self-Hosted AI Memory Server
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under PolyForm Noncommercial 1.0.0 - see LICENSE file
#
# The main entry point. Runs as a MCP server exposing tools that any
# AI client (Claude Code, Codex CLI, etc.) can call to capture and search
# your unified memory.
#
# Dual transport: Streamable HTTP (/mcp) for Codex + Claude Code,
# and SSE (/sse) for Claude Code legacy support.
#
# Usage:
#   python server.py
#   # or with virtual environment:
#   ./venv/bin/python server.py

import asyncio
import hmac
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

# Add project dir to path so imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from starlette.routing import Route
from starlette.responses import JSONResponse

from config import (
    HOST, PORT, SERVER_NAME, DEFAULT_SEARCH_LIMIT, DEFAULT_RECENT_LIMIT,
    DEFAULT_RECENT_HOURS, VALID_TYPES, DEDUP_THRESHOLD,
    MAX_CONTENT_LENGTH, MAX_TAGS, MAX_PEOPLE, MAX_TAG_LENGTH, MAX_PERSON_LENGTH,
    API_KEY, AUTH_ENABLED, ADMIN_KEY,
    MAX_FILE_SIZE, MAX_ATTACHMENT_CONTENT, MAX_ATTACHMENTS_PER_THOUGHT,
)
import auth
import db
from write_queue import WriteQueue
import embedder
import vault


# ============================================================
# OAUTH BYPASS MIDDLEWARE
# ============================================================
# Claude Code (and other MCP clients) probe several OAuth discovery
# endpoints before connecting. The MCP sub-apps (SSE, Streamable HTTP)
# return plain text "Not Found" for unmatched paths, which Claude Code
# can't parse as JSON - causing the connection to fail.
#
# This ASGI middleware sits in front of EVERYTHING and intercepts
# well-known / OAuth / register paths. It returns a JSON 404 which
# tells the client "no auth needed, just connect". Requests to
# actual MCP endpoints (/mcp, /sse, /health) pass through untouched.
# ============================================================

class OAuthBypassMiddleware:
    """Intercept OAuth discovery requests and return JSON 404s.

    Without this, paths like /sse/.well-known/oauth-authorization-server
    hit the SSE sub-app which returns plain text 'Not Found', and Claude
    Code chokes trying to parse it as JSON.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only intercept HTTP requests - let WebSocket/lifespan through
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
            # Return JSON 404 - tells MCP clients "no auth required"
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
# AUTO-TAGGING - Lightweight regex-based extraction (no ML, no spaCy)
# ============================================================
# Extracts potential tags from content text using pure regex.
# No external dependencies, no RAM cost, runs instantly.
# These are SUGGESTIONS merged with user-provided tags - manual tags
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

    # #hashtags - people naturally write these in notes
    hashtags = re.findall(r'#(\w+)', content)
    tags.update(h.lower() for h in hashtags)

    # Domains from URLs - extract the domain name as a tag
    urls = re.findall(r'https?://(?:www\.)?([a-zA-Z0-9.-]+)', content)
    for url in urls:
        # Turn "github.com" into "github", "coinbase.com" into "coinbase"
        domain = url.split('.')[0].lower()
        if len(domain) > 2:  # Skip tiny fragments like "io", "co"
            tags.update([domain])

    # Dollar amounts - tag as "financial" if money is mentioned
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
# PROMPT INJECTION DEFENSE - Sanitize stored thoughts for AI clients
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
    Core hygiene routine - inspects stored text, mitigates injection attempts.
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
# RATE LIMITER - Prevent API abuse on REST endpoints
# ============================================================
# Simple in-memory sliding window. Tracks requests per IP per minute.
# Protects the server from being overwhelmed by rapid-fire API calls
# (each capture triggers an embedding generation at ~0.16s on Pi).

_rate_limits = defaultdict(list)
_rate_limit_last_cleanup = 0.0  # Timestamp of last stale-IP cleanup
RATE_LIMIT_PER_MINUTE = 30  # Max requests per minute per IP
RATE_LIMIT_CLEANUP_INTERVAL = 300  # Clean up stale IPs every 5 minutes

# Upper bounds on user-supplied limit/hours params to prevent OOM
MAX_SEARCH_LIMIT = 100  # Cap on any 'limit' parameter
MAX_HOURS = 8760  # 1 year - cap on 'hours' parameter

def check_rate_limit(request):
    """Check if the request IP has exceeded the rate limit.
    Returns None if OK, or a JSONResponse 429 if rate limited.

    Includes periodic cleanup of stale IPs to prevent unbounded
    memory growth in the _rate_limits dict. Without this, every
    unique IP that ever makes a request stays in memory forever
    (even after its timestamps expire)."""
    global _rate_limit_last_cleanup
    ip = request.client.host if request.client else "unknown"
    now = time.time()

    # Periodic cleanup - remove IPs with no recent requests (every 5 minutes)
    # This prevents the dict from growing unbounded with stale IP keys
    if now - _rate_limit_last_cleanup > RATE_LIMIT_CLEANUP_INTERVAL:
        stale_ips = [k for k, v in _rate_limits.items() if not v or now - v[-1] > 120]
        for k in stale_ips:
            del _rate_limits[k]
        _rate_limit_last_cleanup = now

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
# "127.0.0.1" which auto-enables DNS rebinding protection - blocking
# all non-localhost requests with 421 "Invalid Host header". Since the
# server is accessed over LAN (10.0.0.x), we need host="0.0.0.0" to
# disable that auto-protection.
mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)

# Initialize database tables on import
db.init_db()

# Write queue — serializes all DB writes through a single thread to prevent
# "database is locked" errors when multiple agents write simultaneously.
# Reads bypass the queue entirely (SQLite WAL handles concurrent readers).
_wq = WriteQueue(timeout=60)
auth.init_auth_tables()

# Warn if admin key isn't configured - delete operations will be disabled
if not ADMIN_KEY:
    print("[strata] WARNING: STRATA_ADMIN_KEY not set. Delete operations are disabled until configured.")


# ============================================================
# MCP TOOLS - These are what Claude Code / Codex see and call
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
    trigger: str = "unknown"
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
        thought_type: Category - one of: thought, decision, session,
                      person, insight, project, instruction, reference
        tags: Optional list of tags for filtering (e.g. ["carpi", "hardware"])
        people: Optional list of people mentioned (e.g. ["Chris", "Sarah"])
        source: Where this came from - claude-code, codex, telegram, manual, migration
        force: Set to True to skip dedup check and save even if similar thoughts exist
        machine: Which device uploaded this - surface, helios, telegram, pi-nas, etc.
        trigger: How capture was initiated - auto (Claude decided), requested (Chris asked), manual (typed directly)

    Returns:
        Confirmation message with the thought ID, or dedup warning
    """
    # Validate type
    if thought_type not in VALID_TYPES:
        thought_type = "thought"

    # SAFETY: Never mutate shared default args - always work on fresh copies.
    # Python's mutable default (list=[]) is shared across all calls, so
    # without this copy, tags from previous calls would bleed into new ones.
    tags = list(tags) if tags else []
    people = list(people) if people else []

    # Strip whitespace and reject empty content - prevents storing blank thoughts
    content = content.strip()
    if not content:
        return "Content cannot be empty."

    # INPUT LIMITS: Prevent oversized payloads from crashing the server.
    # Large files should be stored on disk and referenced by path, not inlined.
    if len(content) > MAX_CONTENT_LENGTH:
        return f"Content too long ({len(content)} chars, max {MAX_CONTENT_LENGTH}). Store large files on disk and reference by path."
    tags = [t[:MAX_TAG_LENGTH] for t in tags[:MAX_TAGS] if isinstance(t, str)]
    people = [p[:MAX_PERSON_LENGTH] for p in people[:MAX_PEOPLE] if isinstance(p, str)]
    # Cap free-text fields - unbounded strings here could store 100MB+ per thought
    source = str(source)[:100] if source else "manual"
    machine = str(machine)[:100] if machine else "unknown"
    trigger = str(trigger)[:100] if trigger else "unknown"

    # AUTO-TAGGING: Extract potential tags from content via regex
    # These get merged with user-provided tags - manual tags always take priority.
    # This adds tags the user might forget (e.g., #hashtags, crypto mentions, domains)
    auto_tags = auto_extract_tags(content)
    existing_lower = {t.lower() for t in tags}
    # Only add auto-tags that aren't already in the user's list (avoid duplicates)
    for at in auto_tags:
        if at not in existing_lower:
            tags.append(at)
            existing_lower.add(at)

    # Generate the embedding - this is where the meaning gets captured
    # Takes ~2-3 seconds on Pi, model loads on-demand if not already in memory
    embedding = embedder.embed_text(content)

    # DEDUP CHECK - look for near-duplicates before saving
    # Skip this check if force=True (caller knows what they're doing)
    if not force:
        duplicates = db.find_duplicates(embedding, threshold=DEDUP_THRESHOLD)
        if duplicates:
            # Build a warning message showing the similar existing thoughts
            warning = f"DUPLICATE WARNING: Found {len(duplicates)} similar thought(s):\n"
            for dupe in duplicates[:3]:  # Show top 3 matches max
                warning += f" - Thought #{dupe['id']} ({dupe['similarity']:.0%} similar): {dupe['preview']}\n"
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
        trigger=trigger
    )

    return f"Stored thought #{thought_id} (type={thought_type}, tags={tags}, machine={machine}, trigger={trigger})"


@mcp.tool()
def semantic_search(query: str, limit: int = 10, threshold: float = 0.0) -> str:
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
    limit = min(limit, MAX_SEARCH_LIMIT)  # Cap to prevent OOM on huge requests
    # Cap query length - long queries block the embedder lock, stalling all requests
    if len(query) > 5000:
        return "Query too long (max 5000 characters)."

    # Embed the search query into the same vector space as stored thoughts
    query_embedding = embedder.embed_text(query)

    # Find the closest matches by cosine distance, filtered by threshold
    results = db.search_similar(query_embedding, limit=limit, threshold=threshold)

    if not results:
        return "No matching thoughts found."

    # Track which thoughts got accessed - builds the "heat map" over time
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def list_recent(limit: int = 20, hours: int = 168) -> str:
    """Browse the most recent thoughts/memories within a time window.

    Good for questions like "what was I capturing this week?" or
    "show me my last 5 thoughts". Default window is 7 days.

    Args:
        limit: Max results to return (default 20)
        hours: Look back this many hours (default 168 = 7 days)

    Returns:
        JSON string of recent thoughts, newest first
    """
    if limit < 1:
        limit = DEFAULT_RECENT_LIMIT
    limit = min(limit, MAX_SEARCH_LIMIT)  # Cap to prevent OOM
    if hours < 1:
        hours = DEFAULT_RECENT_HOURS
    hours = min(hours, MAX_HOURS)  # Cap at 1 year

    results = db.list_recent(limit=limit, hours=hours)

    if not results:
        return "No thoughts captured in the last %d hours." % hours

    sanitize_results(results)
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
def update_thought(
    thought_id: int,
    content: str = None,
    thought_type: str = None,
    tags: list[str] = None,
    people: list[str] = None,
) -> str:
    """Update an existing thought in the STRATA database.

    Only the fields you provide will be changed - everything else
    stays the same. If you change the content, the embedding is
    automatically regenerated so semantic search stays accurate.

    Args:
        thought_id: The ID of the thought to update (required)
        content: New text content (leave empty to keep existing)
        thought_type: New category (thought, decision, session, etc.)
        tags: New tags list (replaces all existing tags)
        people: New people list (replaces all existing people)

    Returns:
        Confirmation message or error if thought not found
    """
    # Validate type if provided
    if thought_type is not None and thought_type not in VALID_TYPES:
        return f"Invalid type '{thought_type}'. Valid types: {', '.join(VALID_TYPES)}"

    # INPUT LIMITS: Same limits as capture_thought - update_thought shouldn't
    # be a backdoor to bypass size restrictions.
    if content is not None:
        content = content.strip()
        if not content:
            return "Content cannot be empty."
        if len(content) > MAX_CONTENT_LENGTH:
            return f"Content too long ({len(content)} chars, max {MAX_CONTENT_LENGTH})."
    if tags is not None:
        tags = [t[:MAX_TAG_LENGTH] for t in tags[:MAX_TAGS] if isinstance(t, str)]
    if people is not None:
        people = [p[:MAX_PERSON_LENGTH] for p in people[:MAX_PEOPLE] if isinstance(p, str)]

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
        new_embedding=new_embedding
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

    return f"Updated thought #{thought_id}: {', '.join(changed)}"


@mcp.tool()
def delete_thought(thought_id: int, admin_key: str = "") -> str:
    """Permanently delete a thought from the STRATA database.

    ADMIN ONLY - requires the admin_key to execute. AI clients cannot
    delete thoughts on their own. The human owner must provide the key
    and explicitly instruct deletion.

    Removes the thought, its embedding, its search index entry,
    AND all attached files in the vault. This is irreversible.

    Args:
        thought_id: The ID of the thought to delete
        admin_key: The admin key (required - ask the human owner for it)

    Returns:
        Confirmation message or error if thought not found
    """
    # ADMIN GATE - AI cannot delete without human providing the key
    if not ADMIN_KEY:
        return "DELETE BLOCKED: Admin key not configured. Set STRATA_ADMIN_KEY environment variable."
    if not admin_key or not hmac.compare_digest(str(admin_key), ADMIN_KEY):
        return (
            "DELETE BLOCKED: This is an admin-only action. "
            "The human owner must provide the admin_key to authorize deletion. "
            "AI clients are not allowed to delete thoughts without explicit human instruction."
        )

    # First fetch the thought so we can show what was deleted
    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return f"Thought #{thought_id} not found."

    # Show a preview of what's being deleted (first 100 chars)
    preview = thought["content"][:100]
    if len(thought["content"]) > 100:
        preview += "..."

    # Atomic delete - thought + attachments removed in single DB transaction.
    # Vault files are cleaned up AFTER the DB commit succeeds.
    success, vault_paths = _wq.submit(db.delete_thought_full, thought_id)
    if not success:
        return f"Failed to delete thought #{thought_id}."

    # Clean up vault files (non-critical - orphaned files waste space but don't break anything)
    files_deleted = 0
    for vp in vault_paths:
        try:
            if vault.delete_file(vp):
                files_deleted += 1
        except Exception:
            pass  # File might already be gone

    msg = f"Deleted thought #{thought_id}: \"{preview}\""
    if files_deleted:
        msg += f" (and {files_deleted} attached file(s) from vault)"
    return msg


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
    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return f"Thought #{thought_id} not found."

    # Track that this specific thought was accessed
    _wq.submit_fire_and_forget(db.record_access, [thought_id])

    # Sanitize single thought - wrap content for AI safety
    if "content" in thought:
        thought["content"] = sanitize_for_ai(thought["content"])
    return json.dumps(thought, indent=2, default=str)


@mcp.tool()
def search_by_tag(tag: str, limit: int = 20) -> str:
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
    limit = min(max(limit, 1), MAX_SEARCH_LIMIT)  # Clamp to 1..100
    results = db.search_by_tag(tag, limit=limit)

    if not results:
        return f"No thoughts found with tag '{tag}'."

    # Track access for returned thoughts
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def search_by_person(person: str, limit: int = 20) -> str:
    """Find all memories that mention a specific person.

    Searches the people field with case-insensitive partial matching.
    "chris" will find thoughts tagged with "Chris Mitchell".

    Args:
        person: Name to search for (partial match, case-insensitive)
        limit: Max results to return (default 20)

    Returns:
        JSON string of matching thoughts
    """
    limit = min(max(limit, 1), MAX_SEARCH_LIMIT)  # Clamp to 1..100
    results = db.search_by_person(person, limit=limit)

    if not results:
        return f"No thoughts found mentioning '{person}'."

    # Track access for returned thoughts
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    return json.dumps(results, indent=2, default=str)


# get_relevant_context - available via extensions.py (not included in public repo)
# See README for details on the extension system.


# ============================================================
# ADDITIONAL TOOLS
# ============================================================

@mcp.tool()
def find_related(thought_id: int, limit: int = 5) -> str:
    """Find thoughts similar to an existing thought - "more like this."

    Instead of searching by text, this takes a thought you already have
    and finds its nearest neighbors by vector similarity. No embedding
    generation needed - uses the thought's stored vector directly.

    Great for exploring connections: "I liked this idea, what else
    is related?" or "This decision connects to what other decisions?"

    Args:
        thought_id: The ID of the thought to find relatives for
        limit: Max results to return (default 5)

    Returns:
        JSON string of similar thoughts (excluding the source thought)
    """
    # First verify the thought exists
    source = db.get_thought_by_id(thought_id)
    if not source:
        return f"Thought #{thought_id} not found."

    limit = min(max(limit, 1), MAX_SEARCH_LIMIT)  # Clamp to 1..100
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
    return json.dumps(response, indent=2, default=str)


@mcp.tool()
def hybrid_search(query: str, limit: int = 10, keyword_weight: float = 0.3, threshold: float = 0.0) -> str:
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
    if limit < 1:
        limit = DEFAULT_SEARCH_LIMIT
    limit = min(limit, MAX_SEARCH_LIMIT)  # Cap to prevent OOM
    # Clamp keyword_weight to valid range - values outside 0-1 produce nonsensical rankings
    keyword_weight = max(0.0, min(1.0, keyword_weight))
    # Cap query length
    if len(query) > 5000:
        return "Query too long (max 5000 characters)."

    # Generate embedding for the semantic half of the search
    query_embedding = embedder.embed_text(query)

    # Run the blended search - keyword scores from FTS5, vector scores from numpy
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
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def search_advanced(
    tag: str = "",
    person: str = "",
    thought_type: str = "",
    source: str = "",
    machine: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 20,
) -> str:
    """Multi-filter search - combine any filters in one query.

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
        date_from: Only thoughts created on or after this date (ISO format: YYYY-MM-DD)
        date_to: Only thoughts created on or before this date (ISO format: YYYY-MM-DD)
        limit: Max results to return (default 20)

    Returns:
        JSON string of matching thoughts, newest first
    """
    # Build the filters dict - only include non-empty values
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
    if date_from:
        filters["date_from"] = date_from
    if date_to:
        filters["date_to"] = date_to

    if not filters:
        return "At least one filter is required. Provide tag, person, type, source, machine, date_from, or date_to."

    limit = min(max(limit, 1), MAX_SEARCH_LIMIT)  # Clamp to 1..100
    results = db.search_advanced(filters, limit=limit)

    if not results:
        return f"No thoughts found matching filters: {filters}"

    # Track access
    _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])

    sanitize_results(results)
    return json.dumps(results, indent=2, default=str)


# generate_report - available via extensions.py (not included in public repo)
# The /api/report REST endpoint below still works for the dashboard.


# ============================================================
# FILE VAULT TOOLS - Attach, retrieve, and manage files
# ============================================================
# These tools turn Strata from a text memory into a full
# knowledge vault. Thoughts are the semantic index; the vault
# holds the actual files: code, documents, project archives.
#
# Flow: capture_thought → attach_file → (later) get_thought shows files
#       → get_file reads the actual content on demand

@mcp.tool()
def attach_file(
    thought_id: int,
    filename: str,
    content: str,
    content_type: str = "text",
    device: str = "unknown",
) -> str:
    """Attach a file to an existing thought in the vault.

    This stores the actual file content on the Strata server's disk
    and links it to the thought. When any AI later retrieves this thought,
    they'll see the attachment list and can pull the file content with get_file.

    For TEXT files (code, markdown, config, etc.): pass the content directly
    as a string. Set content_type="text" (the default).

    For BINARY files (images, archives, PDFs): base64-encode the content
    first. Set content_type="base64".

    Files up to ~50MB can be sent via MCP. For larger files, use the
    REST upload endpoint: POST /api/vault/upload

    Args:
        thought_id: The thought to attach this file to (must exist)
        filename: The filename (e.g., "server.py", "architecture.png")
        content: The file content - plain text or base64-encoded string
        content_type: "text" for text files (default), "base64" for binary
        device: Which device is uploading (surface, helios, pi-nas, etc.)

    Returns:
        Confirmation with attachment ID, file size, and vault path
    """
    # Verify thought exists
    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return f"Thought #{thought_id} not found. Capture a thought first, then attach files."

    # Check attachment count limit
    current_count = db.count_attachments(thought_id)
    if current_count >= MAX_ATTACHMENTS_PER_THOUGHT:
        return f"Thought #{thought_id} already has {current_count} attachments (max {MAX_ATTACHMENTS_PER_THOUGHT})."

    # Sanitize inputs
    device = str(device)[:100] if device else "unknown"

    try:
        # Store the file in the vault - text or base64
        if content_type == "base64":
            result = vault.store_from_base64(
                thought_id=thought_id,
                filename=filename,
                base64_content=content,
                device=device,
                created_at=thought.get("created_at"),
            )
        else:
            result = vault.store_from_text(
                thought_id=thought_id,
                filename=filename,
                text_content=content,
                device=device,
                created_at=thought.get("created_at"),
            )

        # Record the attachment in the database
        attachment_id = _wq.submit(
            db.store_attachment,
            thought_id=thought_id,
            vault_path=result["vault_path"],
            filename=result["filename"],
            file_size=result["file_size"],
            mime_type=result["mime_type"],
            checksum=result["checksum"],
            device=device
        )

        # Format human-readable size
        size_str = vault._human_size(result["file_size"])

        return (
            f"Attached file to thought #{thought_id}:\n"
            f"  Attachment ID: {attachment_id}\n"
            f"  Filename: {result['filename']}\n"
            f"  Size: {size_str}\n"
            f"  Type: {result['mime_type']}\n"
            f"  Vault path: {result['vault_path']}\n"
            f"  Checksum: {result['checksum'][:16]}..."
        )

    except ValueError as e:
        return f"Failed to attach file: {str(e)}"
    except Exception as e:
        return f"Error storing file: {str(e)}"


@mcp.tool()
def get_file(thought_id: int, filename: str = "") -> str:
    """Read the content of a file attached to a thought.

    For text files (code, markdown, config), returns the content directly
    as a readable string. For binary files, returns base64-encoded content.
    Text files are capped at 5MB to avoid blowing up AI context windows.

    If the thought has multiple attachments, specify the filename to pick
    which one. If there's only one attachment, it returns that automatically.

    Args:
        thought_id: The thought that has the file attached
        filename: Which file to read (optional if thought has only one attachment)

    Returns:
        JSON string with file content, metadata, and vault path
    """
    # Get attachments for this thought
    attachments = db.get_attachments(thought_id)
    if not attachments:
        return f"Thought #{thought_id} has no attached files."

    # Find the right attachment
    target = None
    if filename:
        # Match by filename (case-insensitive)
        for att in attachments:
            if att["filename"].lower() == filename.lower():
                target = att
                break
        if not target:
            available = ", ".join(a["filename"] for a in attachments)
            return f"File '{filename}' not found on thought #{thought_id}. Available: {available}"
    else:
        if len(attachments) == 1:
            target = attachments[0]
        else:
            available = ", ".join(a["filename"] for a in attachments)
            return f"Thought #{thought_id} has {len(attachments)} files. Specify which one: {available}"

    # Read the file from the vault
    try:
        file_data = vault.read_file(target["vault_path"])
    except FileNotFoundError:
        return f"File exists in DB but missing from vault: {target['vault_path']}"
    except ValueError as e:
        return f"Security error: {str(e)}"

    # Build response
    response = {
        "thought_id": thought_id,
        "attachment_id": target["id"],
        "filename": file_data["filename"],
        "mime_type": file_data["mime_type"],
        "file_size": file_data["file_size"],
        "is_text": file_data["is_text"],
        "vault_path": file_data["vault_path"],
    }

    if file_data["content"] is not None:
        response["content"] = file_data["content"]
    else:
        response["content"] = None
        response["note"] = "File too large to inline. Access via REST: GET /api/vault/file/" + target["vault_path"]

    return json.dumps(response, indent=2, default=str)


@mcp.tool()
def list_attachments(thought_id: int) -> str:
    """List all files attached to a thought.

    Shows filename, size, type, and device for each attachment.
    Use get_file to read the actual content of any listed file.

    Args:
        thought_id: The thought to list attachments for

    Returns:
        JSON string with attachment metadata list
    """
    # Verify thought exists
    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return f"Thought #{thought_id} not found."

    attachments = db.get_attachments(thought_id)
    if not attachments:
        return f"Thought #{thought_id} has no attached files."

    # Add human-readable sizes
    for att in attachments:
        att["size_human"] = vault._human_size(att["file_size"])

    return json.dumps({
        "thought_id": thought_id,
        "thought_preview": thought["content"][:100],
        "total_files": len(attachments),
        "total_size": vault._human_size(sum(a["file_size"] for a in attachments)),
        "attachments": attachments,
    }, indent=2, default=str)


@mcp.tool()
def detach_file(thought_id: int, filename: str, admin_key: str = "") -> str:
    """Remove a file attachment from a thought.

    ADMIN ONLY - requires the admin_key to execute. AI clients cannot
    delete files on their own. The human owner must provide the key
    and explicitly instruct file removal.

    Deletes the file from the vault AND removes the DB record.
    The thought itself is not affected - only the attachment is removed.

    Args:
        thought_id: The thought the file is attached to
        filename: Which file to remove
        admin_key: The admin key (required - ask the human owner for it)

    Returns:
        Confirmation or error message
    """
    # ADMIN GATE - AI cannot delete files without human providing the key
    if not ADMIN_KEY:
        return "DELETE BLOCKED: Admin key not configured. Set STRATA_ADMIN_KEY environment variable."
    if not admin_key or not hmac.compare_digest(str(admin_key), ADMIN_KEY):
        return (
            "DELETE BLOCKED: This is an admin-only action. "
            "The human owner must provide the admin_key to authorize file removal. "
            "AI clients are not allowed to delete files without explicit human instruction."
        )

    attachments = db.get_attachments(thought_id)
    if not attachments:
        return f"Thought #{thought_id} has no attached files."

    # Find the attachment by filename
    target = None
    for att in attachments:
        if att["filename"].lower() == filename.lower():
            target = att
            break

    if not target:
        available = ", ".join(a["filename"] for a in attachments)
        return f"File '{filename}' not found on thought #{thought_id}. Available: {available}"

    # Delete the file from vault
    try:
        vault.delete_file(target["vault_path"])
    except Exception:
        pass  # File might already be gone - still clean up the DB record

    # Delete the DB record
    _wq.submit(db.delete_attachment, target["id"])

    return f"Detached '{filename}' from thought #{thought_id} (attachment #{target['id']}, {vault._human_size(target['file_size'])} freed)"


# ============================================================
# AUTHENTICATION - API key check for REST endpoints
# ============================================================
# MCP transport (/mcp) handles its own auth. These REST endpoints
# (/api/capture, /api/search) need protection so random devices
# on the network can't read/write your brain.
#
# Send the key as a header:   X-API-Key: your-key-here
# Or as a Bearer token:       Authorization: Bearer your-key-here

def check_auth(request):
    """Verify credentials from request headers.

    Accepts THREE methods (checked in order):
    1. X-API-Key header - for AI clients (Claude Code, Codex, bots, scripts)
    2. Authorization: Bearer <API_KEY> - for AI clients (standard method)
    3. Authorization: Bearer <session_token> - for dashboard users (human login)

    Returns None if auth passes, or a JSONResponse 401 if it fails.
    If AUTH_ENABLED is False, always passes (for dev/testing).
    """
    if not AUTH_ENABLED:
        return None  # Auth disabled - let everything through

    # Method 1: X-API-Key header (simplest method for AI clients)
    # SECURITY: Use hmac.compare_digest for constant-time comparison.
    # Plain == short-circuits on first mismatched byte, leaking key length
    # and prefix via timing side-channel. compare_digest always takes the
    # same time regardless of where the mismatch occurs.
    api_key = request.headers.get("x-api-key", "")
    if api_key and hmac.compare_digest(api_key, API_KEY):
        return None  # Auth passed - AI client

    # Method 2 & 3: Authorization: Bearer <token>
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # Try API key first (AI client using Bearer format)
        if hmac.compare_digest(token, API_KEY):
            return None  # Auth passed - AI client
        # Try dashboard session token (human user)
        session = auth.validate_session(token)
        if session:
            return None  # Auth passed - dashboard user

    return JSONResponse(
        {"status": "error", "error": "Unauthorized. Provide X-API-Key header or Authorization: Bearer token."},
        status_code=401
    )


# ============================================================
# HTTP ENDPOINTS - Health check, REST API, MCP
# ============================================================

async def health_check(request):
    """Health endpoint for monitoring.

    Returns minimal info publicly (just status + server name).
    Detailed stats (thought count, DB size, model status) are only
    shown if the request includes a valid API key - prevents info
    leakage about your brain's size/activity to unauthenticated callers."""
    response = {
        "status": "ok",
        "server": SERVER_NAME,
        "timestamp": datetime.now().isoformat(),
    }

    # Only show detailed stats if authenticated - don't leak DB info publicly
    auth_fail = check_auth(request)
    if auth_fail is None:
        stats = db.get_stats()
        response["model_loaded"] = embedder.is_loaded()
        response["total_thoughts"] = stats["total_thoughts"]
        response["db_size_mb"] = stats["db_size_mb"]
        response["write_queue"] = _wq.stats

    return JSONResponse(response)


async def api_capture(request):
    """REST endpoint for quick thought capture from bots, scripts, and webhooks.

    This is the simple HTTP alternative to the MCP capture_thought tool.
    Any client that can POST JSON can capture a thought - no MCP session needed.

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
    # Auth check - protect against unauthorized writes to your brain
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    # Rate limit - prevent embedding DoS (each capture takes ~0.16s on Pi)
    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    # Reject oversized request bodies BEFORE reading into memory.
    # Without this, a 2GB POST gets fully buffered and OOMs the Pi.
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > MAX_CONTENT_LENGTH + 1024:
            return JSONResponse({"status": "error", "error": "Request body too large"}, status_code=413)
    except (ValueError, TypeError):
        return JSONResponse({"status": "error", "error": "Invalid Content-Length header"}, status_code=400)

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

    # Enforce limits on tags and people - truncate silently, don't reject
    tags = data.get("tags", [])
    tags = [t[:MAX_TAG_LENGTH] for t in tags[:MAX_TAGS] if isinstance(t, str)]
    people = data.get("people", [])
    people = [p[:MAX_PERSON_LENGTH] for p in people[:MAX_PEOPLE] if isinstance(p, str)]
    source = str(data.get("source", "api"))[:100]
    force = data.get("force", False)
    machine = str(data.get("machine", "unknown"))[:100]
    trigger = str(data.get("trigger", "manual"))[:100]

    # Generate embedding - run in executor so it doesn't block the async event loop.
    # embed_text takes ~0.16s on Pi which would stall all other requests if run inline.
    loop = asyncio.get_running_loop()
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

    # Store the thought
    thought_id = _wq.submit(
        db.store_thought,
        content=content,
        embedding=embedding_val,
        thought_type=thought_type,
        tags=tags,
        people=people,
        source=source,
        machine=machine,
        trigger=trigger
    )

    return JSONResponse({
        "status": "ok",
        "thought_id": thought_id,
        "type": thought_type,
        "tags": tags,
        "source": source,
        "machine": machine,
        "trigger": trigger,
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
    # Auth check - protect against unauthorized reads of your brain
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    # Rate limit - prevent search spam (each query triggers embedding generation)
    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    # Reject oversized request bodies before reading into memory
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > MAX_CONTENT_LENGTH + 1024:
            return JSONResponse({"status": "error", "error": "Request body too large"}, status_code=413)
    except (ValueError, TypeError):
        return JSONResponse({"status": "error", "error": "Invalid Content-Length header"}, status_code=400)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    query = data.get("query", "").strip()
    if not query:
        return JSONResponse({"status": "error", "error": "Query is required"}, status_code=400)
    if len(query) > 5000:
        return JSONResponse({"status": "error", "error": "Query too long (max 5000 chars)"}, status_code=400)

    limit = min(max(data.get("limit", 5), 1), MAX_SEARCH_LIMIT)  # Clamp to 1..100
    # Run embedding in executor - don't block the event loop for ~0.16s
    loop = asyncio.get_running_loop()
    query_embedding = await loop.run_in_executor(None, embedder.embed_text, query)
    results = db.search_similar(query_embedding, limit=limit)

    # Track access
    if results:
        _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])
        sanitize_results(results)

    return JSONResponse({
        "status": "ok",
        "count": len(results),
        "results": results,
    })


async def api_search_by_tag(request):
    """REST endpoint for searching thoughts by tag.

    POST /api/search/tag
    {
        "tag": "carpi",       (required)
        "limit": 20           (optional, default 20)
    }

    Requires auth. Returns matching thoughts.
    """
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    tag = data.get("tag", "").strip()
    if not tag:
        return JSONResponse({"status": "error", "error": "Tag is required"}, status_code=400)

    limit = min(max(data.get("limit", 20), 1), MAX_SEARCH_LIMIT)
    results = db.search_by_tag(tag, limit=limit)

    if results:
        _wq.submit_fire_and_forget(db.record_access, [r["id"] for r in results])
        sanitize_results(results)

    return JSONResponse({
        "status": "ok",
        "tag": tag,
        "count": len(results),
        "results": results,
    })


async def api_thought_detail(request):
    """REST endpoint for getting a single thought with full content and attachments.

    GET /api/thought/{thought_id}

    Returns the full thought including content, tags, people, access stats,
    and a list of all attached files with download URLs.
    Used by the dashboard detail view - gives humans the same data AI gets.

    Requires X-API-Key header or Authorization: Bearer token.
    """
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    thought_id = request.path_params.get("thought_id")
    try:
        thought_id = int(thought_id)
    except (ValueError, TypeError):
        return JSONResponse({"status": "error", "error": "Invalid thought_id"}, status_code=400)

    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return JSONResponse({"status": "error", "error": f"Thought #{thought_id} not found"}, status_code=404)

    _wq.submit_fire_and_forget(db.record_access, [thought_id])

    # Don't wrap content in AI data markers - this is for human eyes
    # But still escape any HTML when rendering (dashboard handles that)

    return JSONResponse({
        "status": "ok",
        "thought": thought,
    })


async def api_vault_upload(request):
    """REST endpoint for uploading files to the vault (for large files).

    POST /api/vault/upload
    Content-Type: application/json
    {
        "thought_id": 42,
        "filename": "project.zip",
        "content_base64": "<base64-encoded-content>",
        "device": "surface"
    }

    For files too large for MCP (>50MB), use this endpoint.
    Max file size: 1GB.

    Requires X-API-Key header or Authorization: Bearer token.
    """
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    # Allow larger bodies for file uploads - up to ~1.4GB (base64 overhead)
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > MAX_FILE_SIZE * 1.4:
            return JSONResponse({"status": "error", "error": "File too large (max 1GB)"}, status_code=413)
    except (ValueError, TypeError):
        return JSONResponse({"status": "error", "error": "Invalid Content-Length header"}, status_code=400)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    thought_id = data.get("thought_id")
    filename = data.get("filename", "")
    content_b64 = data.get("content_base64", "")
    device = str(data.get("device", "unknown"))[:100]

    if not thought_id or not filename or not content_b64:
        return JSONResponse(
            {"status": "error", "error": "Required: thought_id, filename, content_base64"},
            status_code=400
        )

    # Verify thought exists
    thought = db.get_thought_by_id(thought_id)
    if not thought:
        return JSONResponse({"status": "error", "error": f"Thought #{thought_id} not found"}, status_code=404)

    # Check attachment limit
    current_count = db.count_attachments(thought_id)
    if current_count >= MAX_ATTACHMENTS_PER_THOUGHT:
        return JSONResponse(
            {"status": "error", "error": f"Attachment limit reached ({MAX_ATTACHMENTS_PER_THOUGHT})"},
            status_code=400
        )

    try:
        result = vault.store_from_base64(
            thought_id=thought_id,
            filename=filename,
            base64_content=content_b64,
            device=device,
            created_at=thought.get("created_at"),
        )

        attachment_id = _wq.submit(
            db.store_attachment,
            thought_id=thought_id,
            vault_path=result["vault_path"],
            filename=result["filename"],
            file_size=result["file_size"],
            mime_type=result["mime_type"],
            checksum=result["checksum"],
            device=device
        )

        return JSONResponse({
            "status": "ok",
            "attachment_id": attachment_id,
            "thought_id": thought_id,
            "filename": result["filename"],
            "file_size": result["file_size"],
            "vault_path": result["vault_path"],
            "checksum": result["checksum"],
        })

    except ValueError as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "error": f"Upload failed: {str(e)}"}, status_code=500)


async def api_stats(request):
    """REST endpoint for database statistics.

    GET /api/stats

    Returns total thoughts, type/source/machine breakdown, top tags,
    top people, db size, and vault stats. Used by the dashboard.

    Requires X-API-Key header or Authorization: Bearer token.
    """
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    stats = db.get_stats()
    return JSONResponse({"status": "ok", **stats})


async def api_recent(request):
    """REST endpoint for recent thoughts.

    GET /api/recent?limit=30&hours=8760&offset=0

    Returns recent thoughts, newest first. Used by the dashboard feed.
    Supports offset for infinite scroll pagination.

    Requires X-API-Key header or Authorization: Bearer token.
    """
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    try:
        limit = min(max(int(request.query_params.get("limit", 20)), 1), MAX_SEARCH_LIMIT)
    except (ValueError, TypeError):
        limit = 20
    try:
        hours = min(max(int(request.query_params.get("hours", 168)), 1), MAX_HOURS)
    except (ValueError, TypeError):
        hours = 168
    try:
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except (ValueError, TypeError):
        offset = 0

    results = db.list_recent(limit=limit, hours=hours, offset=offset)
    sanitize_results(results)

    return JSONResponse({
        "status": "ok",
        "count": len(results),
        "offset": offset,
        "results": results,
    })


async def api_report(request):
    """REST endpoint for trend report.

    GET /api/report?days=7

    Returns trending tags, hottest memories, activity by machine/source.
    Used by the dashboard sidebar panels.

    Requires X-API-Key header or Authorization: Bearer token.
    """
    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    try:
        days = min(max(int(request.query_params.get("days", 7)), 1), 365)
    except (ValueError, TypeError):
        days = 7

    report = db.generate_report(days=days)
    return JSONResponse({"status": "ok", **report})


# ============================================================
# DASHBOARD AUTH ENDPOINTS - Account setup, login, recovery
# ============================================================
# These are SEPARATE from the API key auth used by AI clients.
# The API key still works exactly the same for MCP/REST - these
# endpoints add a password-based login for human users on the
# web dashboard, with seed phrase recovery and session management.

# Auth requests are small JSON payloads - 100KB is extremely generous.
# This prevents a 2GB POST from being buffered into memory and OOMing the Pi.
AUTH_MAX_BODY = 100_000

def _check_auth_body_size(request):
    """Reject oversized request bodies on auth endpoints before parsing JSON."""
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > AUTH_MAX_BODY:
            return JSONResponse({"status": "error", "error": "Request body too large"}, status_code=413)
    except (ValueError, TypeError):
        return JSONResponse({"status": "error", "error": "Invalid Content-Length"}, status_code=400)
    return None

async def auth_status(request):
    """Check if initial setup is done and if the current session is valid.

    GET /api/auth/status

    No auth required (chicken-and-egg: need this to know HOW to auth).
    Returns whether setup is complete and whether the request has a valid session.
    """
    setup_done = auth.is_setup_complete()

    # Check if the request has a valid session token
    session_valid = False
    device_name = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        session = auth.validate_session(auth_header[7:])
        if session:
            session_valid = True
            device_name = session.get("device_name", "")

    return JSONResponse({
        "setup_complete": setup_done,
        "authenticated": session_valid,
        "device_name": device_name,
    })


async def auth_setup(request):
    """First-time account setup. Creates the owner account.

    POST /api/auth/setup
    {
        "password": "my-password",
        "device_name": "my-laptop"    (optional)
    }

    Only works ONCE - after the account exists, returns an error.
    Returns the 12-word recovery seed phrase. THIS IS THE ONLY TIME
    the seed phrase is shown. The user MUST write it down.
    """
    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    if auth.is_setup_complete():
        return JSONResponse(
            {"status": "error", "error": "Account already exists. Use login instead."},
            status_code=400,
        )

    size_fail = _check_auth_body_size(request)
    if size_fail:
        return size_fail
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    password = data.get("password", "").strip()
    device_name = data.get("device_name", "").strip()[:100]

    if not password or len(password) < 6:
        return JSONResponse(
            {"status": "error", "error": "Password must be at least 6 characters."},
            status_code=400,
        )

    # If device name already taken during setup, just clear it — setup only runs once
    # and shouldn't block on stale session data
    if device_name and auth.is_device_name_taken(device_name):
        auth.refresh_session_by_device(device_name, 1)  # clears old session

    seed_phrase, error = auth.setup_account(password, device_name)
    if error:
        return JSONResponse({"status": "error", "error": error}, status_code=400)

    # Auto-login after setup
    user = auth.login(password)
    token = auth.create_session(user["id"], device_name, days=30)

    return JSONResponse({
        "status": "ok",
        "seed_phrase": seed_phrase,
        "token": token,
    })


async def auth_login(request):
    """Login with password.

    POST /api/auth/login
    {
        "password": "my-password",
        "device_name": "my-phone",    (optional - labels this device)
        "remember_days": 30           (optional - 1 to 365, default 30)
    }

    Returns a session token to store in the browser.
    """
    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    size_fail = _check_auth_body_size(request)
    if size_fail:
        return size_fail
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    password = data.get("password", "").strip()
    device_name = data.get("device_name", "").strip()[:100]
    try:
        remember = min(max(int(data.get("remember_days", 30)), 1), 365)
    except (ValueError, TypeError):
        remember = 30

    if not password:
        return JSONResponse({"status": "error", "error": "Password required."}, status_code=400)

    user = auth.login(password)
    if not user:
        # Record failed login attempt (no user_id since auth failed)
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        auth.record_login(None, device_name or "unknown", ip, ua, success=False)
        return JSONResponse({"status": "error", "error": "Invalid password."}, status_code=401)

    # Auto-detect device name from User-Agent if not provided.
    # Users shouldn't have to name their device — we figure it out.
    if not device_name:
        ua = request.headers.get("user-agent", "")
        device_name = auth.parse_user_agent(ua)

    # If this device name already has an active session, refresh it
    # instead of rejecting — same user, same device, new token.
    if auth.is_device_name_taken(device_name):
        token = auth.refresh_session_by_device(device_name, user["id"], days=remember)
    else:
        token = auth.create_session(user["id"], device_name, days=remember)

    # Record successful login with IP and user-agent
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    auth.record_login(user["id"], device_name, ip, ua, success=True)

    return JSONResponse({
        "status": "ok",
        "token": token,
        "device_name": device_name,
        "expires_in_days": remember,
    })


async def auth_change_password(request):
    """Change password. Requires current password.

    POST /api/auth/change-password
    {
        "old_password": "current",
        "new_password": "new-one"
    }

    Requires a valid session token in Authorization header.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer ") or not auth.validate_session(auth_header[7:]):
        return JSONResponse({"status": "error", "error": "Not authenticated."}, status_code=401)

    size_fail = _check_auth_body_size(request)
    if size_fail:
        return size_fail
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    old_pw = data.get("old_password", "").strip()
    new_pw = data.get("new_password", "").strip()

    if not old_pw or not new_pw:
        return JSONResponse(
            {"status": "error", "error": "Both old and new password required."},
            status_code=400,
        )
    if len(new_pw) < 6:
        return JSONResponse(
            {"status": "error", "error": "New password must be at least 6 characters."},
            status_code=400,
        )

    success, error = auth.change_password(old_pw, new_pw)
    if not success:
        return JSONResponse({"status": "error", "error": error}, status_code=400)

    return JSONResponse({"status": "ok", "message": "Password changed."})


async def auth_recover(request):
    """Recover account with 12-word seed phrase.

    POST /api/auth/recover
    {
        "seed_phrase": "word1 word2 word3 ... word12",
        "new_password": "my-new-password"
    }

    No auth required (user forgot their password, that's the point).
    Invalidates ALL existing sessions for security.
    """
    rate_fail = check_rate_limit(request)
    if rate_fail:
        return rate_fail

    size_fail = _check_auth_body_size(request)
    if size_fail:
        return size_fail
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    seed = data.get("seed_phrase", "").strip()
    new_pw = data.get("new_password", "").strip()

    if not seed or not new_pw:
        return JSONResponse(
            {"status": "error", "error": "Seed phrase and new password required."},
            status_code=400,
        )
    if len(new_pw) < 6:
        return JSONResponse(
            {"status": "error", "error": "Password must be at least 6 characters."},
            status_code=400,
        )

    success, error = auth.recover_with_seed(seed, new_pw)
    if not success:
        return JSONResponse({"status": "error", "error": error}, status_code=400)

    return JSONResponse({"status": "ok", "message": "Password reset. All sessions logged out."})


async def auth_logout(request):
    """Logout - delete the current session.

    POST /api/auth/logout

    Requires the session token in Authorization header.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        auth.delete_session(auth_header[7:])
    return JSONResponse({"status": "ok"})


async def auth_sessions(request):
    """List all active sessions (connected devices).

    GET /api/auth/sessions

    Requires a valid session token. Shows device names and last activity.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer ") or not auth.validate_session(auth_header[7:]):
        return JSONResponse({"status": "error", "error": "Not authenticated."}, status_code=401)

    sessions = auth.get_active_sessions()
    return JSONResponse({"status": "ok", "sessions": sessions})


async def auth_login_history(request):
    """Get login history — every login attempt with device, IP, and timestamp.

    GET /api/auth/history?limit=50

    The account owner can see who accessed their dashboard and when,
    even if they didn't post anything. Failed logins show too.
    Requires a valid session token.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer ") or not auth.validate_session(auth_header[7:]):
        return JSONResponse({"status": "error", "error": "Not authenticated."}, status_code=401)

    try:
        limit = min(int(request.query_params.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    history = auth.get_login_history(limit=limit)
    return JSONResponse({"status": "ok", "history": history})


async def auth_revoke_session(request):
    """Revoke (kick off) a session by its ID. Admin-only — you must be
    logged in to revoke other sessions.

    POST /api/auth/revoke
    {
        "session_id": 5
    }

    The owner can remove anyone from their dashboard. The revoked device
    will be logged out on their next request.
    """
    auth_header = request.headers.get("authorization", "")
    current_session = auth.validate_session(auth_header[7:]) if auth_header.startswith("Bearer ") else None
    if not current_session:
        return JSONResponse({"status": "error", "error": "Not authenticated."}, status_code=401)

    size_fail = _check_auth_body_size(request)
    if size_fail:
        return size_fail
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    session_id = data.get("session_id")
    if not session_id:
        return JSONResponse({"status": "error", "error": "session_id required."}, status_code=400)

    # Don't let the user revoke their own current session (use logout for that)
    if session_id == current_session.get("id"):
        return JSONResponse({"status": "error", "error": "Can't revoke your own session. Use logout instead."}, status_code=400)

    success, error = auth.revoke_session(session_id)
    if success:
        return JSONResponse({"status": "ok", "message": "Session revoked."})
    return JSONResponse({"status": "error", "error": error}, status_code=400)


async def auth_rename_device(request):
    """Rename the current device. 10-day cooldown between renames.
    Device names must be unique across active sessions.

    POST /api/auth/rename-device
    {
        "device_name": "new-name"
    }

    Requires a valid session token in Authorization header.
    """
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token or not auth.validate_session(token):
        return JSONResponse({"status": "error", "error": "Not authenticated."}, status_code=401)

    size_fail = _check_auth_body_size(request)
    if size_fail:
        return size_fail
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    new_name = data.get("device_name", "").strip()[:100]
    ok, error = auth.rename_session_device(token, new_name)
    if not ok:
        return JSONResponse({"status": "error", "error": error}, status_code=400)

    return JSONResponse({"status": "ok", "device_name": new_name})


async def serve_dashboard(request):
    """Serve the STRATA dashboard HTML page.

    GET /dashboard

    No auth required - the dashboard handles its own login flow.
    """
    dashboard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    if not os.path.exists(dashboard_path):
        return JSONResponse({"error": "dashboard.html not found"}, status_code=404)

    from starlette.responses import HTMLResponse
    with open(dashboard_path, "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html)


async def api_vault_download(request):
    """REST endpoint for downloading a file from the vault.

    GET /api/vault/file/{thought_id}/{filename}

    Returns the raw file content with appropriate Content-Type header.
    Requires X-API-Key header or Authorization: Bearer token.
    """
    from starlette.responses import FileResponse as _FileResponse

    auth_fail = check_auth(request)
    if auth_fail:
        return auth_fail

    thought_id = request.path_params.get("thought_id")
    filename = request.path_params.get("filename")

    if not thought_id or not filename:
        return JSONResponse({"status": "error", "error": "Required: thought_id and filename in path"}, status_code=400)

    try:
        thought_id = int(thought_id)
    except (ValueError, TypeError):
        return JSONResponse({"status": "error", "error": "Invalid thought_id"}, status_code=400)

    # Find the attachment
    attachments = db.get_attachments(thought_id)
    target = None
    for att in attachments:
        if att["filename"].lower() == filename.lower():
            target = att
            break

    if not target:
        return JSONResponse({"status": "error", "error": "File not found"}, status_code=404)

    # Read the file
    try:
        abs_path = os.path.realpath(os.path.join(vault.VAULT_DIR, target["vault_path"]))
        vault_abs = os.path.realpath(vault.VAULT_DIR)

        if not (abs_path == vault_abs or abs_path.startswith(vault_abs + os.sep)):
            return JSONResponse({"status": "error", "error": "Invalid path"}, status_code=400)

        if not os.path.exists(abs_path):
            return JSONResponse({"status": "error", "error": "File missing from vault"}, status_code=404)

        # Stream from disk instead of buffering entire file into memory.
        # A 1GB vault file would OOM the Pi if loaded into a bytes object.
        # FileResponse streams in chunks - constant memory usage.
        return _FileResponse(
            path=abs_path,
            media_type=target["mime_type"],
            filename=target["filename"],
        )

    except Exception as e:
        return JSONResponse({"status": "error", "error": f"Download failed: {str(e)}"}, status_code=500)


# Build the ASGI app - Streamable HTTP transport + health check
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

# Custom routes go first - matched before MCP's catch-all
# Register REST routes - indices must be unique and sequential
custom_routes = [
    Route("/health", health_check),
    Route("/api/capture", api_capture, methods=["POST"]),
    Route("/api/search", api_search, methods=["POST"]),
    Route("/api/search/tag", api_search_by_tag, methods=["POST"]),
    Route("/api/thought/{thought_id:int}", api_thought_detail, methods=["GET"]),
    Route("/api/vault/upload", api_vault_upload, methods=["POST"]),
    Route("/api/vault/file/{thought_id:int}/{filename:path}", api_vault_download, methods=["GET"]),
    Route("/api/stats", api_stats, methods=["GET"]),
    Route("/api/recent", api_recent, methods=["GET"]),
    Route("/api/report", api_report, methods=["GET"]),
    Route("/api/auth/status", auth_status, methods=["GET"]),
    Route("/api/auth/setup", auth_setup, methods=["POST"]),
    Route("/api/auth/login", auth_login, methods=["POST"]),
    Route("/api/auth/change-password", auth_change_password, methods=["POST"]),
    Route("/api/auth/recover", auth_recover, methods=["POST"]),
    Route("/api/auth/logout", auth_logout, methods=["POST"]),
    Route("/api/auth/sessions", auth_sessions, methods=["GET"]),
    Route("/api/auth/history", auth_login_history, methods=["GET"]),
    Route("/api/auth/revoke", auth_revoke_session, methods=["POST"]),
    Route("/api/auth/rename-device", auth_rename_device, methods=["POST"]),
    Route("/dashboard", serve_dashboard),
]
for i, route in enumerate(custom_routes):
    mcp_app.routes.insert(i, route)

# Wrap the entire app with OAuth bypass - this is the ASGI entrypoint
# The middleware intercepts /.well-known/* and /register before they
# reach Starlette, guaranteeing JSON 404 responses for OAuth discovery.
app = OAuthBypassMiddleware(mcp_app)


# ============================================================
# EXTENSIONS � Private agent orchestration layer (optional)
# ============================================================
# If extensions.py is present, additional MCP tools and REST
# endpoints are registered. If it's missing (like on a fresh
# clone from GitHub), the server runs fine with just the core
# tools above. This is the "Option B" architecture � public
# core in server.py, private agent layer in extensions.py.
try:
    from extensions import register_extensions
    register_extensions(
        mcp, mcp_app,
        db=db, embedder=embedder, json=json,
        sanitize_results=sanitize_results,
        sanitize_for_ai=sanitize_for_ai,
        check_auth=check_auth,
    )
    print("[strata] Extensions loaded � private agent tools active.")
except ImportError:
    # No extensions.py found � that's fine, core tools are sufficient.
    print("[strata] No extensions found � running public toolset only.")
except Exception as e:
    # Extensions exist but failed to load � log it but don't crash.
    print(f"[strata] WARNING: Extensions failed to load: {e}")


if __name__ == "__main__":
    import uvicorn
    print(f"[strata] Starting MCP server on {HOST}:{PORT}")
    print(f"[strata] MCP endpoint:    http://0.0.0.0:{PORT}/mcp")
    print(f"[strata] Health check:    http://0.0.0.0:{PORT}/health")
    print(f"[strata] REST capture:    http://0.0.0.0:{PORT}/api/capture")
    print(f"[strata] REST search:     http://0.0.0.0:{PORT}/api/search")
    print(f"[strata] REST vault:      http://0.0.0.0:{PORT}/api/vault/upload")
    print(f"[strata] REST download:   http://0.0.0.0:{PORT}/api/vault/file/{{id}}/{{name}}")
    print(f"[strata] REST stats:      http://0.0.0.0:{PORT}/api/stats")
    print(f"[strata] REST recent:     http://0.0.0.0:{PORT}/api/recent")
    print(f"[strata] REST report:     http://0.0.0.0:{PORT}/api/report")
    print(f"[strata] Dashboard:       http://0.0.0.0:{PORT}/dashboard")
    print(f"[strata] Database:        {db.DB_PATH}")
    print(f"[strata] Vault:           {vault.VAULT_DIR}")
    uvicorn.run(app, host=HOST, port=PORT)
