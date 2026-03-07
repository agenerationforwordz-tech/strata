# STRATA

Self-hosted AI memory server. Your thoughts, your hardware, your data.

## Why Strata?

Your AI forgets everything the moment the session ends. Strata fixes that. It gives any MCP-compatible AI a persistent memory that lives on YOUR hardware - not in the cloud, not on someone else's server. Search by meaning, attach real files, and run it all on a Raspberry Pi.

![Strata Dashboard](dashboard-preview.png)

I built this because I got tired of my AI tools forgetting everything between sessions. STRATA gives any MCP-compatible AI (Claude Code, Codex CLI, whatever comes next) a persistent brain that lives on YOUR machine. Not in the cloud. Not on someone else's server. Yours.

It runs on a Raspberry Pi 4B. Seriously. The whole thing - semantic search, file storage, embeddings - runs on a $55 computer with 4GB of RAM.

## What it does

- **Search by meaning** - ask for "money stuff" and it finds your notes about Bitcoin, investments, and budgets. Not keyword matching. Actual understanding.
- **File vault** - attach real files to your thoughts. Code, documents, project archives. The thought is your index, the vault holds the goods.
- **Auto-tagging** - write about Docker? It tags it. Mention a URL? Tagged. You don't have to organize anything manually.
- **Dedup protection** - tries to save something you already saved? It catches it and warns you.
- **Access tracking** - see which memories actually get used vs which ones just sit there.
- **Admin-only deletion** - AI can read and write memories, but it CANNOT delete anything without your explicit admin key. Your data, your control.

## Why I built it this way

Regular databases search by keywords. If you stored "switching careers" but search for "new job", you get nothing.

STRATA converts every thought into a 768-dimensional vector that captures what it *means*. Similar ideas land near each other in that vector space. So "switching careers" and "new job" and "career change" all find each other.

The model runs locally through ONNX Runtime (not PyTorch - that thing eats 1.5GB just to import on ARM). Total RAM for the embedding model is about 100-150MB, and it unloads after 5 minutes of idle time.

## The file vault

This is what makes STRATA more than a note-taking app.

You capture a thought about a project. Then you attach the actual source files to it. Later, when you or your AI searches for that topic, you get the thought AND a list of every file attached to it. The AI picks which files it needs and pulls just those - it doesn't load your whole drive into memory.

Files are organized on disk like this:
```
data/vault/
  my-laptop/
    2026-03/
      42/
        server.py
        config.py
  my-desktop/
    2026-03/
      55/
        training_data.csv
```

Organized by device and month. You can browse it manually if you want. Files up to 1GB, text files capped at 5MB when returned to AI (so it doesn't blow up the context window).

## Getting started

```bash
git clone https://github.com/agenerationforwordz-tech/strata.git
cd strata

python -m venv venv
source venv/bin/activate  # Linux/macOS
# or: venv\Scripts\activate  # Windows

pip install -r requirements.txt

# Set your keys
export STRATA_API_KEY="pick-something-secure"
export STRATA_ADMIN_KEY="different-key-for-delete-ops"

python server.py
```

First request downloads the embedding model (~170MB). After that it's cached.

Server runs at `http://0.0.0.0:4320`.

## Connecting your AI

### Claude Code

```bash
claude mcp add --transport http strata http://your-server-ip:4320/mcp
```

Or add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "strata": {
      "url": "http://your-server-ip:4320/mcp"
    }
  }
}
```

### Codex CLI

Same format in your Codex MCP config.

### Any HTTP client

```bash
# Save a thought
curl -X POST http://your-server:4320/api/capture \
 -H "Content-Type: application/json" \
 -H "X-API-Key: your-key" \
 -d '{"content": "STRATA is running on my Pi!", "tags": ["setup"]}'

# Search by meaning
curl -X POST http://your-server:4320/api/search \
 -H "Content-Type: application/json" \
 -H "X-API-Key: your-key" \
 -d '{"query": "server setup", "limit": 5}'

# Health check (no auth needed)
curl http://your-server:4320/health
```

## Configuration

All in `config.py`. Override with environment variables:

| Variable | Default | What it does |
|----------|---------|-------------|
| `STRATA_API_KEY` | `change-me-before-deploy` | Protects REST endpoints |
| `STRATA_ADMIN_KEY` | *(empty - disables deletion)* | Required for delete operations |
| `STRATA_HOST` | `0.0.0.0` | Listen address |
| `STRATA_PORT` | `4320` | Server port |
| `STRATA_DATA_DIR` | `./data` | Database location |
| `STRATA_VAULT_DIR` | `./data/vault` | File vault location |
| `STRATA_AUTH_ENABLED` | `true` | Kill switch for auth (don't) |

## The tools (18 total)

### Memory
| Tool | What it does |
|------|-------------|
| `capture_thought` | Save a new memory with auto-tagging and dedup check |
| `semantic_search` | Find memories by meaning |
| `hybrid_search` | Keywords + meaning combined |
| `search_by_tag` | Filter by tag |
| `search_by_person` | Filter by person mentioned |
| `search_advanced` | Stack filters: tag + type + date + machine |
| `get_relevant_context` | Smart search - deduped and grouped by type |
| `find_related` | "More like this" for a specific thought |
| `list_recent` | What got captured recently |
| `get_thought` | View one thought (includes its file attachments) |
| `update_thought` | Edit a thought (re-embeds automatically) |
| `delete_thought` | Remove a thought + vault files (admin key required) |
| `get_stats` | Database stats and vault usage |
| `generate_report` | Trend report - what's rising, what's declining |

### File vault
| Tool | What it does |
|------|-------------|
| `attach_file` | Attach a file to a thought |
| `get_file` | Read an attached file |
| `list_attachments` | See all files on a thought |
| `detach_file` | Remove an attachment (admin key required) |

## Web dashboard

STRATA includes a full web dashboard at `http://your-server:4320/dashboard`. No extra setup needed - first visit walks you through account creation.

### Account system

- **Password-protected** - first visit creates your account with a password (PBKDF2-SHA256, 480K iterations)
- **12-word recovery phrase** - generated during setup like a Bitcoin wallet. Write it down. It's the only way to reset a forgotten password. Stored as a hash, never in plaintext.
- **Device sessions** - each browser gets a named session ("my-laptop", "my-phone"). Names must be unique across devices. Sessions last 7-365 days (you pick).
- **10-day rename cooldown** - device names can only be changed once every 10 days to prevent abuse
- **Settings page** - change password (requires current), view connected devices, rename your device

AI clients (Claude Code, Codex, etc.) still use the API key via `X-API-Key` header. The dashboard auth is a separate system - they don't interfere with each other.

### Dashboard features

- **Capture thoughts** from the browser with tags, people, and type selection
- **Semantic search** - find by meaning, not keywords
- **Tag search** - click any tag to filter all thoughts with that tag. Type `#tagname` in the search bar for the same thing.
- **Infinite scroll** - loads 30 thoughts at a time, fetches more as you scroll
- **Stats cards** - total memories, tag count, people mentioned, database size
- **Trending tags** - rising/declining/new tags with clickable filters
- **Device activity** - animated bars showing which machines contribute the most
- **Hottest memories** - most-accessed thoughts ranked by hit count
- **Dark theme** - warm dark mode, responsive on mobile

## Running as a service

Create `/etc/systemd/system/strata.service`:

```ini
[Unit]
Description=STRATA Memory Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/strata
ExecStart=/path/to/strata/venv/bin/python server.py
Environment=STRATA_API_KEY=your-secret-key
Environment=STRATA_ADMIN_KEY=your-admin-key
Environment=STRATA_DATA_DIR=/path/to/your/data
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable strata
sudo systemctl start strata
```

## How it's built

```
AI Clients (Claude Code, Codex, bots)          Humans (browser)
        |                    |                       |
   MCP (/mcp)          REST (/api/*)          Dashboard (/dashboard)
        |                    |                       |
        +-------- server.py - rate limiting, sanitization --------+
        |                    |                       |
   API key auth         API key auth          Password + session auth
   (X-API-Key)          (X-API-Key)           (auth.py - PBKDF2, seeds)
        |                    |                       |
   embedder.py          vault.py                     |
   ONNX model         file storage                   |
   on-demand          by device/month                 |
        |                    |                       |
              db.py - SQLite + FTS5 + numpy
              thoughts + attachments + users + sessions
```

## Hardware

Built to run on a Raspberry Pi 4B (4GB). Here's what it actually uses:

| What | RAM |
|------|-----|
| Server idle | ~50MB |
| Embedding model loaded | ~100-150MB |
| 10K thoughts | ~30MB for vectors |

Model loads when you need it, unloads after 5 min idle.

### Storage

| What | Disk |
|------|------|
| Python venv (fastembed, numpy, onnxruntime) | ~700MB |
| ONNX model (downloaded on first use) | ~170MB |
| Code + dashboard | ~1MB |
| Database | ~1KB per thought |

A fresh install needs roughly **1GB of disk space**. A 16GB SD card is plenty. The database and file vault grow with usage, so if you plan to attach a lot of files, point the vault at external storage.

Minimum specs: any machine with 2GB RAM, 2GB free disk, and Python 3.10+.

## Security

- **Dual auth** - API key for AI clients (`X-API-Key`), password-based sessions for dashboard users
- **PBKDF2-SHA256** password hashing - 480K iterations, random 32-byte salt, constant-time comparison
- **12-word seed phrase** recovery - hashed and stored permanently, never in plaintext
- **Session tokens** - 256-bit random, stored in SQLite, auto-expire, per-device
- Separate admin key for destructive operations (delete/detach)
- Rate limiting - 30 req/min per IP
- Input size limits - 50KB per thought, 1GB per vault file
- Prompt injection defense - content wrapped as data markers for AI clients
- XSS prevention - all user content escaped before rendering
- Atomic file writes - temp file + rename, no partial files on power loss
- Path traversal protection on all vault operations
- No eval/exec anywhere in the codebase
- Zero additional dependencies - auth uses only Python standard library

## License

PolyForm Noncommercial License 1.0.0. Copyright (c) 2026 A Generation Forwordz Foundation.

Use it, improve it, share it, learn from it. Just keep it free and give credit. Do not sell it.

See [LICENSE](LICENSE) for details.

---

Built by Christian Mitchell. If you use this, keep the attribution.
