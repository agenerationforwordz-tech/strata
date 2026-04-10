#!/bin/bash
# =============================================================================
# STRATA ENCRYPTION AT REST — setup_encryption.sh
# =============================================================================
# Option 2: SQLCipher database encryption
#
# WHY: Even with file permissions (Option 1), if someone copies the DB file
# (stolen drive, backup leak, compromised NAS), they get all your data in
# plain SQLite. SQLCipher encrypts the entire database — without the key,
# the file is random bytes.
#
# WHAT IT DOES:
#   1. Installs sqlcipher + pysqlcipher3 (Python binding)
#   2. Generates a random 256-bit encryption key
#   3. Migrates existing unencrypted DB to encrypted format
#   4. Sets STRATA_DB_ENCRYPT=true in the systemd service
#   5. Stores the key in a root-only file (/etc/strata/db.key)
#
# REQUIREMENTS: Debian/Ubuntu with apt. ARM64 (Pi 5) or x86_64.
# =============================================================================

set -e

# Detect whichever DB filename is already on disk so the encryption
# migration script Just Works regardless of which upgrade path got you here.
STRATA_DATA_DIR="${STRATA_DATA_DIR:-./data}"
DB_NAME="strata.db"  # default for fresh installs
for candidate in strata.db brain.db; do
    if [ -f "$STRATA_DATA_DIR/$candidate" ]; then
        DB_NAME="$candidate"
        break
    fi
done
DB_PATH="$STRATA_DATA_DIR/$DB_NAME"
DB_ENCRYPTED="$STRATA_DATA_DIR/${DB_NAME%.db}_encrypted.db"
DB_BACKUP="$STRATA_DATA_DIR/${DB_NAME%.db}_unencrypted_backup.db"
KEY_DIR="/etc/strata"
KEY_FILE="$KEY_DIR/db.key"
STRATA_SERVICE="/etc/systemd/system/strata.service"

echo "=== Strata Encryption Setup ==="
echo ""

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Run with sudo"
    exit 1
fi

# --- Step 1: Install dependencies ---
echo "[+] Installing sqlcipher and Python bindings..."
apt-get update -qq
apt-get install -y -qq sqlcipher libsqlcipher-dev
# pysqlcipher3 needs to compile against libsqlcipher
pip3 install pysqlcipher3 2>/dev/null || {
    echo "[!] pip3 install failed, trying with --break-system-packages..."
    pip3 install --break-system-packages pysqlcipher3
}
echo "[OK] SQLCipher installed"

# --- Step 2: Generate encryption key ---
echo "[+] Generating 256-bit encryption key..."
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [ -f "$KEY_FILE" ]; then
    echo "[SKIP] Key already exists at $KEY_FILE"
else
    # 32 bytes = 256 bits, hex-encoded = 64 chars
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$KEY_FILE"
    chmod 400 "$KEY_FILE"
    # If strata user exists (Option 1 was run), let them read it
    if id strata &>/dev/null; then
        chown strata:strata "$KEY_FILE"
        chown strata:strata "$KEY_DIR"
    fi
    echo "[OK] Key generated and saved to $KEY_FILE (root/strata-only access)"
fi

KEY=$(cat "$KEY_FILE")

# --- Step 3: Migrate existing DB ---
if [ ! -f "$DB_PATH" ]; then
    echo "[SKIP] No existing database to migrate"
else
    echo "[+] Backing up unencrypted DB to $DB_BACKUP..."
    cp "$DB_PATH" "$DB_BACKUP"
    
    echo "[+] Encrypting database with SQLCipher..."
    # sqlcipher can open a plain DB and export to encrypted
    sqlcipher "$DB_PATH" << SQLEOF
ATTACH DATABASE '$DB_ENCRYPTED' AS encrypted KEY '$KEY';
SELECT sqlcipher_export('encrypted');
DETACH DATABASE encrypted;
SQLEOF
    
    if [ -f "$DB_ENCRYPTED" ]; then
        # Verify the encrypted DB works
        VERIFY=$(sqlcipher "$DB_ENCRYPTED" << SQLEOF2
PRAGMA key = '$KEY';
SELECT count(*) FROM thoughts;
SQLEOF2
        )
        echo "[OK] Encrypted DB verified — $VERIFY"
        
        # Swap files
        mv "$DB_PATH" "${DB_PATH}.plain.bak"
        mv "$DB_ENCRYPTED" "$DB_PATH"
        echo "[OK] Encrypted DB is now the active database"
    else
        echo "ERROR: Encryption failed — encrypted file not created"
        exit 1
    fi
fi

# --- Step 4: Update systemd service ---
echo "[+] Adding encryption env vars to systemd service..."
if ! grep -q "STRATA_DB_ENCRYPT" "$STRATA_SERVICE"; then
    # Add encryption env vars after the existing Environment lines
    sed -i '/^Environment=STRATA_LOG_DIR/a Environment=STRATA_DB_ENCRYPT=true\nEnvironment=STRATA_DB_KEY_FILE=/etc/strata/db.key' "$STRATA_SERVICE"
    systemctl daemon-reload
    echo "[OK] Systemd service updated with encryption vars"
else
    echo "[SKIP] Encryption vars already in service file"
fi

echo ""
echo "=== Encryption Complete ==="
echo ""
echo "What changed:"
echo "  - Database is now AES-256 encrypted at rest via SQLCipher"
echo "  - Key stored at $KEY_FILE (mode 400, root/strata only)"
echo "  - Unencrypted backup saved at $DB_BACKUP (DELETE when satisfied)"
echo "  - Strata will auto-detect encryption on next start"
echo ""
echo "To activate: sudo systemctl restart strata"
echo "To verify:   sqlcipher $DB_PATH 'PRAGMA key=\"$KEY\"; SELECT count(*) FROM thoughts;'"
echo ""
echo "⚠️  BACK UP YOUR KEY. If lost, the database is UNRECOVERABLE."
echo "    Key location: $KEY_FILE"
