# Strata Demo Dataset

A pre-populated Strata database so you can see what the system looks like with real data, without having to capture your own thoughts first.

## What's in here

- **`strata.db`** — SQLite database with **666 curated thoughts** about Strata itself: how the system works, why it was designed this way, what it's good at, what it's not, real-world quotes from the build process. Every thought is embedded with the same `BAAI/bge-base-en-v1.5` model the live server uses, so semantic search works out of the box.
- **3 sample agent placeholders** in the `agent_keys` table — `claude-code`, `codex-cli`, `vox` — each with their own identity color so the constellation viewer renders them distinctly. **All three are shipped DISABLED (`enabled=0`)** so the free-tier 3-active hard lock doesn't fire when a new user registers their first real agent. The keys are obvious placeholders (`agent-DEMO-...`) — register your own real agents from `/admin/agents` before using the demo as a real install.
- **Zero personal data, zero session history, zero login records.** The demo was sanitized before shipping. No PII, no real API keys.

## How to use it

### As a fresh demo (recommended for first-time users)

```bash
git clone https://github.com/agenerationforwordz-tech/strata.git
cd strata
python -m venv venv
source venv/bin/activate     # or venv\Scripts\activate on Windows
pip install -r requirements.txt

# Drop the demo database into your data directory
mkdir -p data
cp demo/strata.db data/strata.db

# Run with demo mode so blank passwords work
STRATA_DEMO_MODE=true python server.py
```

Visit `http://localhost:4320/dashboard` — the dashboard accepts a blank password in demo mode. Open `/constellation` to see the 666 thoughts laid out in 3D. Open `/admin/agents` to see the sample agents and their colors.

### As a sandbox for testing

If you already have a populated Strata install and want to point a *separate* test instance at the demo:

```bash
STRATA_DATA_DIR=./demo \
STRATA_PORT=4321 \
STRATA_DEMO_MODE=true \
python server.py
```

That runs a second Strata on port 4321 against the demo data, leaving your real install on 4320 untouched.

## What the demo is NOT

- **Not a backup of any real install.** The 666 thoughts are seed data written specifically to teach Strata's tone and capabilities. They are not personal notes belonging to any user.
- **Not a benchmark dataset.** The thoughts are chosen for variety (every type, every tone, lots of cross-references), not for benchmarking semantic search precision.
- **Not the full feature set.** The demo doesn't ship the file vault, attachments, or thought_history audit trail rows. Those are populated as you use the system.

## Regenerating the demo

If you want to build your own demo dataset (different thoughts, different agents, your own seed data):

1. Stand up a fresh empty Strata install
2. Capture the thoughts you want via the dashboard or `/api/capture`
3. Register the agents you want via `/admin/agents`
4. Stop the server and copy `data/strata.db` somewhere
5. Sanitize it: `DELETE FROM users; DELETE FROM sessions; DELETE FROM login_history; DELETE FROM thought_history; DELETE FROM system_config; VACUUM;`
6. Replace any real `api_key` values with `agent-DEMO-...` placeholders
7. Ship as `demo/strata.db`

## License

The demo content is part of Strata and licensed under the same terms as the rest of the project — **PolyForm Noncommercial 1.0.0** (see `LICENSE` in the repo root). You're free to use it for learning, evaluation, and noncommercial work. For commercial use, contact the project owner.
