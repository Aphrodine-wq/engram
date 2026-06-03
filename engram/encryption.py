"""
encryption.py — At-rest encryption for Claude Eyes screen data.

Encrypts sensitive fields (text, window_title, extra_context) using Fernet
symmetric encryption. Key is stored in the OS keychain (macOS Keychain,
Linux Secret Service, Windows Credential Manager).

Design:
- Encryption is opt-in via config: {"encryption": true}
- Non-sensitive fields (timestamp, app_name, project, phash) stay plaintext
  so indexing, filtering, and basic queries still work fast
- FTS5 indexes encrypted fields as opaque blobs — full-text search
  decrypts matching rows at the application layer
- Existing unencrypted DBs can be migrated with encrypt_existing_db()
- All crypto is Fernet (AES-128-CBC + HMAC-SHA256) — authenticated encryption

Threat model:
- Protects against: DB file exfiltration, casual disk access
- Does NOT protect against: root access while daemon is running (key in memory)
- Combined with: privacy.py redaction (removes secrets pre-storage),
  file permissions (600), and ignore_apps filtering
"""

from __future__ import annotations

import os
import base64
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger("eyes.encryption")

# Lazy imports — don't break if deps missing
_fernet = None
_keyring = None

SERVICE_NAME = "ai-knows-me"
OLD_SERVICE_NAME = "claude-eyes"
KEY_NAME = "encryption-key"


def _get_fernet():
    """Lazy-load Fernet cipher with key from keychain."""
    global _fernet
    if _fernet is not None:
        return _fernet

    try:
        from cryptography.fernet import Fernet
        import keyring
    except ImportError:
        raise RuntimeError(
            "Encryption requires 'cryptography' and 'keyring' packages. "
            "Install: pip install cryptography keyring"
        )

    # Try to load existing key from OS keychain (check both old and new service names)
    key = keyring.get_password(SERVICE_NAME, KEY_NAME)
    if key is None:
        key = keyring.get_password(OLD_SERVICE_NAME, KEY_NAME)
        if key is not None:
            # Migrate key to new service name
            keyring.set_password(SERVICE_NAME, KEY_NAME, key)
            logger.info("Migrated encryption key from old service name")

    if key is None:
        # Generate and store a new key
        key = Fernet.generate_key().decode("utf-8")
        keyring.set_password(SERVICE_NAME, KEY_NAME, key)
        logger.info("Generated new encryption key and stored in OS keychain")

    _fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    return _fernet


def is_available() -> bool:
    """Check if encryption dependencies are installed."""
    try:
        import cryptography  # noqa: F401
        import keyring  # noqa: F401
        return True
    except ImportError:
        return False


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns base64-encoded ciphertext prefixed with 'ENC:'."""
    if not plaintext:
        return plaintext
    f = _get_fernet()
    token = f.encrypt(plaintext.encode("utf-8"))
    return "ENC:" + token.decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a string. Handles both encrypted ('ENC:...') and plaintext transparently."""
    if not ciphertext:
        return ciphertext
    if not ciphertext.startswith("ENC:"):
        # Not encrypted — return as-is (supports mixed encrypted/plaintext DBs)
        return ciphertext
    f = _get_fernet()
    token = ciphertext[4:].encode("utf-8")
    return f.decrypt(token).decode("utf-8")


def encrypt_fields(text: str, window_title: str, extra_context: str) -> tuple[str, str, str]:
    """Encrypt the three sensitive fields. Returns (enc_text, enc_title, enc_context)."""
    return encrypt(text), encrypt(window_title), encrypt(extra_context)


def decrypt_fields(text: str, window_title: str, extra_context: str) -> tuple[str, str, str]:
    """Decrypt the three sensitive fields. Handles mixed encrypted/plaintext."""
    return decrypt(text), decrypt(window_title), decrypt(extra_context)


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted."""
    return isinstance(value, str) and value.startswith("ENC:")


def export_key() -> str:
    """Export the encryption key (for backup). Returns base64 key string."""
    import keyring
    key = keyring.get_password(SERVICE_NAME, KEY_NAME)
    if key is None:
        raise RuntimeError("No encryption key found in keychain")
    return key


def import_key(key: str) -> None:
    """Import an encryption key (for restore/migration)."""
    # Validate it's a valid Fernet key
    from cryptography.fernet import Fernet
    Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    import keyring
    keyring.set_password(SERVICE_NAME, KEY_NAME, key)
    global _fernet
    _fernet = None  # Force reload
    logger.info("Imported encryption key into OS keychain")


def encrypt_existing_db(db_path: str, batch_size: int = 500) -> dict:
    """
    Migrate an existing unencrypted DB to encrypted.
    Encrypts text, window_title, extra_context in-place.
    Returns stats dict.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM frames WHERE text NOT LIKE 'ENC:%'").fetchone()[0]

    if total == 0:
        conn.close()
        return {"migrated": 0, "already_encrypted": True}

    migrated = 0
    while True:
        rows = conn.execute(
            "SELECT id, text, window_title, extra_context FROM frames "
            "WHERE text NOT LIKE 'ENC:%' LIMIT ?",
            (batch_size,)
        ).fetchall()

        if not rows:
            break

        for row_id, text, title, ctx in rows:
            enc_text, enc_title, enc_ctx = encrypt_fields(text, title, ctx)
            conn.execute(
                "UPDATE frames SET text=?, window_title=?, extra_context=? WHERE id=?",
                (enc_text, enc_title, enc_ctx, row_id)
            )
        conn.commit()
        migrated += len(rows)
        logger.info(f"Encrypted {migrated}/{total} frames...")

    # Rebuild FTS index (now indexes encrypted blobs — search goes through app layer)
    conn.execute("INSERT INTO frames_fts(frames_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    return {"migrated": migrated, "total": total}


def decrypt_existing_db(db_path: str, batch_size: int = 500) -> dict:
    """
    Reverse migration — decrypt all fields back to plaintext.
    Use before disabling encryption or for export.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM frames WHERE text LIKE 'ENC:%'").fetchone()[0]

    if total == 0:
        conn.close()
        return {"decrypted": 0, "already_plaintext": True}

    decrypted = 0
    while True:
        rows = conn.execute(
            "SELECT id, text, window_title, extra_context FROM frames "
            "WHERE text LIKE 'ENC:%' LIMIT ?",
            (batch_size,)
        ).fetchall()

        if not rows:
            break

        for row_id, text, title, ctx in rows:
            plain_text = decrypt(text)
            plain_title = decrypt(title)
            plain_ctx = decrypt(ctx)
            conn.execute(
                "UPDATE frames SET text=?, window_title=?, extra_context=? WHERE id=?",
                (plain_text, plain_title, plain_ctx, row_id)
            )
        conn.commit()
        decrypted += len(rows)

    conn.execute("INSERT INTO frames_fts(frames_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    return {"decrypted": decrypted, "total": total}


def harden_permissions(db_path: str) -> None:
    """Set strict file permissions on the DB and related files."""
    p = Path(db_path)
    for suffix in ["", "-shm", "-wal"]:
        target = Path(str(p) + suffix)
        if target.exists():
            os.chmod(target, 0o600)

    # Also harden the config
    config = p.parent / "config.json"
    if config.exists():
        os.chmod(config, 0o600)
