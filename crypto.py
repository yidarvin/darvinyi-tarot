"""
Fernet symmetric encryption for per-user Anthropic API key storage.

Requires the FERNET_KEY environment variable — a 32-byte URL-safe base64
key generated once at setup:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Store the output in .env as FERNET_KEY=<value>. Keep it separate from
Flask's SECRET_KEY; they serve different cryptographic purposes and should
never share the same value.
"""

import os

from cryptography.fernet import Fernet, InvalidToken


def _get_fernet() -> Fernet:
    key = os.environ.get("FERNET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FERNET_KEY environment variable is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt a plaintext API key string; returns a URL-safe token string."""
    if not plaintext:
        raise ValueError("API key must not be empty")
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(token: str) -> str:
    """Decrypt a stored token back to a plaintext API key string.

    Raises:
        cryptography.fernet.InvalidToken: if the token is corrupt or the key is wrong.
        RuntimeError: if FERNET_KEY is not set.
    """
    if not token:
        raise ValueError("Token must not be empty")
    return _get_fernet().decrypt(token.encode()).decode()


def generate_key() -> str:
    """Return a new random Fernet key as a printable string (for setup scripts)."""
    return Fernet.generate_key().decode()
