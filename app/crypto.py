"""Encryption at rest for upstream credentials, and opaque id generation."""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def seal(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def open(self, ciphertext: bytes) -> str | None:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken:
            return None


def new_opaque_id() -> str:
    """22 url-safe chars, 128 bits of randomness."""
    return secrets.token_urlsafe(16)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge
