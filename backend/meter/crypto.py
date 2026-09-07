"""Symmetric encryption for credentials stored at rest.

Uses Fernet (AES-128-CBC + HMAC, authenticated) with a key derived from
``APP_SECRET_KEY``. Plaintext connector secrets are encrypted before they ever
touch the database; only ciphertext is persisted (see connector_credential).
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    secret = os.environ.get("APP_SECRET_KEY")
    if not secret:
        raise RuntimeError("APP_SECRET_KEY is not set; cannot encrypt/decrypt credentials.")
    # Fernet needs a 32-byte url-safe base64 key; derive one deterministically.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> bytes:
    """Encrypt a secret string, returning ciphertext bytes for bytea storage."""
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    """Decrypt ciphertext bytes back to the original secret string."""
    return _fernet().decrypt(bytes(token)).decode()
