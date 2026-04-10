#!/bin/bash
# =============================================================================
# STRATA SECURITY HARDENING — setup_secure.sh
# =============================================================================
# Option 1: File-level access control via dedicated system user
#
# WHY: Without this, anyone with SSH/filesystem access can bypass all API
# guardrails (admin keys, rate limits, audit trails) by editing the SQLite
# file directly. This script creates a dedicated 'strata' system user and
# restricts DB access to only the Strata process.
#
# WHAT IT DOES:
#   1. Creates a 'strata' system user (no login, no home dir)
#   2. Changes ownership of data files (DB, vault) to strata:strata
#   3. Sets permissions so only the strata user can read/write the DB
#   4. Code files stay readable by everyone (nacho can still edit code)
#   5. Updates the systemd service to run as the strata user
#
# AFTER RUNNING: Direct DB access via SSH is blocked. All access goes
# through the API with proper auth and rate limiting. Admin operations
# require the admin key via the API — no shortcuts.
#
# TO UNDO: sudo chown -R nacho:nacho $DATA_DIR && update systemd User=nacho
# =============================================================================

set -e

# --- Configuration ---
# Override these if your Strata install lives somewhere else. The defaults
# match a fresh install where the repo sits next to its data directory.
STRATA_USER="${STRATA_USER:-strata}"
STRATA_CODE_DIR="${STRATA_CODE_DIR:-$(pwd)}"
STRATA_DATA_DIR="${STRATA_DATA_DIR:-$STRATA_CODE_DIR/data}"
STRATA_VAULT_DIR="${STRATA_VAULT_DIR:-$STRATA_DATA_DIR/vault}"
STRATA_LOG_DIR="${STRATA_LOG_DIR:-$STRATA_CODE_DIR/logs}"
STRATA_SERVICE="${STRATA_SERVICE:-/etc/systemd/system/strata.service}"

echo "=== Strata Security Hardening ==="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Run with sudo — this script needs root to create users and set permissions."
    exit 1
fi

# --- Step 1: Create dedicated system user ---
if id "$STRATA_USER" &>/dev/null; then
    echo "[OK] User '$STRATA_USER' already exists"
else
    echo "[+] Creating system user '$STRATA_USER' (no login, no home dir)..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$STRATA_USER"
    echo "[OK] User '$STRATA_USER' created"
fi

# --- Step 2: Set data directory ownership ---
echo "[+] Setting ownership on data directory: $STRATA_DATA_DIR"
chown -R "$STRATA_USER:$STRATA_USER" "$STRATA_DATA_DIR"
# DB and vault: owner read/write only. No group, no other.
chmod 700 "$STRATA_DATA_DIR"
find "$STRATA_DATA_DIR" -type f -exec chmod 600 {} \;
find "$STRATA_DATA_DIR" -type d -exec chmod 700 {} \;
echo "[OK] Data directory locked to '$STRATA_USER' only"

# --- Step 3: Log directory ---
echo "[+] Setting ownership on log directory: $STRATA_LOG_DIR"
mkdir -p "$STRATA_LOG_DIR"
chown -R "$STRATA_USER:$STRATA_USER" "$STRATA_LOG_DIR"
chmod 700 "$STRATA_LOG_DIR"
echo "[OK] Log directory locked"

# --- Step 4: Code stays readable (nacho can edit, strata can execute) ---
echo "[+] Ensuring code directory is readable by '$STRATA_USER'..."
# Code stays owned by nacho, but world-readable so strata user can execute
chmod -R o+rX "$STRATA_CODE_DIR"
# Venv needs to be executable by strata
chmod -R o+rX "$STRATA_CODE_DIR/venv"
echo "[OK] Code directory readable by all, writable by nacho only"

# --- Step 5: Update systemd service ---
echo "[+] Updating systemd service to run as '$STRATA_USER'..."
if grep -q "User=nacho" "$STRATA_SERVICE"; then
    sed -i "s/User=nacho/User=$STRATA_USER/" "$STRATA_SERVICE"
    echo "[OK] Service updated: User=$STRATA_USER"
else
    echo "[SKIP] Service already uses User=$STRATA_USER (or different user)"
fi

# Reload systemd
systemctl daemon-reload

echo ""
echo "=== Hardening Complete ==="
echo ""
echo "What changed:"
echo "  - Strata database in $STRATA_DATA_DIR is now owned by '$STRATA_USER'"
echo "  - Only the Strata process can read/write the database"
echo "  - SSH users (including nacho) CANNOT directly access the DB"
echo "  - All access must go through the Strata API with proper auth"
echo "  - Admin operations require the admin key (no filesystem bypass)"
echo ""
echo "To activate: sudo systemctl restart strata"
echo "To undo:     sudo chown -R nacho:nacho $STRATA_DATA_DIR && sudo sed -i 's/User=strata/User=nacho/' $STRATA_SERVICE && sudo systemctl daemon-reload"
echo ""
echo "⚠️  Direct DB access is now BLOCKED. Use the API."
