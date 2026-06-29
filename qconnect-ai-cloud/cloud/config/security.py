"""Security helpers: JWT encode/decode, password hashing placeholder, RBAC.

These wrap PyJWT (falling back gracefully if it is not installed) and provide a
minimal role-based access-control check. Real key management (rotation, vault)
belongs to the deployment environment; this module only offers primitives.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

try:
    import jwt  # PyJWT

    _HAS_JWT = True
except BaseException:  # pragma: no cover - minimal/broken-crypto env
    # Catch BaseException (not just Exception): a misconfigured native
    # cryptography backend can raise a pyo3 PanicException, which must not
    # prevent this module from importing.
    jwt = None  # type: ignore
    _HAS_JWT = False


DEFAULT_ALGORITHM = "HS256"
DEFAULT_TOKEN_TTL_SECONDS = 86_400  # 24h


# --------------------------------------------------------------------------- #
# Role-Based Access Control
# --------------------------------------------------------------------------- #
# Roles ordered from least to most privileged. A user satisfies a required role
# if their role's rank is >= the requirement's rank.
ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 10,
    "operator": 20,
    "qc_manager": 30,
    "lab_director": 40,
    "admin": 100,
}


class AuthError(Exception):
    """Raised when authentication or authorization fails."""


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def create_jwt(
    subject: str,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    extra_claims: dict[str, Any] | None = None,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """Create a signed JWT for ``subject`` (a user id or lab id)."""
    if not _HAS_JWT:  # pragma: no cover
        raise AuthError("PyJWT is not installed")
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
    """Decode and verify a JWT, raising :class:`AuthError` on failure."""
    if not _HAS_JWT:  # pragma: no cover
        raise AuthError("PyJWT is not installed")
    try:
        return jwt.decode(token, secret, algorithms=algorithms or [DEFAULT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:  # type: ignore[attr-defined]
        raise AuthError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:  # type: ignore[attr-defined]
        raise AuthError("Invalid token") from exc


# --------------------------------------------------------------------------- #
# Password hashing (placeholder — replace with argon2/bcrypt in production)
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, salt: str = "qconnect-static-salt") -> str:
    """Hash a password.

    PLACEHOLDER: uses PBKDF2-HMAC-SHA256 with a static salt for portability.
    Production MUST use a per-user random salt and a memory-hard KDF (argon2id).
    """
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return dk.hex()


def verify_password(password: str, hashed: str, *, salt: str = "qconnect-static-salt") -> bool:
    """Constant-time password verification against :func:`hash_password`."""
    return hmac.compare_digest(hash_password(password, salt=salt), hashed)


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
def has_role(user_role: str | None, required_role: str) -> bool:
    """Return True if ``user_role`` meets or exceeds ``required_role``.

    Unknown roles are treated as having zero privilege.
    """
    if user_role is None:
        return False
    user_rank = ROLE_HIERARCHY.get(user_role.lower(), 0)
    required_rank = ROLE_HIERARCHY.get(required_role.lower(), 9_999)
    allowed = user_rank >= required_rank
    if not allowed:
        logger.debug("RBAC denied: role={} required={}", user_role, required_role)
    return allowed


def require_role(claims: dict[str, Any], required_role: str) -> None:
    """Raise :class:`AuthError` unless the token claims satisfy ``required_role``."""
    if not has_role(claims.get("role"), required_role):
        raise AuthError(f"Requires role '{required_role}'")
