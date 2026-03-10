# STRATA - Dashboard Account System
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under PolyForm Noncommercial 1.0.0 - see LICENSE file
#
# Handles password hashing, session tokens, and seed phrase recovery
# for the web dashboard. Uses ONLY Python standard library - no extra
# pip packages, no RAM impact on the Pi.
#
# AI clients (Claude Code, Codex, bots) still use the API key.
# This auth layer is specifically for human users on the dashboard.

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "brain.db")

# PBKDF2 config - 480K iterations balances security vs Pi CPU speed.
# On a Pi 4B this takes ~0.3s per hash which is fine for login but
# prevents brute-force attacks (attacker gets ~3 guesses/sec on Pi hardware).
PBKDF2_ITERATIONS = 480_000
SALT_LENGTH = 32
SESSION_TOKEN_BYTES = 32  # 256-bit random tokens

# 256 simple English words for seed phrase generation.
# 12 random picks from 256 words = 96 bits of entropy (2^96 possible phrases).
# That's ~79 billion billion billion combinations - effectively unguessable.
# Words chosen to be: common, easy to spell, unambiguous, 4-7 letters each.
SEED_WORDS = [
    "acorn", "alarm", "album", "alder", "alert", "alive", "alley", "amber",
    "ample", "angel", "ankle", "anvil", "apple", "arena", "armor", "arrow",
    "atlas", "awake", "badge", "bagel", "baker", "basil", "basin", "batch",
    "beach", "beard", "bench", "berry", "birch", "blade", "blank", "blaze",
    "blend", "bloom", "board", "bonus", "booth", "bound", "brace", "brain",
    "brave", "bread", "brick", "bride", "brief", "brisk", "broad", "brook",
    "brush", "build", "bunch", "cabin", "cable", "camel", "candy", "cargo",
    "cedar", "chain", "chalk", "charm", "chase", "chess", "chief", "cider",
    "civic", "claim", "clamp", "clash", "clasp", "clean", "clerk", "cliff",
    "climb", "clock", "cloth", "cloud", "coach", "coral", "couch", "cover",
    "craft", "crane", "crash", "cream", "crisp", "cross", "crowd", "crush",
    "curve", "cycle", "daily", "dance", "delta", "depot", "diary", "diver",
    "donor", "draft", "drain", "drawn", "drift", "drill", "drums", "dwell",
    "eagle", "earth", "easel", "elbow", "elder", "ember", "entry", "equal",
    "evade", "extra", "fable", "faith", "feast", "fence", "fiber", "field",
    "flame", "flask", "flock", "flood", "flora", "fluid", "flute", "focal",
    "forge", "forum", "found", "frame", "fresh", "frost", "fruit", "gable",
    "gauge", "giant", "given", "gland", "glass", "gleam", "globe", "glove",
    "grace", "grain", "grand", "grant", "grape", "grasp", "grind", "grove",
    "guard", "guide", "haven", "hazel", "heart", "hedge", "heron", "hinge",
    "honey", "hover", "humor", "ideal", "index", "inner", "input", "ivory",
    "jewel", "joint", "judge", "juice", "kayak", "known", "label", "latch",
    "layer", "lemon", "level", "light", "lilac", "linen", "lodge", "lunar",
    "lunch", "maple", "march", "marsh", "mason", "medal", "merge", "metal",
    "minor", "model", "moist", "mound", "mural", "nerve", "noble", "north",
    "noted", "novel", "ocean", "olive", "onset", "opera", "orbit", "organ",
    "otter", "outer", "oxide", "paint", "panel", "paper", "patch", "pearl",
    "pedal", "penny", "phase", "piano", "pilot", "pitch", "pixel", "place",
    "plank", "plant", "plaza", "plumb", "polar", "pouch", "power", "press",
    "price", "pride", "prism", "probe", "prong", "pulse", "quail", "quest",
    "radar", "raven", "realm", "ridge", "rival", "robin", "royal", "ruler",
    "salad", "scale", "scout", "shaft", "shell", "shore", "sigma", "silk",
]


def _get_db():
    """Get a database connection with row factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_auth_tables():
    """Create users, sessions, and login_history tables if they don't exist.
    Called once at server startup alongside db.init_db()."""
    conn = _get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                password_hash TEXT NOT NULL,
                device_name TEXT DEFAULT '',
                seed_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                device_name TEXT DEFAULT '',
                remember_days INTEGER DEFAULT 30,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP,
                name_changed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        # Login history — tracks every login (successful or failed) with
        # device info, IP, and timestamp. The account owner can see who
        # accessed their dashboard and when, even if they didn't post.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                device_name TEXT DEFAULT '',
                ip_address TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                success INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add name_changed_at if upgrading from older schema
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN name_changed_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Index on token for fast session lookups (every API call checks this)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_history_created ON login_history(created_at)")
        conn.commit()
    finally:
        conn.close()


# ============================================================
# PASSWORD HASHING - PBKDF2-SHA256 (built into Python)
# ============================================================
# No bcrypt, no argon2, no pip install needed. PBKDF2 with 480K
# iterations is OWASP-recommended and runs fine on Pi hardware.

def hash_password(password):
    """Hash a password using PBKDF2-SHA256 with random salt.
    Returns 'salt_hex:key_hex' string for storage."""
    salt = os.urandom(SALT_LENGTH)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex() + ":" + key.hex()


def verify_password(password, stored_hash):
    """Verify a password against a stored PBKDF2 hash.
    Uses constant-time comparison to prevent timing attacks."""
    try:
        salt_hex, key_hex = stored_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return hmac.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


# ============================================================
# SEED PHRASE - 12-word recovery key (like a Bitcoin wallet)
# ============================================================
# Generated once during setup, shown to the user ONCE, never stored
# in plaintext. Only the hash is kept. If user forgets their password,
# entering the 12 words correctly resets it.

def generate_seed_phrase():
    """Generate a 12-word recovery phrase from 256 curated words.
    96 bits of entropy - effectively unguessable by brute force."""
    return " ".join(secrets.choice(SEED_WORDS) for _ in range(12))


# ============================================================
# ACCOUNT MANAGEMENT
# ============================================================

def is_setup_complete():
    """Check if the initial account has been created."""
    conn = _get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return count > 0
    except sqlite3.OperationalError:
        # Table might not exist yet on very first run
        return False
    finally:
        conn.close()


def setup_account(password, device_name=""):
    """Create the owner account. Only works once (single-user system).

    Returns (seed_phrase, None) on success, or (None, error_message) on failure.
    The seed phrase is shown to the user ONCE - we only store its hash.
    """
    if is_setup_complete():
        return None, "Account already exists. Use login instead."

    seed_phrase = generate_seed_phrase()
    password_hash = hash_password(password)
    seed_hash = hash_password(seed_phrase)

    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO users (password_hash, device_name, seed_hash) VALUES (?, ?, ?)",
            (password_hash, device_name[:100], seed_hash),
        )
        conn.commit()
        return seed_phrase, None
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()


def login(password):
    """Authenticate with password. Returns user dict or None."""
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        if verify_password(password, row["password_hash"]):
            # Update last login timestamp
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (datetime.now().isoformat(), row["id"]),
            )
            conn.commit()
            return dict(row)
        return None
    finally:
        conn.close()


def change_password(old_password, new_password):
    """Change password. Requires the current password to authorize.
    Returns (True, None) on success, (False, error_message) on failure."""
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False, "No account found."
        if not verify_password(old_password, row["password_hash"]):
            return False, "Current password is incorrect."
        new_hash = hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, row["id"]))
        conn.commit()
        return True, None
    finally:
        conn.close()


def recover_with_seed(seed_phrase, new_password):
    """Reset password using the 12-word recovery phrase.
    Invalidates ALL existing sessions for security (password was lost/compromised).
    Returns (True, None) on success, (False, error_message) on failure."""
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False, "No account found."
        if not verify_password(seed_phrase.strip().lower(), row["seed_hash"]):
            return False, "Recovery phrase is incorrect. Check spelling and word order."
        new_hash = hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, row["id"]))
        # Kill all sessions - if the password was lost, any existing sessions
        # might be from an attacker. Clean slate.
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        return True, None
    finally:
        conn.close()


# ============================================================
# SESSION MANAGEMENT
# ============================================================
# Sessions are random 256-bit tokens stored in the database.
# Each device gets its own session. The dashboard sends the token
# with every API request in the Authorization: Bearer header.

def create_session(user_id, device_name="", days=30):
    """Create a new session token for a device.
    Returns the token string (stored in browser localStorage)."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    expires = (datetime.now() + timedelta(days=days)).isoformat()

    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO sessions (user_id, token, device_name, remember_days, expires_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, token, device_name[:100], days, expires),
        )
        # Housekeeping: remove expired sessions so the table doesn't grow forever
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now().isoformat(),))
        conn.commit()
        return token
    finally:
        conn.close()


def validate_session(token):
    """Check if a session token is valid and not expired.
    Updates last_used timestamp on each check (tracks device activity).
    Returns session dict or None."""
    if not token:
        return None
    conn = _get_db()
    try:
        row = conn.execute(
            """SELECT s.id, s.user_id, s.device_name, s.expires_at, s.created_at, s.remember_days
               FROM sessions s
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, datetime.now().isoformat()),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE sessions SET last_used = ? WHERE id = ?",
                (datetime.now().isoformat(), row["id"]),
            )
            conn.commit()
            return dict(row)
        return None
    finally:
        conn.close()


def delete_session(token):
    """Delete a specific session (logout)."""
    conn = _get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def get_active_sessions():
    """List all active (non-expired) sessions with their IDs.
    Used by settings page to show connected devices + revoke buttons."""
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT id, device_name, created_at, last_used, expires_at, remember_days
               FROM sessions
               WHERE expires_at > ?
               ORDER BY last_used DESC""",
            (datetime.now().isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def is_device_name_taken(device_name, exclude_session_token=None):
    """Check if a device name is already used by an active session.
    Optionally exclude a specific session (so a device can 'keep' its own name).
    Case-insensitive comparison - 'My-Laptop' and 'my-laptop' are the same."""
    if not device_name or not device_name.strip():
        return False  # blank names are always allowed (they show as "Unknown")
    conn = _get_db()
    try:
        if exclude_session_token:
            row = conn.execute(
                """SELECT COUNT(*) FROM sessions
                   WHERE LOWER(device_name) = LOWER(?) AND token != ? AND expires_at > ?""",
                (device_name.strip(), exclude_session_token, datetime.now().isoformat()),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) FROM sessions
                   WHERE LOWER(device_name) = LOWER(?) AND expires_at > ?""",
                (device_name.strip(), datetime.now().isoformat()),
            ).fetchone()
        return row[0] > 0
    finally:
        conn.close()


def refresh_session_by_device(device_name, user_id, days=30):
    """Refresh an existing session for a device name.
    If someone logs in with the correct password and a device name that
    already exists, we replace the old session with a fresh one instead
    of rejecting them. This is the expected behavior — same user, same
    device label, new token. Returns the new token string."""
    conn = _get_db()
    try:
        # Delete the old session(s) with this device name
        conn.execute(
            "DELETE FROM sessions WHERE LOWER(device_name) = LOWER(?) AND expires_at > ?",
            (device_name.strip(), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    # Create a fresh session with the same device name
    return create_session(user_id, device_name, days=days)


# ============================================================
# LOGIN HISTORY — Every login gets recorded (success or fail)
# ============================================================
# The account owner can see who accessed their dashboard, from
# what device, at what time. Even if they didn't post anything.

def record_login(user_id, device_name="", ip_address="", user_agent="", success=True):
    """Record a login attempt in the history table.
    Called on every login — successful or failed."""
    conn = _get_db()
    try:
        conn.execute(
            """INSERT INTO login_history (user_id, device_name, ip_address, user_agent, success)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, device_name[:100], ip_address[:45], user_agent[:500], 1 if success else 0),
        )
        # Keep history from growing forever — cap at 500 entries
        conn.execute("""
            DELETE FROM login_history WHERE id NOT IN (
                SELECT id FROM login_history ORDER BY created_at DESC LIMIT 500
            )
        """)
        conn.commit()
    finally:
        conn.close()


def get_login_history(limit=50):
    """Get recent login history. Returns list of dicts with
    device_name, ip_address, user_agent, success, created_at."""
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT device_name, ip_address, user_agent, success, created_at
               FROM login_history
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_session(session_id):
    """Revoke (delete) a session by its database ID.
    Admin uses this to kick devices off the dashboard."""
    conn = _get_db()
    try:
        # Make sure the session exists before deleting
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return False, "Session not found."
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def parse_user_agent(ua_string):
    """Extract a friendly device label from a User-Agent string.
    'Mozilla/5.0 (Windows NT 10.0...) Chrome/120...' → 'Chrome on Windows'
    No pip packages needed — just simple string matching."""
    if not ua_string:
        return "Unknown"
    ua = ua_string.lower()

    # Detect browser
    browser = "Browser"
    if "edg/" in ua or "edge/" in ua:
        browser = "Edge"
    elif "chrome/" in ua and "safari/" in ua:
        browser = "Chrome"
    elif "firefox/" in ua:
        browser = "Firefox"
    elif "safari/" in ua:
        browser = "Safari"
    elif "opera" in ua or "opr/" in ua:
        browser = "Opera"

    # Detect OS
    os_name = "Unknown"
    if "windows" in ua:
        os_name = "Windows"
    elif "iphone" in ua:
        os_name = "iPhone"
    elif "ipad" in ua:
        os_name = "iPad"
    elif "mac os" in ua or "macos" in ua:
        os_name = "Mac"
    elif "android" in ua:
        os_name = "Android"
    elif "linux" in ua:
        os_name = "Linux"
    elif "chromeos" in ua or "cros" in ua:
        os_name = "ChromeOS"

    return f"{browser} on {os_name}"


def rename_session_device(token, new_name):
    """Rename the device for a specific session.
    Stores the rename timestamp so we can enforce a 10-day cooldown.
    Returns (True, None) on success, (False, error) on failure."""
    if not token:
        return False, "No session."
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT id, device_name, name_changed_at FROM sessions WHERE token = ? AND expires_at > ?",
            (token, datetime.now().isoformat()),
        ).fetchone()
        if not row:
            return False, "Session not found."

        # Enforce 10-day cooldown between renames
        changed_at = row["name_changed_at"] if "name_changed_at" in row.keys() else None
        if changed_at:
            last_change = datetime.fromisoformat(changed_at)
            cooldown_end = last_change + timedelta(days=10)
            if datetime.now() < cooldown_end:
                days_left = (cooldown_end - datetime.now()).days + 1
                return False, f"Device name can only be changed every 10 days. Try again in {days_left} day{'s' if days_left != 1 else ''}."

        # Check uniqueness
        if new_name.strip() and is_device_name_taken(new_name, exclude_session_token=token):
            return False, f"Device name '{new_name}' is already in use."

        conn.execute(
            "UPDATE sessions SET device_name = ?, name_changed_at = ? WHERE id = ?",
            (new_name.strip()[:100], datetime.now().isoformat(), row["id"]),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def update_device_name(user_id, device_name):
    """Update the default device name for the account."""
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE users SET device_name = ? WHERE id = ?",
            (device_name[:100], user_id),
        )
        conn.commit()
    finally:
        conn.close()
