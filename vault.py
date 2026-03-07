# STRATA - Self-Hosted AI Memory Server
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under MIT - see LICENSE file
#
# File Vault - stores actual files attached to thoughts.
#
# This is the layer that turns Strata from a note-taking brain into a
# full knowledge system. Thoughts are the semantic index (searchable by
# meaning), and the vault holds the real content: source code, documents,
# images, entire project archives.
#
# Vault structure:
#   data/vault/{device}/{YYYY-MM}/{thought_id}/filename
#
# Organized by device and month so you can:
#  - Browse by device: "show me everything from my Surface"
#  - Browse by time: "show me what I stored in March"
#  - Find a thought's files: "thought 42 → vault/surface/2026-03/42/"
#
# For the product vision (selling pre-built units), the vault IS the product.
# The SSD/HDD holds all your files, organized and searchable by meaning.
# The brain.db is tiny - the vault is what fills the drive.

import base64
import hashlib
import os
import shutil
import tempfile
from datetime import datetime

from config import VAULT_DIR, MAX_FILE_SIZE, MAX_ATTACHMENT_CONTENT


def _vault_path(thought_id, device="unknown", created_at=None):
    """Build the vault directory path for a thought's attachments.

    Structure: vault/{device}/{YYYY-MM}/{thought_id}/

    Args:
        thought_id: The thought this file belongs to
        device: Which device uploaded the file (surface, helios, pi-nas, etc.)
        created_at: When the thought was created (for month bucketing).
                    Defaults to now if not provided.

    Returns:
        Absolute path to the thought's vault directory
    """
    if created_at is None:
        created_at = datetime.now()
    elif isinstance(created_at, str):
        # Parse ISO format timestamp from DB
        try:
            created_at = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            created_at = datetime.now()

    # Sanitize device name - no path traversal, no weird chars
    safe_device = "".join(c for c in device if c.isalnum() or c in "-_").strip() or "unknown"
    month_bucket = created_at.strftime("%Y-%m")

    return os.path.join(VAULT_DIR, safe_device, month_bucket, str(int(thought_id)))


def _sanitize_filename(filename):
    """Make a filename safe for storage. Prevents path traversal attacks.

    Strips directory components, replaces dangerous chars, caps length.
    A file named '../../../etc/passwd' becomes 'etc_passwd'.

    Args:
        filename: The original filename from the user

    Returns:
        A safe filename string (never empty - falls back to 'unnamed')
    """
    # Strip any directory components - only keep the filename part
    filename = os.path.basename(filename)

    # Replace path separators and null bytes
    filename = filename.replace("\x00", "").replace("/", "_").replace("\\", "_")

    # Remove leading dots (hidden files / traversal attempts)
    filename = filename.lstrip(".")

    # Cap length at 255 chars (filesystem limit)
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        if len(ext) > 20:
            # Absurdly long extension - just truncate the whole thing
            filename = filename[:255]
        else:
            filename = name[:255 - len(ext)] + ext

    return filename or "unnamed"


def _file_checksum(file_path):
    """Calculate SHA-256 checksum of a file on disk.

    Used for deduplication - if two files have the same checksum,
    they're identical. Also useful for integrity verification.

    Reads in 64KB chunks so it doesn't load the whole file into memory.
    Important for Pi 4B with only 4GB RAM.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)  # 64KB chunks
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def _content_checksum(content_bytes):
    """Calculate SHA-256 checksum from bytes (for inline content uploads).
    Checksum helps resolve integrity status, tracks identical artifacts,
    notifies on mismatches - important for archive-level validation."""
    return hashlib.sha256(content_bytes).hexdigest()


def store_file(thought_id, filename, content_bytes, device="unknown", created_at=None):
    """Store a file in the vault, attached to a thought.

    Creates the vault directory structure if needed, writes the file,
    and returns metadata about the stored file.

    Args:
        thought_id: Which thought this file belongs to
        filename: Original filename (will be sanitized)
        content_bytes: Raw bytes of the file content
        device: Which device is uploading (surface, helios, etc.)
        created_at: Thought creation timestamp (for directory bucketing)

    Returns:
        Dict with: vault_path, filename, file_size, checksum, mime_type

    Raises:
        ValueError: If file exceeds MAX_FILE_SIZE or filename is invalid
    """
    # Validate size
    if len(content_bytes) > MAX_FILE_SIZE:
        raise ValueError(
            f"File too large: {len(content_bytes):,} bytes "
            f"(max {MAX_FILE_SIZE:,} bytes / {MAX_FILE_SIZE // 1_000_000}MB)"
        )

    # Sanitize the filename
    safe_name = _sanitize_filename(filename)

    # Build the vault path and create directories
    vault_dir = _vault_path(thought_id, device, created_at)
    os.makedirs(vault_dir, exist_ok=True)

    # If a file with this name already exists, add a suffix
    dest_path = os.path.join(vault_dir, safe_name)
    if os.path.exists(dest_path):
        name, ext = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(dest_path):
            safe_name = f"{name}_{counter}{ext}"
            dest_path = os.path.join(vault_dir, safe_name)
            counter += 1

    # Write to temp file first, then atomic rename. If power cuts mid-write,
    # you get a leftover .tmp_ file instead of a corrupt file in the vault.
    # os.replace() is atomic on POSIX (same filesystem), near-atomic on Windows.
    fd, tmp_path = tempfile.mkstemp(dir=vault_dir, prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content_bytes)
        os.replace(tmp_path, dest_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Calculate checksum for dedup/integrity
    checksum = _content_checksum(content_bytes)

    # Guess mime type from extension
    mime_type = _guess_mime(safe_name)

    # Return the vault-relative path (portable across machines)
    rel_path = os.path.relpath(dest_path, VAULT_DIR)

    return {
        "vault_path": rel_path,           # Relative path within vault/
        "abs_path": dest_path,             # Absolute path on this machine
        "filename": safe_name,             # Sanitized filename
        "file_size": len(content_bytes),   # Size in bytes
        "checksum": checksum,              # SHA-256 for dedup
        "mime_type": mime_type,             # Guessed MIME type
    }


def store_from_base64(thought_id, filename, base64_content, device="unknown", created_at=None):
    """Store a file from base64-encoded content (for MCP tool uploads).

    MCP tools pass JSON, so binary files need base64 encoding.
    This decodes and passes to store_file().

    Args:
        thought_id: Which thought this file belongs to
        filename: Original filename
        base64_content: Base64-encoded file content
        device: Source device name
        created_at: Thought creation timestamp

    Returns:
        Same dict as store_file()

    Raises:
        ValueError: If base64 is invalid or decoded content exceeds limits
    """
    # Check base64 string length before decoding (rough size estimate)
    if len(base64_content) > MAX_ATTACHMENT_CONTENT:
        raise ValueError(
            f"Base64 content too large for MCP upload: {len(base64_content):,} chars "
            f"(max {MAX_ATTACHMENT_CONTENT:,}). Use REST upload for files over "
            f"{MAX_ATTACHMENT_CONTENT // 1_000_000}MB."
        )

    try:
        content_bytes = base64.b64decode(base64_content)
    except Exception:
        raise ValueError("Invalid base64 content - could not decode")

    return store_file(thought_id, filename, content_bytes, device, created_at)


def store_from_text(thought_id, filename, text_content, device="unknown", created_at=None):
    """Store a text file from string content (for code, markdown, etc.).

    Most common case for AI clients - they read a source file as text
    and want to attach it to a thought. No base64 needed.

    Args:
        thought_id: Which thought this file belongs to
        filename: Original filename (e.g., "server.py")
        text_content: The text content as a string
        device: Source device name
        created_at: Thought creation timestamp

    Returns:
        Same dict as store_file()
    """
    content_bytes = text_content.encode("utf-8")
    return store_file(thought_id, filename, content_bytes, device, created_at)


def read_file(vault_path):
    """Read a file from the vault by its relative path.

    Args:
        vault_path: Path relative to VAULT_DIR (as stored in attachments table)

    Returns:
        Dict with: content (str or base64), is_text (bool), file_size, filename

    Raises:
        FileNotFoundError: If the file doesn't exist
        ValueError: If the path tries to escape the vault (path traversal)
    """
    # Resolve absolute path and verify it's inside the vault
    abs_path = os.path.realpath(os.path.join(VAULT_DIR, vault_path))
    vault_abs = os.path.realpath(VAULT_DIR)

    if not (abs_path == vault_abs or abs_path.startswith(vault_abs + os.sep)):
        raise ValueError("Path traversal detected - path escapes vault directory")

    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"File not found in vault: {vault_path}")

    file_size = os.path.getsize(abs_path)
    filename = os.path.basename(abs_path)
    mime = _guess_mime(filename)

    # For text files, return content as string. For binary, return base64.
    is_text = _is_text_file(filename)

    if is_text:
        # Cap text file reads at 5MB to avoid blowing up AI context windows
        if file_size > 5_000_000:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(5_000_000)
            content += f"\n\n[... truncated at 5MB, full file is {file_size:,} bytes ...]"
        else:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
    else:
        # Binary file - return base64 (capped at 10MB for API responses)
        if file_size > 10_000_000:
            content = None  # Too large to inline - return path only
        else:
            with open(abs_path, "rb") as f:
                content = base64.b64encode(f.read()).decode("ascii")

    return {
        "content": content,
        "is_text": is_text,
        "file_size": file_size,
        "filename": filename,
        "mime_type": mime,
        "vault_path": vault_path,
    }


def delete_file(vault_path):
    """Delete a file from the vault.

    Args:
        vault_path: Path relative to VAULT_DIR

    Returns:
        True if deleted, False if file didn't exist

    Raises:
        ValueError: If path tries to escape vault
    """
    abs_path = os.path.realpath(os.path.join(VAULT_DIR, vault_path))
    vault_abs = os.path.realpath(VAULT_DIR)

    if not (abs_path == vault_abs or abs_path.startswith(vault_abs + os.sep)):
        raise ValueError("Path traversal detected - path escapes vault directory")

    if not os.path.exists(abs_path):
        return False

    os.remove(abs_path)

    # Clean up empty parent directories (don't leave empty month/device folders)
    parent = os.path.dirname(abs_path)
    while parent != vault_abs:
        try:
            if not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
            else:
                break
        except OSError:
            break

    return True


def delete_thought_files(thought_id, device="unknown", created_at=None):
    """Delete ALL files for a thought (when the thought itself is deleted).

    Removes the entire thought directory from the vault.

    Args:
        thought_id: The thought whose files should be deleted
        device: Device name (needed to find the vault path)
        created_at: Thought creation timestamp

    Returns:
        Number of files deleted
    """
    vault_dir = _vault_path(thought_id, device, created_at)

    if not os.path.exists(vault_dir):
        return 0

    # Count files before deletion
    count = sum(1 for _ in _walk_files(vault_dir))

    # Remove the entire thought directory
    shutil.rmtree(vault_dir, ignore_errors=True)

    # Clean up empty parent directories
    parent = os.path.dirname(vault_dir)
    vault_abs = os.path.realpath(VAULT_DIR)
    while parent != vault_abs:
        try:
            if not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
            else:
                break
        except OSError:
            break

    return count


def get_vault_stats():
    """Get overall vault statistics - total files, total size, by device.

    Returns:
        Dict with total_files, total_size_bytes, total_size_human, by_device
    """
    if not os.path.exists(VAULT_DIR):
        return {"total_files": 0, "total_size_bytes": 0, "total_size_human": "0 B", "by_device": {}}

    total_files = 0
    total_size = 0
    by_device = {}

    for device_dir in _list_dirs(VAULT_DIR):
        device_name = os.path.basename(device_dir)
        device_files = 0
        device_size = 0

        for f in _walk_files(device_dir):
            device_files += 1
            device_size += os.path.getsize(f)

        if device_files > 0:
            by_device[device_name] = {
                "files": device_files,
                "size_bytes": device_size,
                "size_human": _human_size(device_size),
            }
            total_files += device_files
            total_size += device_size

    return {
        "total_files": total_files,
        "total_size_bytes": total_size,
        "total_size_human": _human_size(total_size),
        "by_device": by_device,
    }


# ============================================================
# HELPERS
# ============================================================

# Text file extensions - these get returned as readable strings.
# Everything else is treated as binary (returned as base64 or path-only).
_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss", ".less",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".txt", ".rst", ".csv", ".tsv", ".log",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1",
    ".sql", ".graphql", ".gql",
    ".xml", ".svg", ".env", ".gitignore", ".dockerignore",
    ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php",
    ".r", ".R", ".jl", ".lua", ".pl", ".pm", ".swift", ".kt", ".scala",
    ".tf", ".hcl", ".nix", ".el", ".vim", ".ex", ".exs",
    ".makefile", ".cmake", ".gradle",
    ".dockerfile", ".service", ".timer", ".mount",
}


def _is_text_file(filename):
    """Check if a file is text-based by its extension."""
    _, ext = os.path.splitext(filename.lower())
    # Also treat files with no extension as text (README, LICENSE, Makefile, etc.)
    return ext in _TEXT_EXTENSIONS or ext == ""


def _guess_mime(filename):
    """Guess MIME type from file extension. Simple mapping, no magic bytes."""
    ext = os.path.splitext(filename.lower())[1]
    mime_map = {
        ".py": "text/x-python", ".js": "text/javascript", ".ts": "text/typescript",
        ".html": "text/html", ".css": "text/css", ".json": "application/json",
        ".md": "text/markdown", ".txt": "text/plain", ".csv": "text/csv",
        ".yaml": "text/yaml", ".yml": "text/yaml", ".toml": "application/toml",
        ".xml": "application/xml", ".svg": "image/svg+xml",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
        ".pdf": "application/pdf", ".zip": "application/zip",
        ".tar": "application/x-tar", ".gz": "application/gzip",
        ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".mp4": "video/mp4", ".webm": "video/webm",
    }
    return mime_map.get(ext, "application/octet-stream")


def _human_size(size_bytes):
    """Convert bytes to human-readable string (1234567 → '1.2 MB')."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _list_dirs(path):
    """List subdirectories of a path (non-recursive)."""
    try:
        return [os.path.join(path, d) for d in os.listdir(path)
                if os.path.isdir(os.path.join(path, d))]
    except OSError:
        return []


def _walk_files(path):
    """Yield all file paths under a directory (recursive)."""
    for root, _, files in os.walk(path):
        for f in files:
            yield os.path.join(root, f)
