"""
Fernet-based symmetric encryption for DB connection strings.

Usage:
    key = generate_key()           # store in AMA_ENCRYPTION_KEY env var
    token = encrypt(conn_str, key)  # store token in DB
    conn_str = decrypt(token, key)  # retrieve at runtime

The key is a URL-safe base64-encoded 32-byte secret.
Store it in AMA_ENCRYPTION_KEY environment variable — never commit it.
"""
from __future__ import annotations

import base64
import os


def _get_fernet(key: str | bytes | None = None):
    """Return a Fernet instance. Raises ImportError if cryptography not installed."""
    try:
        from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
    except ImportError:
        raise ImportError(
            "cryptography package is required for connection string encryption. "
            "Install with: pip install cryptography"
        )
    from cryptography.fernet import Fernet

    resolved_key: bytes
    if key is None:
        env_key = os.environ.get("AMA_ENCRYPTION_KEY", "")
        if not env_key:
            raise ValueError(
                "AMA_ENCRYPTION_KEY environment variable is not set. "
                "Generate one with: python -c \"from ama.mcp.encryption import generate_key; print(generate_key())\""
            )
        resolved_key = env_key.encode() if isinstance(env_key, str) else env_key
    elif isinstance(key, str):
        resolved_key = key.encode()
    else:
        resolved_key = key

    return Fernet(resolved_key)


def generate_key() -> str:
    """
    Generate a new Fernet key.
    Run once; store output in AMA_ENCRYPTION_KEY.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise ImportError("Install cryptography: pip install cryptography")
    return Fernet.generate_key().decode()


def encrypt(plaintext: str, key: str | bytes | None = None) -> str:
    """Encrypt a connection string. Returns a URL-safe token string."""
    f = _get_fernet(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, key: str | bytes | None = None) -> str:
    """Decrypt a token. Raises ValueError on bad key or corrupted token."""
    try:
        f = _get_fernet(key)
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Failed to decrypt connection string: {exc}") from exc


def mask_connection_string(conn_str: str) -> str:
    """
    Return a safe display version of a connection string with password masked.
    Example: postgresql://user:secret@host/db → postgresql://user:****@host/db
    """
    import re
    return re.sub(r"(:)([^:@/][^@]*)(@)", lambda m: f"{m.group(1)}****{m.group(3)}", conn_str)

