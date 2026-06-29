"""Security helpers shared by edge and cloud: JWT and AES-256-GCM.

These are thin, dependency-light wrappers. Key management (rotation, storage in a
vault/HSM) is the responsibility of the deployment environment; here we only
provide the primitives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from shared.exceptions import AuthenticationError

# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
DEFAULT_ALGORITHM = "HS256"
DEFAULT_TOKEN_TTL_SECONDS = 86_400  # 24h, matches the daily-rotation policy


def create_jwt(
    subject: str,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    extra_claims: dict[str, Any] | None = None,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """Create a signed JWT for ``subject`` (typically a ``lab_id`` or user id)."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_jwt(
    token: str,
    secret: str,
    *,
    algorithms: list[str] | None = None,
) -> dict[str, Any]:
    """Decode and verify a JWT, raising :class:`AuthenticationError` on failure."""
    try:
        return jwt.decode(token, secret, algorithms=algorithms or [DEFAULT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:  # pragma: no cover - thin wrapper
        raise AuthenticationError("Token has expired", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid token", code="token_invalid") from exc


# --------------------------------------------------------------------------- #
# AES-256-GCM (authenticated encryption at rest / in transit envelopes)
# --------------------------------------------------------------------------- #
_NONCE_BYTES = 12  # 96-bit nonce, recommended for GCM


def generate_aes_key() -> bytes:
    """Generate a random 256-bit AES key."""
    return AESGCM.generate_key(bit_length=256)


def aes_encrypt(plaintext: bytes, key: bytes, *, associated_data: bytes | None = None) -> str:
    """Encrypt ``plaintext`` with AES-256-GCM.

    Returns a base64url string of ``nonce || ciphertext || tag``.
    """
    if len(key) != 32:
        raise ValueError("AES-256 requires a 32-byte key")
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def aes_decrypt(token: str, key: bytes, *, associated_data: bytes | None = None) -> bytes:
    """Reverse :func:`aes_encrypt`. Raises ValueError/InvalidTag on tampering."""
    if len(key) != 32:
        raise ValueError("AES-256 requires a 32-byte key")
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ct, associated_data)


# --------------------------------------------------------------------------- #
# Payload signing (tamper detection for synced records)
# --------------------------------------------------------------------------- #
def sign_payload(payload: bytes, secret: str) -> str:
    """Return a hex HMAC-SHA256 signature for ``payload``."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Constant-time verification of an HMAC-SHA256 signature."""
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature)
