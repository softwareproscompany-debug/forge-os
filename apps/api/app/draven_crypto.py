"""Fernet encryption for Draven per-business provider secrets.

Secrets (provider API keys, custom base URLs) are encrypted with a
server-side key (``DRAVEN_CONFIG_KEY`` env var) before storage in
``draven_provider_config``. The key never leaves the server and encrypted
values are never returned to clients.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


def get_fernet(config_key: str) -> Fernet:
    """Build a Fernet instance from the configured key.

    Raises ``ValueError`` when the key is missing or malformed — callers
    must fail closed (refuse to store secrets) rather than fall back to
    plaintext.
    """
    if not config_key or not config_key.strip():
        raise ValueError(
            "DRAVEN_CONFIG_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\" "
            "and set it before saving provider credentials."
        )
    try:
        return Fernet(config_key.strip().encode())
    except Exception as exc:
        raise ValueError(
            "DRAVEN_CONFIG_KEY is not a valid Fernet key "
            "(expected base64url-encoded 32 bytes)."
        ) from exc


def encrypt_secret(fernet: Fernet, plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(fernet: Fernet, token: str | None) -> str | None:
    if token is None:
        return None
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Stored provider secret cannot be decrypted — the "
            "DRAVEN_CONFIG_KEY may have changed."
        ) from exc
